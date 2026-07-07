"""
Router do Assistente IA (premium) — endpoints finos, gated por
`require_feature("ai_assistant")` (402 se o tenant não tem o módulo).

A lógica vive em analise.py; a chamada gRPC ao sidecar é síncrona (stub grpc) —
corre-se em thread para não bloquear o event loop.
"""
from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlmodel import Session

from app.database import get_session
from app.premium import analise as service
from app.premium.client import PremiumClient, get_premium_client
from app.premium.dependencies import require_feature
from app.premium.schemas import AnaliseIASchema
from app.shared.dependencies import CurrentUserDep, get_empresa_ativa

router = APIRouter(tags=["Análise IA"])


@router.post(
    "/controlos/{controlo_empresa_id}/analisar-gaps",
    response_model=AnaliseIASchema,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Solicitar análise IA de um controlo (assíncrona)",
    dependencies=[Depends(require_feature("ai_assistant"))],
)
async def solicitar_analise(
    controlo_empresa_id: uuid.UUID,
    utilizador: CurrentUserDep,
    request: Request,
    db: Session = Depends(get_session),
    premium: PremiumClient = Depends(get_premium_client),
):
    """Submete o controlo (contexto + evidências seladas) ao sidecar e devolve o job."""
    empresa = get_empresa_ativa(db, utilizador)
    return await asyncio.to_thread(
        service.solicitar_analise,
        db,
        controlo_empresa_id,
        empresa,
        utilizador,
        premium,
        request,
    )


@router.get(
    "/controlos/{controlo_empresa_id}/analise-ia",
    response_model=AnaliseIASchema | None,
    summary="Estado/resultado da análise IA de um controlo (polling)",
    dependencies=[Depends(require_feature("ai_assistant"))],
)
async def get_analise(
    controlo_empresa_id: uuid.UUID,
    utilizador: CurrentUserDep,
    request: Request,
    db: Session = Depends(get_session),
    premium: PremiumClient = Depends(get_premium_client),
):
    """Devolve o job mais recente do controlo (o frontend faz polling deste endpoint)."""
    empresa = get_empresa_ativa(db, utilizador)
    return await asyncio.to_thread(
        service.get_analise_por_controlo,
        db,
        controlo_empresa_id,
        empresa,
        utilizador,
        premium,
        request,
    )
