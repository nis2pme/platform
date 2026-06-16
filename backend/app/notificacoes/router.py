"""
Router do módulo de notificações.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlmodel import Session

from app.database import get_session
from app.notificacoes import service
from app.notificacoes.schemas import (
    ListaNotificacoesSchema,
    ResultadoNotificacoesMarcadasSchema,
)
from app.shared.dependencies import CurrentUserDep

router = APIRouter(prefix="/notificacoes", tags=["Notificações"])


@router.get(
    "",
    response_model=ListaNotificacoesSchema,
    summary="Listar notificações não lidas",
)
async def listar_notificacoes(
    utilizador: CurrentUserDep,
    limite: int | None = Query(10, ge=1, le=20),
    apenas_total: bool = False,
    db: Session = Depends(get_session),
):
    """Devolve notificações não lidas do utilizador autenticado."""
    return service.listar_notificacoes(
        db,
        utilizador,
        limite=limite,
        apenas_total=apenas_total,
    )


@router.put(
    "/{notificacao_id}/lida",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Marcar notificação como lida",
)
async def marcar_lida(
    notificacao_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """Marca uma notificação específica como lida."""
    service.marcar_lida(db, notificacao_id, utilizador)
    db.commit()


@router.put(
    "/controlo/{controlo_empresa_id}/lidas",
    response_model=ResultadoNotificacoesMarcadasSchema,
    status_code=status.HTTP_200_OK,
    summary="Marcar notificações de um controlo como lidas",
)
async def marcar_lidas_por_controlo(
    controlo_empresa_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """
    Marca como lidas todas as notificações deste utilizador
    relativas ao controlo indicado. Chamado ao visitar o detalhe do controlo.
    """
    marcadas = service.marcar_lidas_por_controlo(
        db,
        controlo_empresa_id,
        utilizador,
    )
    db.commit()
    return ResultadoNotificacoesMarcadasSchema(marcadas=marcadas)
