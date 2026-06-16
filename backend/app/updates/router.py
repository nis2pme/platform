"""
Router de verificação de atualizações (on-prem).
"""
from fastapi import APIRouter, Depends

from app.auth.models import RoleUtilizador, Utilizador
from app.config import get_settings
from app.setup.env_file import atualizar_env
from app.shared.dependencies import get_current_user, require_role
from app.updates import service
from app.updates.schemas import (
    UpdateConfigRespostaSchema,
    UpdateConfigSchema,
    UpdateStatusSchema,
)

router = APIRouter(tags=["Atualizações"])


@router.get(
    "/updates/status",
    response_model=UpdateStatusSchema,
    summary="Estado da verificação de atualizações",
)
def get_update_status(utilizador: Utilizador = Depends(get_current_user)):
    """Versão atual, última conhecida e se há atualização disponível. Requer sessão."""
    return UpdateStatusSchema(**service.obter_estado())


@router.post(
    "/updates/config",
    response_model=UpdateConfigRespostaSchema,
    summary="Ligar/desligar a verificação de atualizações",
)
def set_update_config(
    dados: UpdateConfigSchema,
    utilizador: Utilizador = Depends(require_role(RoleUtilizador.ADMIN)),
):
    """Persiste VERIFY_UPDATES no .env e aplica imediatamente. Requer admin."""
    atualizar_env({"VERIFY_UPDATES": "true" if dados.verificar else "false"})
    return UpdateConfigRespostaSchema(verificar_ativo=get_settings().VERIFY_UPDATES)
