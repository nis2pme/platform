"""
Router interno de gestão privilegiada de tenants (máquina-a-máquina).

Montado **só** em DEPLOYMENT_MODE=saas e quando `CORE_SUSPEND_TOKEN` está definido
(ver app.main). Fora do OpenAPI, alcançável apenas pela rede interna. Autenticado
por token partilhado (`hmac.compare_digest`); sem ele responde 404 — não revela a
existência do endpoint. O `X-Actor` (informativo) identifica quem ordenou, para
auditoria do core.

Suspender/reativar (`CORE_SUSPEND_TOKEN`).
"""
import hmac
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlmodel import Session

from app.config import get_settings
from app.database import get_session
from app.internal_admin import service

settings = get_settings()

router = APIRouter(prefix="/internal/admin", tags=["Interno"], include_in_schema=False)


def _exigir_token(x_internal_token: str = Header(default=None, alias="X-Internal-Token")) -> None:
    """Exige o token de suspensão; 404 (não 401) em falha — não revela o endpoint."""
    esperado = settings.CORE_SUSPEND_TOKEN
    if (
        not esperado
        or not x_internal_token
        or not hmac.compare_digest(x_internal_token, esperado)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


@router.post("/empresas/{empresa_id}/suspender", dependencies=[Depends(_exigir_token)])
def suspender_empresa(
    empresa_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_session),
    x_actor: str = Header(default="desconhecido", alias="X-Actor"),
) -> dict:
    empresa = service.definir_suspensao(db, empresa_id, True, x_actor, request)
    return {"empresa_id": str(empresa.id), "suspenso": empresa.suspenso}


@router.post("/empresas/{empresa_id}/reativar", dependencies=[Depends(_exigir_token)])
def reativar_empresa(
    empresa_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_session),
    x_actor: str = Header(default="desconhecido", alias="X-Actor"),
) -> dict:
    empresa = service.definir_suspensao(db, empresa_id, False, x_actor, request)
    return {"empresa_id": str(empresa.id), "suspenso": empresa.suspenso}
