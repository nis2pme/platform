"""Ligações evidência ↔ requisito.

Toda a leitura e escrita da relação N:N passa por aqui. Ter um sítio só evita o
que aconteceu noutros módulos deste repo: três cópias do mesmo `ON CONFLICT`, uma
das quais já divergira sem ninguém dar por isso.

O modelo é **append-only**. Desligar preenche `desligado_em`; religar cria linha
nova. Nada se apaga, porque a pergunta que isto serve para responder — *"que
provas sustentavam este controlo em março?"* — deixa de ter resposta no momento
em que se apaga uma linha.
"""
import uuid
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.evidencias.models import Evidencia, EvidenciaRequisito


def ligar(
    db: Session,
    *,
    evidencia_id: uuid.UUID,
    requisito_id: uuid.UUID,
    empresa_id: uuid.UUID,
    ligado_por_id: uuid.UUID | None = None,
    nota_ambito: str | None = None,
    ambito_por_confirmar: bool = False,
) -> EvidenciaRequisito:
    """Liga uma evidência a um requisito. Idempotente enquanto a ligação estiver ativa.

    Religar o que já está ligado devolve a ligação existente em vez de criar uma
    segunda: o índice único parcial recusaria a segunda de qualquer forma, e é
    melhor devolver o que o utilizador queria do que um erro por uma ação que, do
    ponto de vista dele, já estava feita.
    """
    existente = ligacao_ativa(db, evidencia_id=evidencia_id, requisito_id=requisito_id)
    if existente is not None:
        return existente

    ligacao = EvidenciaRequisito(
        evidencia_id=evidencia_id,
        requisito_id=requisito_id,
        empresa_id=empresa_id,
        ligado_por_id=ligado_por_id,
        nota_ambito=nota_ambito,
        ambito_por_confirmar=ambito_por_confirmar,
    )
    db.add(ligacao)
    # O flush materializa a linha antes de qualquer leitura seguinte na mesma
    # transação — sem ele, ligar e listar no mesmo pedido não veria a ligação.
    db.flush()
    return ligacao


def desligar(
    db: Session, *, evidencia_id: uuid.UUID, requisito_id: uuid.UUID
) -> EvidenciaRequisito | None:
    """Desliga (soft) uma evidência de um requisito. Devolve a ligação fechada.

    Não toca na evidência: o conteúdo continua a existir e a sustentar os outros
    requisitos a que esteja ligado. Ficar **órfã** — sem nenhuma ligação ativa —
    é um estado, não um apagamento; quem trata das órfãs é uma varredura
    periódica, e é isso que dá "desfazer" de borla e evita a corrida de dois
    utilizadores a desligarem ao mesmo tempo (ou ninguém apaga, ou apagam os dois).
    """
    ligacao = ligacao_ativa(db, evidencia_id=evidencia_id, requisito_id=requisito_id)
    if ligacao is None:
        return None
    ligacao.desligado_em = datetime.now(timezone.utc)
    db.add(ligacao)
    db.flush()
    return ligacao


def ligacao_ativa(
    db: Session, *, evidencia_id: uuid.UUID, requisito_id: uuid.UUID
) -> EvidenciaRequisito | None:
    return db.exec(
        select(EvidenciaRequisito).where(
            EvidenciaRequisito.evidencia_id == evidencia_id,
            EvidenciaRequisito.requisito_id == requisito_id,
            EvidenciaRequisito.desligado_em.is_(None),
        )
    ).first()


def requisitos_de(db: Session, evidencia_id: uuid.UUID) -> list[EvidenciaRequisito]:
    """Ligações ativas de uma evidência — a quantos controlos ela responde hoje."""
    return list(
        db.exec(
            select(EvidenciaRequisito).where(
                EvidenciaRequisito.evidencia_id == evidencia_id,
                EvidenciaRequisito.desligado_em.is_(None),
            )
        ).all()
    )


def evidencias_de(
    db: Session, *, requisito_id: uuid.UUID, empresa_id: uuid.UUID
) -> list[Evidencia]:
    """Evidências vivas ligadas a um requisito, mais recentes primeiro.

    O filtro por empresa é redundante com a ligação (que já a tem) e está cá de
    propósito: é a condição que mantém o isolamento entre tenants verificável
    numa linha só, sem depender de a ligação ter sido criada corretamente.
    """
    return list(
        db.exec(
            select(Evidencia)
            .join(EvidenciaRequisito, EvidenciaRequisito.evidencia_id == Evidencia.id)
            .where(
                EvidenciaRequisito.requisito_id == requisito_id,
                EvidenciaRequisito.desligado_em.is_(None),
                Evidencia.empresa_id == empresa_id,
                Evidencia.deleted_at.is_(None),
            )
            .order_by(Evidencia.created_at.desc())
        ).all()
    )


def contar_ligacoes_ativas(db: Session, evidencia_id: uuid.UUID) -> int:
    return len(requisitos_de(db, evidencia_id))


def contar_ligacoes_ativas_em_lote(
    db: Session, evidencia_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """O mesmo que `contar_ligacoes_ativas`, para muitas evidências numa consulta."""
    from sqlalchemy import func

    if not evidencia_ids:
        return {}
    linhas = db.exec(
        select(EvidenciaRequisito.evidencia_id, func.count())
        .where(
            EvidenciaRequisito.evidencia_id.in_(evidencia_ids),
            EvidenciaRequisito.desligado_em.is_(None),
        )
        .group_by(EvidenciaRequisito.evidencia_id)
    ).all()
    return {evidencia_id: total for evidencia_id, total in linhas}


def esta_orfa(db: Session, evidencia_id: uuid.UUID) -> bool:
    """Sem nenhuma ligação ativa — candidata a reciclagem, não a apagamento."""
    return contar_ligacoes_ativas(db, evidencia_id) == 0


def ultimas_desligadas(db: Session, evidencia_id: uuid.UUID) -> list[EvidenciaRequisito]:
    """As ligações que a evidência perdeu no último momento em que foi desligada.

    É de onde uma órfã saiu — e para onde "restaurar" a devolve. As desligadas
    antes disso não contam: saíram quando a evidência ainda sustentava outros
    controlos, e foram portanto retiradas de propósito desses, mesmo que um
    segundo antes. O mesmo gesto fecha-as com a mesma hora (`desligar_todas`),
    por isso a comparação é exata.
    """
    fechadas = list(
        db.exec(
            select(EvidenciaRequisito).where(
                EvidenciaRequisito.evidencia_id == evidencia_id,
                EvidenciaRequisito.desligado_em.is_not(None),
            )
        ).all()
    )
    if not fechadas:
        return []
    ultimo = max(l.desligado_em for l in fechadas)
    por_requisito: dict[uuid.UUID, EvidenciaRequisito] = {}
    for ligacao in fechadas:
        if ligacao.desligado_em == ultimo:
            por_requisito.setdefault(ligacao.requisito_id, ligacao)
    return list(por_requisito.values())


def desligar_todas(db: Session, evidencia_id: uuid.UUID) -> int:
    """Fecha todas as ligações ativas de uma evidência. Usado ao eliminá-la.

    Devolve quantas fechou, para quem audita poder dizer de quantos controlos a
    prova saiu — que é a informação que interessa a quem lê o registo depois.
    """
    ligacoes = requisitos_de(db, evidencia_id)
    agora = datetime.now(timezone.utc)
    for ligacao in ligacoes:
        ligacao.desligado_em = agora
        db.add(ligacao)
    if ligacoes:
        db.flush()
    return len(ligacoes)
