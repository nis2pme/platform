"""
Router premium (open-core) — superfície fina, gated por `require_feature`.
A lógica premium real vive do outro lado do contrato gRPC, no sidecar privado.
"""
from fastapi import APIRouter, Depends

from app.premium.client import PremiumClient, get_premium_client
from app.premium.dependencies import require_feature
from app.shared.dependencies import get_current_user

router = APIRouter(prefix="/premium", tags=["Premium"])


@router.get("/status", summary="Estado do subsistema premium")
def premium_status(
    utilizador=Depends(get_current_user),
    premium: PremiumClient = Depends(get_premium_client),
):
    """Indica se o premium está ligado (há sidecar configurado). Requer sessão."""
    return {"premium_enabled": premium.enabled}


@router.get(
    "/_demo",
    include_in_schema=False,
    summary="Demonstração do gate premium (placeholder)",
    dependencies=[Depends(require_feature("demo"))],
)
def premium_demo():
    """Endpoint de demonstração. Com premium desligado, o gate devolve 402."""
    return {"ok": True}
