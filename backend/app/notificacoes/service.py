"""
Lógica de negócio do módulo de notificações.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func
from sqlmodel import Session, select

from app.auth.models import Utilizador
from app.notificacoes.models import Notificacao
from app.notificacoes.schemas import ListaNotificacoesSchema, NotificacaoSchema


def criar_notificacao(
    db: Session,
    *,
    empresa_id: uuid.UUID,
    utilizador_id: uuid.UUID,
    tipo: str,
    titulo: str,
    mensagem: str,
    controlo_empresa_id: uuid.UUID | None = None,
) -> Notificacao:
    """Cria uma nova notificação para o utilizador indicado."""
    notif = Notificacao(
        empresa_id=empresa_id,
        utilizador_id=utilizador_id,
        tipo=tipo,
        titulo=titulo,
        mensagem=mensagem,
        controlo_empresa_id=controlo_empresa_id,
    )
    db.add(notif)
    return notif


def listar_notificacoes(
    db: Session,
    utilizador: Utilizador,
    *,
    limite: int | None = 10,
    apenas_total: bool = False,
) -> ListaNotificacoesSchema:
    """Lista notificações não lidas do utilizador autenticado."""
    filtros = (
        Notificacao.utilizador_id == utilizador.id,
        Notificacao.empresa_id == utilizador.empresa_id,
        Notificacao.lida.is_(False),  # type: ignore[union-attr]
    )

    total: int = db.exec(
        select(func.count())
        .select_from(Notificacao)
        .where(*filtros)
    ).one()

    controlos_com_notificacoes = [
        controlo_empresa_id
        for controlo_empresa_id in db.exec(
            select(Notificacao.controlo_empresa_id)
            .where(
                *filtros,
                Notificacao.controlo_empresa_id.is_not(None),
            )
            .distinct()
        ).all()
        if controlo_empresa_id is not None
    ]

    notifs: list[Notificacao] = []
    if not apenas_total:
        stmt = (
            select(Notificacao)
            .where(*filtros)
            .order_by(Notificacao.created_at.desc())
        )
        if limite is not None:
            stmt = stmt.limit(limite)
        notifs = db.exec(stmt).all()

    schemas_list = [
        NotificacaoSchema.model_validate(n) for n in notifs
    ]
    return ListaNotificacoesSchema(
        total=total,
        nao_lidas=total,
        notificacoes=schemas_list,
        controlos_com_notificacoes=controlos_com_notificacoes,
    )


def marcar_lida(
    db: Session,
    notificacao_id: uuid.UUID,
    utilizador: Utilizador,
) -> bool:
    """Marca uma notificação como lida. Devolve True se encontrada."""
    notif = db.exec(
        select(Notificacao).where(
            Notificacao.id == notificacao_id,
            Notificacao.utilizador_id == utilizador.id,
        )
    ).first()
    if not notif:
        return False
    notif.lida = True
    db.add(notif)
    return True


def marcar_lidas_por_controlo(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    utilizador: Utilizador,
) -> int:
    """
    Marca como lidas todas as notificações deste utilizador
    associadas ao controlo indicado. Devolve o nº de marcações.
    """
    notifs = db.exec(
        select(Notificacao).where(
            Notificacao.controlo_empresa_id == controlo_empresa_id,
            Notificacao.utilizador_id == utilizador.id,
            Notificacao.lida.is_(False),  # type: ignore[union-attr]
        )
    ).all()
    for n in notifs:
        n.lida = True
        db.add(n)
    return len(notifs)
