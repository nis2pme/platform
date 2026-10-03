"""Testemunho externo da trilha de auditoria.

A cadeia de hashes prova que nada mudou **antes** da última cabeça que saiu da
máquina. Quem tem acesso à base pode reescrever a história e recalcular todos os
hashes: a cadeia continua íntegra e nada o denuncia. Por isso, de hora a hora,
as cabeças de todas as cadeias seguem para o fornecedor no heartbeat da licença
(só o hash e a sequência — nenhum dado pessoal), e voltam no recibo assinado.

Conferir é simples: o hash que o fornecedor testemunhou tem de continuar na
trilha. Reescrever uma linha anterior obriga a recalcular os hashes seguintes, e
o testemunhado deixa de existir. O que escapa é só o que se escreve e se
reescreve entre dois heartbeats (uma a duas horas).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from app.shared.audit import AuditLog
from app.shared.audit_cadeia import CADEIA_PLATAFORMA, AuditHashChainHead

logger = logging.getLogger(__name__)

PLATAFORMA = "plataforma"
MAX_CABECAS = 20


def _empresa_de(ambito: str) -> uuid.UUID | None:
    return None if ambito == PLATAFORMA else uuid.UUID(ambito)


def cabecas_atuais(db: Session, *, agora: datetime | None = None) -> list[dict[str, Any]]:
    """As cabeças a entregar: a da plataforma sempre, e as das empresas até ao
    teto. Com mais empresas do que cabem, a janela roda de hora a hora — todas
    acabam testemunhadas, e dentro da mesma hora a lista é a mesma."""
    linhas = db.exec(select(AuditHashChainHead)).all()
    plataforma = []
    empresas = []
    for h in linhas:
        if not h.head_hash or not h.sequencia:
            continue
        c = {
            "ambito": PLATAFORMA if h.empresa_id == CADEIA_PLATAFORMA else str(h.empresa_id),
            "sequencia": int(h.sequencia),
            "head_hash": h.head_hash,
        }
        (plataforma if c["ambito"] == PLATAFORMA else empresas).append(c)
    empresas.sort(key=lambda c: c["ambito"])
    lugares = MAX_CABECAS - len(plataforma)
    if len(empresas) > lugares:
        agora = agora or datetime.now(timezone.utc)
        janelas = -(-len(empresas) // lugares)
        inicio = (int(agora.timestamp() // 3600) % janelas) * lugares
        empresas = (empresas + empresas)[inicio : inicio + lugares]
    return plataforma + empresas


def _instante(texto: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _ambito_existe(db: Session, empresa_id: uuid.UUID | None) -> bool:
    """A cadeia existe nesta instalação: tem cabeça. Uma cadeia de que o sidecar
    fale e que não exista aqui (um id qualquer) não é uma divergência desta
    trilha — não se regista nada por ela."""
    chave = CADEIA_PLATAFORMA if empresa_id is None else empresa_id
    return db.get(AuditHashChainHead, chave) is not None


def _no_arquivo(empresa_id: uuid.UUID | None, head_hash: str, *, directorio=None) -> bool:
    """O hash está numa linha que a retenção já passou para o arquivo."""
    import gzip

    from app.auditoria import arquivo

    pasta = directorio if directorio is not None else arquivo.ARQUIVO_DIR
    alvo = str(empresa_id) if empresa_id is not None else "plataforma"
    if not pasta.is_dir():
        return False
    for ficheiro in pasta.glob(f"audit-{alvo}-*.jsonl.gz"):
        try:
            with gzip.open(ficheiro, "rt", encoding="utf-8") as fh:
                for linha in fh:
                    if head_hash in linha and json.loads(linha).get("hash_registo") == head_hash:
                        return True
        except (OSError, ValueError):
            logger.warning("testemunho: arquivo da trilha ilegível: %s", ficheiro.name)
    return False


def _reposto_depois(db: Session, empresa_id: uuid.UUID | None, quando: datetime | None) -> bool:
    """Houve um restauro de backup depois do testemunho: a trilha voltou atrás."""
    from app.shared.audit import Acao

    if quando is None:
        return False
    condicao = AuditLog.empresa_id == empresa_id if empresa_id else AuditLog.empresa_id.is_(None)
    return db.exec(
        select(AuditLog.id)
        .where(condicao, AuditLog.acao == Acao.RESTAURO_EXECUTADO, AuditLog.created_at > quando)
        .limit(1)
    ).first() is not None


def divergencias(db: Session, testemunhadas: list[dict], testemunhadas_em: str, *, arquivo_dir=None) -> list[dict[str, Any]]:
    """As cabeças testemunhadas que já não estão na trilha.

    Um hash que saiu da tabela conta como presente só se estiver mesmo no
    arquivo da retenção. As datas das linhas não decidem nada: quem reescreve a
    trilha escolhe-as. Um restauro de backup posterior ao testemunho não cala a
    divergência — fica registada como reposição (a trilha voltou atrás por um
    restauro), para quem a lê distinguir de uma reescrita."""
    quando = _instante(testemunhadas_em)
    achados = []
    for c in testemunhadas:
        try:
            empresa_id = _empresa_de(c["ambito"])
        except (KeyError, ValueError):
            continue
        if not _ambito_existe(db, empresa_id):
            continue
        condicao = AuditLog.empresa_id == empresa_id if empresa_id else AuditLog.empresa_id.is_(None)
        existe = db.exec(
            select(AuditLog.id).where(condicao, AuditLog.hash_registo == c["head_hash"]).limit(1)
        ).first()
        if existe or _no_arquivo(empresa_id, c["head_hash"], directorio=arquivo_dir):
            continue
        motivo = "reposicao" if _reposto_depois(db, empresa_id, quando) else "reescrita"
        achados.append({**c, "testemunhada_em": testemunhadas_em, "motivo": motivo})
    return achados


def _ja_registado(db: Session, achado: dict[str, Any]) -> bool:
    from app.shared.audit import Acao

    empresa_id = _empresa_de(achado["ambito"])
    condicao = AuditLog.empresa_id == empresa_id if empresa_id else AuditLog.empresa_id.is_(None)
    ultimo = db.exec(
        select(AuditLog.dados_novos)
        .where(condicao, AuditLog.acao == Acao.AUDIT_TESTEMUNHO_DIVERGENTE)
        .order_by(AuditLog.created_at.desc())  # type: ignore[union-attr]
        .limit(1)
    ).first()
    if not ultimo:
        return False
    try:
        return json.loads(ultimo).get("head_hash") == achado["head_hash"]
    except (TypeError, ValueError):
        return False


def testemunhar_e_conferir(db: Session, entregar=None) -> dict[str, Any]:
    """O tick de hora a hora: entrega as cabeças atuais ao sidecar e confere as
    que o fornecedor testemunhou. Uma divergência fica no log e na trilha da
    empresa (uma vez por hash testemunhado)."""
    from app.shared.audit import Acao, ResultadoAcao, registar_acao

    if entregar is None:
        from app.premium.trilha_client import testemunhar as entregar

    resposta = entregar(cabecas_atuais(db))
    if not resposta or not resposta.get("ativo"):
        return {"ativo": False, "divergencias": []}
    achados = divergencias(db, resposta["testemunhadas"], resposta["testemunhadas_em"])
    for achado in achados:
        if achado["motivo"] == "reposicao":
            logger.warning(
                "Trilha de auditoria atrás do testemunho do fornecedor (%s, sequência %s, testemunhada em %s): "
                "houve um restauro de backup depois do testemunho.",
                achado["ambito"], achado["sequencia"], achado["testemunhada_em"],
            )
        else:
            logger.error(
                "Trilha de auditoria DIVERGE do testemunho do fornecedor (%s, sequência %s, testemunhada em %s): "
                "a história anterior foi reescrita.",
                achado["ambito"], achado["sequencia"], achado["testemunhada_em"],
            )
        if _ja_registado(db, achado):
            continue
        registar_acao(
            db,
            acao=Acao.AUDIT_TESTEMUNHO_DIVERGENTE,
            resultado=ResultadoAcao.FALHA,
            empresa_id=_empresa_de(achado["ambito"]),
            dados_novos={k: achado[k] for k in ("sequencia", "head_hash", "testemunhada_em", "motivo")},
        )
    db.commit()
    return {"ativo": True, "testemunhadas": len(resposta["testemunhadas"]), "divergencias": achados}
