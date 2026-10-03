"""
Router de verificação de atualizações (on-prem).
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth.models import Utilizador
from app.config import get_settings
from app.setup.env_file import atualizar_env
from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import SessionDep, get_current_user
from app.updates import service
from app.updates.schemas import (
    UpdateConfigRespostaSchema,
    UpdateConfigSchema,
    UpdateStatusSchema,
)

# A versão instalada e a existência de atualização mostram-se na aba
# Sistema; o endpoint segue a mesma célula que esse ecrã.
_SistemaVerDep = Depends(require_capability("sistema", ClasseAcao.VER))

router = APIRouter(tags=["Atualizações"])


@router.get(
    "/updates/status",
    response_model=UpdateStatusSchema,
    summary="Estado da verificação de atualizações",
    dependencies=[_SistemaVerDep],
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
    request: Request,
    db: SessionDep,
    utilizador: Utilizador = Depends(require_capability("sistema", ClasseAcao.OPERAR)),
):
    """Persiste VERIFY_UPDATES no .env e aplica imediatamente. Requer admin.
    Fica na trilha: desligar a verificação cala o aviso das versões de segurança."""
    # O `.env` é da instância. Em SaaS a instância serve todos os tenants, e a
    # verificação de atualizações nem sequer é lida — um admin de um tenant não
    # tem nada a escrever aí.
    if get_settings().DEPLOYMENT_MODE != "onprem":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"codigo": "so_onprem"},
        )
    anterior = get_settings().VERIFY_UPDATES
    atualizar_env({"VERIFY_UPDATES": "true" if dados.verificar else "false"})
    registar_acao(
        db, acao=Acao.SISTEMA_ATUALIZACOES_CONFIGURADAS, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, dados_anteriores={"verificar": anterior},
        dados_novos={"verificar": dados.verificar}, request=request,
    )
    db.commit()
    return UpdateConfigRespostaSchema(verificar_ativo=get_settings().VERIFY_UPDATES)
