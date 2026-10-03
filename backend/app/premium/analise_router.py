"""
Router do Assistente IA (premium) — endpoints finos, gated por
`require_feature("ai_assistant")` (402 se o tenant não tem o módulo).

A lógica vive em analise.py.
Rotas `def`: a base e o gRPC são síncronos e correm no threadpool, fora do
event loop.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status
from sqlmodel import Session

from app.database import get_session
from app.premium import analise as service
from app.premium.client import PremiumClient, get_premium_client
from app.premium.dependencies import require_feature
from app.premium.schemas import AnaliseIASchema
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, get_empresa_ativa

# Pedir uma análise é trabalho SOBRE o controlo e gasta quota do tenant, por
# isso a leitura do módulo não chega. Sobre QUE controlo decide-se no serviço,
# com o registo em mão.
_AnaliseDep = Depends(require_capability("controlos", ClasseAcao.OPERAR))

router = APIRouter(tags=["Análise IA"])


@router.post(
    "/controlos/{controlo_empresa_id}/analisar-gaps",
    response_model=AnaliseIASchema,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Solicitar análise IA de um controlo (assíncrona)",
    dependencies=[_AnaliseDep, Depends(require_feature("ai_assistant"))],
)
def solicitar_analise(
    controlo_empresa_id: uuid.UUID,
    utilizador: CurrentUserDep,
    request: Request,
    db: Session = Depends(get_session, scope="function"),
    premium: PremiumClient = Depends(get_premium_client),
):
    """Submete o controlo (contexto + evidências seladas) ao sidecar e devolve o job."""
    empresa = get_empresa_ativa(db, utilizador)
    return service.solicitar_analise(
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
    dependencies=[_AnaliseDep, Depends(require_feature("ai_assistant"))],
)
def get_analise(
    controlo_empresa_id: uuid.UUID,
    utilizador: CurrentUserDep,
    request: Request,
    db: Session = Depends(get_session, scope="function"),
    premium: PremiumClient = Depends(get_premium_client),
):
    """Devolve o job mais recente do controlo (o frontend faz polling deste endpoint)."""
    empresa = get_empresa_ativa(db, utilizador)
    return service.get_analise_por_controlo(
        db,
        controlo_empresa_id,
        empresa,
        utilizador,
        premium,
        request,
    )
