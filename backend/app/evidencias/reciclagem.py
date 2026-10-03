"""Reciclagem das evidências sem controlo associado.

Uma evidência que deixa de estar ligada a qualquer controlo não desaparece nem
fica presa: passa a **órfã**, que é um estado, e uma varredura periódica trata as
que lá estiverem há mais de `EVIDENCIA_ORFA_DIAS`. Reciclagem, não caixote —
enquanto lá está, aparece numa lista, conta na quota (é o que dá incentivo a
esvaziá-la) e pode ser religada com um clique.

**Porque não se apaga no momento em que se desliga a última ligação.** Duas
pessoas a desligar ao mesmo tempo leem ambas "ainda resta uma ligação" antes de
gravar, e não apaga ninguém; com o tempo ao contrário, tentam apagar as duas.
Contar referências no instante da decisão é uma corrida. Com estado e varredura,
a pergunta desaparece.

**Retenção ganha à reciclagem.** Uma prova que já sustentou um controlo na
história de conformidade, ou que pode ter saído num dossiê de auditoria, não é
reciclada: fica órfã e retida (a regra vive em `retencao`). Um dossiê antigo cujo
conteúdo desapareceu deixa de ser verificável, e isso é o contrário do produto.
Sair de vez exige um ato explícito — o apagamento com lápide.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.evidencias import apagamentos, ligacoes, retencao
from app.evidencias.models import Evidencia, EvidenciaRequisito
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.pii import decifrar_pii

logger = logging.getLogger(__name__)


def _orfas_da_empresa(db: Session, empresa_id: uuid.UUID) -> dict[uuid.UUID, datetime]:
    """`{evidencia_id: quando ficou órfã}` — sem nenhuma ligação ativa.

    Ficar órfã é não ter ligação **ativa**; a data é a do último desligar, que é
    quando o relógio da reciclagem começa a contar.
    """
    vivas = db.exec(
        select(Evidencia.id).where(
            Evidencia.empresa_id == empresa_id, Evidencia.deleted_at.is_(None)
        )
    ).all()
    if not vivas:
        return {}

    ligacoes_todas = db.exec(
        select(EvidenciaRequisito).where(EvidenciaRequisito.empresa_id == empresa_id)
    ).all()

    com_ligacao_ativa = {
        l.evidencia_id for l in ligacoes_todas if l.desligado_em is None
    }
    ultimo_desligar: dict[uuid.UUID, datetime] = {}
    for ligacao in ligacoes_todas:
        if ligacao.desligado_em is None:
            continue
        anterior = ultimo_desligar.get(ligacao.evidencia_id)
        if anterior is None or ligacao.desligado_em > anterior:
            ultimo_desligar[ligacao.evidencia_id] = ligacao.desligado_em

    return {
        evidencia_id: ultimo_desligar[evidencia_id]
        for evidencia_id in vivas
        if evidencia_id not in com_ligacao_ativa and evidencia_id in ultimo_desligar
    }


def listar_orfas(
    db: Session, empresa_id: uuid.UUID, dias: int
) -> list[dict]:
    """As órfãs da empresa, com o tempo que lhes resta na reciclagem."""
    orfas = _orfas_da_empresa(db, empresa_id)
    if not orfas:
        return []
    motivos = retencao.motivos_em_lote(db, empresa_id, orfas.keys())
    agora = datetime.now(timezone.utc)
    sucessoras = {
        ev.substitui_id: ev.id
        for ev in db.exec(
            select(Evidencia).where(Evidencia.substitui_id.in_(list(orfas.keys())))
        ).all()
    }

    itens = []
    for evidencia in db.exec(
        select(Evidencia).where(Evidencia.id.in_(list(orfas.keys())))
    ).all():
        desde = orfas[evidencia.id]
        # As datas guardadas em SQLite voltam sem fuso; comparar com um datetime
        # com fuso levantaria TypeError a meio de uma listagem inofensiva.
        if desde.tzinfo is None:
            desde = desde.replace(tzinfo=timezone.utc)
        decorridos = (agora - desde).days
        retida = evidencia.id in motivos
        itens.append({
            "id": evidencia.id,
            "titulo": evidencia.titulo,
            "tipo": evidencia.tipo.value,
            "ficheiro_tamanho": evidencia.ficheiro_tamanho,
            "ficheiro_nome": decifrar_pii(evidencia.ficheiro_nome) if evidencia.ficheiro_nome else None,
            "ficheiro_tipo": evidencia.ficheiro_tipo,
            "uploaded_by_id": evidencia.uploaded_by_id,
            "created_at": evidencia.created_at,
            "saiu_de": [l.requisito_id for l in ligacoes.ultimas_desligadas(db, evidencia.id)],
            "substituida_por_id": sucessoras.get(evidencia.id),
            "orfa_desde": desde,
            "dias_orfa": decorridos,
            "dias_ate_reciclar": None if retida else max(0, dias - decorridos),
            # Retida = já foi prova. Fica na lista até ao fim do prazo de
            # conservação, e é isso que se quer: quem a quiser fora antes disso
            # tem de o dizer explicitamente, com o apagamento com lápide.
            "retida": retida,
            "motivos_retencao": sorted(motivos.get(evidencia.id, ())),
        })
    itens.sort(key=lambda i: i["orfa_desde"])
    return itens


def varrer_empresa(db: Session, empresa_id: uuid.UUID, dias: int) -> int:
    """Recicla as órfãs desta empresa que já passaram a janela. Devolve quantas.

    Faz o commit da empresa e só depois remove os ficheiros: se a transação
    falhasse com os ficheiros já fora, ficavam linhas vivas a apontar para nada.
    """
    orfas = _orfas_da_empresa(db, empresa_id)
    if not orfas:
        return 0
    retidos = retencao.ids_retidos(db, empresa_id, orfas.keys())
    agora = datetime.now(timezone.utc)
    limite = agora - timedelta(days=dias)

    ficheiros: list[str | None] = []
    for evidencia in db.exec(
        select(Evidencia).where(Evidencia.id.in_(list(orfas.keys())))
    ).all():
        if evidencia.id in retidos:
            continue
        desde = orfas[evidencia.id]
        if desde.tzinfo is None:
            desde = desde.replace(tzinfo=timezone.utc)
        if desde > limite:
            continue

        # Sai tudo: ficheiro, texto da nota, nome do ficheiro e título. Nunca foi
        # prova de nada, e deixar metade na base não é apagar.
        ficheiros.append(apagamentos.limpar_conteudo(evidencia, agora))
        db.add(evidencia)

        # Sem autor humano — é a única ação deste módulo que ninguém pediu, e
        # por isso a que mais precisa de ficar escrita. Leva a impressão digital
        # do conteúdo e não o título, que pode ser ele próprio um dado pessoal.
        registar_acao(
            db,
            acao=Acao.EVIDENCIA_RECICLADA,
            resultado=ResultadoAcao.SUCESSO,
            empresa_id=empresa_id,
            utilizador_id=None,
            entidade_tipo="Evidencia",
            entidade_id=evidencia.id,
            dados_novos={
                "conteudo_hash": evidencia.conteudo_hash,
                "dias_sem_controlo": (agora - desde).days,
            },
        )

    if not ficheiros:
        return 0
    db.commit()
    apagamentos.remover_ficheiros(ficheiros)
    return len(ficheiros)


def varrer(db: Session, dias: int) -> int:
    """Varre todas as empresas. Chamada pelo tick diário."""
    from app.empresas.models import Empresa

    total = 0
    for empresa_id in db.exec(select(Empresa.id)).all():
        try:
            total += varrer_empresa(db, empresa_id, dias)
        except Exception:  # noqa: BLE001 — uma empresa não pode parar as outras
            logger.exception(
                "Varredura de evidências órfãs falhou para a empresa %s.", empresa_id
            )
            db.rollback()
    if total:
        logger.info("Reciclagem de evidências: %d evidência(s) sem controlo.", total)
    return total
