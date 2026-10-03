"""
Retenção do audit log: arquivar em ficheiro e só depois apagar da tabela.

## Janela deslizante, não despejo em bloco

O tick diário pergunta apenas *"há registos mais antigos do que a retenção?"*. Cada
dia que passa cai um dia pela cauda e entra um dia novo pela frente — a base mantém
sempre a janela configurada e nunca fica sem histórico. Numa instalação recente isto
não faz nada durante cerca de um ano, o que é o comportamento correto e a razão de os
testes terem de semear datas antigas à mão.

## Um ficheiro por tenant e por mês civil

`audit-<empresa_id>-<AAAA-MM>.jsonl.gz`, mais `audit-plataforma-<AAAA-MM>.jsonl.gz`
para as ações sem empresa associada. Numa instalação on-prem há um só tenant, portanto
há um ficheiro por mês. Separar por tenant é o que permite ao hard-delete de uma conta
apagar também o que ficou em arquivo: sem isso, a purga apagaria as linhas da tabela e
deixaria as mesmas linhas dentro de um ficheiro que a escrita única impede de reescrever.

O tick só toca num mês quando ele sai INTEIRO da janela de retenção. Consequência a
assumir: a retenção arredonda para o fim do mês — com 365 dias a base guarda entre 12 e
13 meses. Nunca menos do que o configurado, e nunca um mês partido ao meio.

## Ordem: escrever, garantir no disco, só então apagar

O ficheiro é escrito para `.tmp`, sincronizado e renomeado. O rename é atómico, portanto
um ficheiro com o nome final é sempre um ficheiro completo. Só depois disso é que as
linhas saem da tabela — nunca há um instante em que um registo não esteja em lado nenhum.
Se o processo morrer entre o rename e o DELETE, a execução seguinte encontra o ficheiro
já fechado, não lhe toca, e limita-se a terminar o apagamento.

Cada ficheiro é escrito uma vez e nunca reaberto. É essa propriedade que sustenta a
garantia acima, e é também por isso que a máscara de dados pessoais tem de ser aplicada
na escrita: não existe momento posterior em que se possa corrigir o conteúdo.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete
from sqlmodel import Session, select

from app.config import get_settings
from app.shared.anonimizacao import mascarar_pii_em_dados
from app.shared.audit import Acao, AuditLog, registar_acao

logger = logging.getLogger(__name__)

_DATA_DIR = Path("/app/data")
ARQUIVO_DIR = _DATA_DIR / "audit-archive"

# Teto de meses tratados por execução. Baixar a retenção de 24 para 6 meses deixa 18
# meses por arquivar de uma vez; sem teto, o primeiro tick a seguir ficaria a trabalhar
# durante muito tempo. O resto apanha-se nos dias seguintes.
MAX_MESES_POR_EXECUCAO = 3

# Linhas apagadas por transação, para não segurar a tabela num só DELETE.
LOTE_APAGAR = 5000

_BUCKET_PLATAFORMA = "plataforma"


# ---------------------------------------------------------------------------
# Nomes e datas
# ---------------------------------------------------------------------------

def nome_ficheiro(empresa_id: _uuid.UUID | None, mes: datetime) -> str:
    """Nome do ficheiro de arquivo de um tenant num mês. Ordena sozinho pelo nome."""
    alvo = str(empresa_id) if empresa_id is not None else _BUCKET_PLATAFORMA
    return f"audit-{alvo}-{mes.year:04d}-{mes.month:02d}.jsonl.gz"


def _inicio_do_mes(momento: datetime) -> datetime:
    return datetime(momento.year, momento.month, 1, tzinfo=timezone.utc)


def _mes_seguinte(inicio: datetime) -> datetime:
    if inicio.month == 12:
        return datetime(inicio.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(inicio.year, inicio.month + 1, 1, tzinfo=timezone.utc)


def _com_fuso(momento: datetime | None) -> datetime | None:
    """O SQLite devolve datetimes sem fuso; o Postgres devolve-os com. Normaliza para UTC."""
    if momento is None:
        return None
    if momento.tzinfo is None:
        return momento.replace(tzinfo=timezone.utc)
    return momento.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Serialização
# ---------------------------------------------------------------------------

def linha_para_arquivo(log: AuditLog) -> dict:
    """
    Converte um registo para a linha JSON que vai para o arquivo.

    `ip_address` e `user_agent` saem CIFRADOS, exatamente como estão na base: são a
    PII com valor forense real (de onde veio o acesso) e o arquivo herda a proteção da
    tabela — a PII_ENCRYPTION_KEY continua a ser a única forma de os ler.

    `dados_anteriores` e `dados_novos` saem MASCARADOS, sempre. São JSON em claro e
    transportam email e nome; num ficheiro que nunca mais é reescrito, uma anonimização
    posterior nunca os alcançaria. O `utilizador_id` fica e é ele que dá a
    rastreabilidade enquanto a conta existir.

    Os três hashes da cadeia seguem para o arquivo tal e qual. Sem eles, a
    purga cortaria a cadeia ao meio e a história arquivada deixaria de ser
    verificável — que é o contrário do que um arquivo serve para fazer. Como o
    `hash_registo` é calculado sobre o `dados_hash` e não sobre o texto dos dados,
    o mascaramento acima **não** invalida a verificação: uma linha arquivada
    continua a encadear na seguinte, e só a reconstrução do conteúdo mascarado
    fica fora de alcance — que é a intenção.
    """
    resultado = log.resultado
    return {
        "id": str(log.id),
        "created_at": _com_fuso(log.created_at).isoformat(),
        "empresa_id": str(log.empresa_id) if log.empresa_id else None,
        "utilizador_id": str(log.utilizador_id) if log.utilizador_id else None,
        "acao": log.acao,
        "entidade_tipo": log.entidade_tipo,
        "entidade_id": str(log.entidade_id) if log.entidade_id else None,
        "dados_anteriores": mascarar_pii_em_dados(log.dados_anteriores),
        "dados_novos": mascarar_pii_em_dados(log.dados_novos),
        "ip_address": log.ip_address,
        # Entra no hash do registo: sem ele, a linha arquivada não voltava a
        # verificar. É um HMAC, não o IP.
        "ip_hash": log.ip_hash,
        "user_agent": log.user_agent,
        "resultado": getattr(resultado, "value", resultado),
        "dados_hash": log.dados_hash,
        "hash_anterior": log.hash_anterior,
        "hash_registo": log.hash_registo,
    }


# ---------------------------------------------------------------------------
# Escrita e apagamento
# ---------------------------------------------------------------------------

def _escrever_ficheiro(
    db: Session,
    empresa_id: _uuid.UUID | None,
    inicio: datetime,
    fim: datetime,
    caminho: Path,
) -> int:
    """Escreve o `.jsonl.gz` de forma atómica. Devolve o número de linhas escritas."""
    temporario = caminho.with_name(caminho.name + ".tmp")
    escritas = 0

    stmt = (
        select(AuditLog)
        .where(
            AuditLog.empresa_id == empresa_id,
            AuditLog.created_at >= inicio,
            AuditLog.created_at < fim,
        )
        .order_by(AuditLog.created_at)  # type: ignore[arg-type]
    )

    with open(temporario, "wb") as bruto:
        # mtime=0 torna o ficheiro reproduzível: dois arquivos do mesmo conteúdo
        # ficam byte a byte iguais, o que ajuda a comparar backups.
        with gzip.GzipFile(fileobj=bruto, mode="wb", mtime=0) as gz:
            for log in db.exec(stmt).yield_per(1000):
                linha = json.dumps(linha_para_arquivo(log), ensure_ascii=False)
                gz.write(f"{linha}\n".encode("utf-8"))
                escritas += 1
        bruto.flush()
        os.fsync(bruto.fileno())

    os.replace(temporario, caminho)

    # Sincronizar o directório torna o rename durável. Não existe em Windows, onde
    # abrir um directório dá erro — aí o rename já é o suficiente na prática.
    try:
        fd = os.open(caminho.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        pass

    return escritas


def _apagar_linhas(
    db: Session,
    empresa_id: _uuid.UUID | None,
    inicio: datetime,
    fim: datetime,
) -> int:
    """Apaga as linhas já arquivadas, por lotes. Devolve o total apagado."""
    apagadas = 0
    while True:
        ids = list(
            db.exec(
                select(AuditLog.id)  # type: ignore[arg-type]
                .where(
                    AuditLog.empresa_id == empresa_id,
                    AuditLog.created_at >= inicio,
                    AuditLog.created_at < fim,
                )
                .limit(LOTE_APAGAR)
            ).all()
        )
        if not ids:
            return apagadas
        resultado = db.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))  # type: ignore[attr-defined]
        db.commit()
        apagadas += resultado.rowcount or 0


def _corte_atual(db: Session, empresa_id: _uuid.UUID | None) -> str | None:
    """O `hash_anterior` do primeiro elo que ficou vivo depois de arquivar.

    Se o arquivo levou todas as linhas encadeadas, a parte viva recomeça na
    cabeça da cadeia (é dela que a entrada da purga se vai encadear)."""
    from app.auditoria.verificar_cadeia import _registos
    from app.shared import audit_cadeia

    for registo in audit_cadeia.pela_ordem_da_cadeia(_registos(db, empresa_id)):
        if registo.hash_registo is not None:
            return registo.hash_anterior or audit_cadeia.GENESE
    cabeca = db.get(audit_cadeia.AuditHashChainHead, empresa_id or audit_cadeia.CADEIA_PLATAFORMA)
    return cabeca.head_hash if cabeca else None


def _arquivar_mes(db: Session, inicio: datetime, fim: datetime) -> list[dict]:
    """Arquiva e apaga um mês civil, um ficheiro por tenant com registos nesse mês."""
    empresas = list(
        db.exec(
            select(AuditLog.empresa_id)  # type: ignore[arg-type]
            .where(AuditLog.created_at >= inicio, AuditLog.created_at < fim)
            .distinct()
        ).all()
    )

    ARQUIVO_DIR.mkdir(parents=True, exist_ok=True)
    relatorio: list[dict] = []

    for empresa_id in empresas:
        caminho = ARQUIVO_DIR / nome_ficheiro(empresa_id, inicio)

        if caminho.exists():
            # Só se chega aqui se uma execução anterior morreu entre o rename e o
            # DELETE. O ficheiro está completo (o rename é atómico) — não se reabre,
            # só se termina o apagamento.
            logger.warning(
                "Arquivo %s já existe com linhas ainda na tabela — execução anterior "
                "interrompida. O ficheiro não é reescrito; as linhas são apagadas.",
                caminho.name,
            )
            escritas = 0
        else:
            escritas = _escrever_ficheiro(db, empresa_id, inicio, fim, caminho)

        apagadas = _apagar_linhas(db, empresa_id, inicio, fim)

        # A purga da própria trilha de auditoria fica registada na trilha. Um
        # apagamento silencioso é exatamente o que um auditor não quer encontrar.
        # O `corte` é o elo de onde a parte viva passa a começar: a verificação
        # só aceita um início diferente da génese se for um corte registado aqui,
        # e é isso que denuncia linhas apagadas do princípio por outra via.
        registar_acao(
            db,
            acao=Acao.AUDIT_PURGADO,
            empresa_id=empresa_id,
            entidade_tipo="AuditLog",
            dados_novos={
                "mes": f"{inicio.year:04d}-{inicio.month:02d}",
                "ficheiro": caminho.name,
                "linhas_arquivadas": escritas,
                "linhas_apagadas": apagadas,
                "corte": _corte_atual(db, empresa_id),
            },
        )
        db.commit()

        logger.info(
            "Audit log arquivado: %s (%s linhas escritas, %s apagadas).",
            caminho.name, escritas, apagadas,
        )
        relatorio.append({
            "empresa_id": str(empresa_id) if empresa_id else None,
            "mes": f"{inicio.year:04d}-{inicio.month:02d}",
            "ficheiro": caminho.name,
            "linhas_arquivadas": escritas,
            "linhas_apagadas": apagadas,
        })

    return relatorio


# ---------------------------------------------------------------------------
# Ponto de entrada do tick
# ---------------------------------------------------------------------------

def arquivar_e_purgar(db: Session, *, agora: datetime | None = None) -> list[dict]:
    """
    Arquiva e apaga os meses civis que saíram inteiros da janela de retenção.

    Devolve um relatório por ficheiro escrito — vazio na esmagadora maioria dos dias,
    porque só há trabalho quando vira um mês.
    """
    dias = get_settings().AUDIT_RETENCAO_DIAS
    if dias <= 0:
        return []

    agora = agora or datetime.now(timezone.utc)
    corte = agora - timedelta(days=dias)
    # Qualquer mês que comece ANTES do mês do corte está inteiramente fora da janela.
    # O mês do próprio corte fica de fora — está partido ao meio.
    limite = _inicio_do_mes(corte)

    mais_antigo = _com_fuso(
        db.exec(select(AuditLog.created_at).where(AuditLog.created_at < limite).order_by(AuditLog.created_at).limit(1)).first()  # type: ignore[arg-type]
    )
    if mais_antigo is None:
        return []

    relatorio: list[dict] = []
    inicio = _inicio_do_mes(mais_antigo)
    for _ in range(MAX_MESES_POR_EXECUCAO):
        if inicio >= limite:
            break
        fim = _mes_seguinte(inicio)
        relatorio.extend(_arquivar_mes(db, inicio, fim))
        # O mês seguinte COM registos: um mês vazio não dá trabalho e não pode gastar
        # o teto. Com um buraco na história, os meses a seguir ficavam dias a mais
        # na tabela, já fora da retenção.
        seguinte = _com_fuso(
            db.exec(
                select(AuditLog.created_at)
                .where(AuditLog.created_at >= fim, AuditLog.created_at < limite)  # type: ignore[arg-type]
                .order_by(AuditLog.created_at)  # type: ignore[arg-type]
                .limit(1)
            ).first()
        )
        if seguinte is None:
            break
        inicio = _inicio_do_mes(seguinte)

    return relatorio


def listar_arquivos_do_tenant(
    empresa_id: str, *, directorio: Path | None = None
) -> list[dict]:
    """
    Meses de um tenant que já saíram da janela e estão em ficheiro.

    Só metadados — nome, mês e tamanho. O conteúdo não se serve pela aplicação:
    são ficheiros de leitura pontual, com PII cifrada lá dentro, e abri-los pela
    API criaria um segundo caminho de acesso a dados que a janela de retenção
    já tirou de circulação.

    Existe porque sem isto o utilizador não tem como saber que há histórico fora
    do que a listagem lhe mostra — e concluiria que a trilha começa no dia em
    que a retenção corta.
    """
    pasta = directorio if directorio is not None else ARQUIVO_DIR
    if not pasta.is_dir():
        return []
    meses = []
    for ficheiro in sorted(pasta.glob(f"audit-{empresa_id}-*.jsonl.gz"), reverse=True):
        # audit-<empresa>-AAAA-MM.jsonl.gz — o mês são os dois últimos campos.
        partes = ficheiro.name[: -len(".jsonl.gz")].split("-")
        mes = "-".join(partes[-2:]) if len(partes) >= 2 else "—"
        meses.append({
            "mes": mes,
            "ficheiro": ficheiro.name,
            "bytes": ficheiro.stat().st_size,
        })
    return meses


def apagar_arquivos_do_tenant(empresa_id: str, *, directorio: Path | None = None) -> list[str]:
    """
    Apaga todos os ficheiros de arquivo de um tenant. Usado pelo hard-delete da conta:
    sem isto a purga apagava as linhas da tabela e deixava as mesmas linhas em arquivo.

    Devolve os nomes apagados. Não silencia erros de I/O — uma purga que falha tem de
    falhar em voz alta, nunca ficar registada como concluída.
    """
    pasta = directorio if directorio is not None else ARQUIVO_DIR
    if not pasta.is_dir():
        return []
    apagados = []
    for ficheiro in sorted(pasta.glob(f"audit-{empresa_id}-*.jsonl.gz")):
        ficheiro.unlink()
        apagados.append(ficheiro.name)
    return apagados
