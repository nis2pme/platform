"""
require_feature(...) — dependência FastAPI que faz o *gate* de funcionalidades premium.

Espelha o `require_role(...)` do RBAC (app/shared/dependencies.py), mas em vez de
um papel verifica um ENTITLEMENT do tenant junto do sidecar premium (via
PremiumClient). O tenant é o `empresa_id` do utilizador autenticado.
"""
from typing import Annotated

from fastapi import Depends, HTTPException, status

from app.premium.client import PremiumClient, get_premium_client
from app.shared.dependencies import get_current_user

# Alias tipado para injeção limpa nos routers.
PremiumClientDep = Annotated[PremiumClient, Depends(get_premium_client)]


def require_feature(*features: str):
    """
    Factory de dependência: exige que o tenant do utilizador autenticado tenha
    TODAS as features premium indicadas. Caso contrário → 402 Payment Required.

    Uso nos routers:
        @router.get("/x", dependencies=[Depends(require_feature("ai_assistant"))])
        async def rota(...):

    Ou como parâmetro tipado:
        async def rota(utilizador = Depends(require_feature("ai_assistant"))):
    """
    def verificador(
        utilizador=Depends(get_current_user),
        premium: PremiumClient = Depends(get_premium_client),
    ):
        tenant_id = str(utilizador.empresa_id)
        for feature in features:
            if not premium.has_feature(tenant_id, feature):
                # Código estável (não texto cravado) → o frontend traduz por i18n.
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail={"codigo": "premium_inativo", "feature": feature},
                )
        return utilizador

    return verificador
