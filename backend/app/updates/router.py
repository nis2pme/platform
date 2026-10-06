"""
Router de atualizações (on-prem): verificação, pedido de atualização e progresso.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from slowapi import Limiter

from app.auth.models import Utilizador
from app.config import get_settings
from app.setup.env_file import atualizar_env
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import SessionDep, get_current_user
from app.shared.utils import obter_chave_limite
from app.updates import service
from app.updates.schemas import (
    UpdateAplicarRespostaSchema,
    UpdateAplicarSchema,
    UpdateConfigRespostaSchema,
    UpdateConfigSchema,
    UpdateProgressoSchema,
    UpdateStatusSchema,
)

# A versão instalada e a existência de atualização mostram-se na aba
# Sistema; o endpoint segue a mesma célula que esse ecrã.
_SistemaVerDep = Depends(require_capability("sistema", ClasseAcao.VER))

router = APIRouter(tags=["Atualizações"])

# Cada pedido de atualização paga uma verificação de password (argon2).
_limiter = Limiter(key_func=obter_chave_limite)


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


@router.post(
    "/updates/aplicar",
    response_model=UpdateAplicarRespostaSchema,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Pedir a atualização da instalação para a versão anunciada",
)
@_limiter.limit("5/minute")
def aplicar_atualizacao(
    dados: UpdateAplicarSchema,
    request: Request,
    db: SessionDep,
    utilizador: Utilizador = Depends(require_capability("sistema", ClasseAcao.OPERAR)),
):
    """Deixa o pedido para o agente do anfitrião. Exige a password de quem pede:
    o que corre a seguir muda o código da instalação. O backend não toca no Docker."""
    from app.auth.service import verify_password

    if get_settings().DEPLOYMENT_MODE != "onprem":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"codigo": "so_onprem"})

    def _recusar(codigo: str, http: int) -> HTTPException:
        # Escrita seguida de erro é revertida pelo rollback da sessão: a tentativa
        # fica na trilha por uma sessão própria.
        registar_acao(
            None, acao=Acao.SISTEMA_ATUALIZACAO_PEDIDA, resultado=ResultadoAcao.FALHA,
            empresa_id=utilizador.empresa_id, utilizador_id=utilizador.id,
            dados_novos={"versao": dados.versao, "motivo": codigo}, request=request,
            force_commit=True,
        )
        return HTTPException(status_code=http, detail={"codigo": codigo})

    if not verify_password(dados.password, utilizador.password_hash):
        raise _recusar("password_incorreta", status.HTTP_400_BAD_REQUEST)

    try:
        pedido_id = service.criar_pedido(
            utilizador_id=utilizador.id, empresa_id=utilizador.empresa_id,
            versao=dados.versao, sem_backup=dados.aceito_sem_backup,
        )
    except service.PedidoRecusado as recusa:
        raise _recusar(recusa.codigo, status.HTTP_409_CONFLICT)

    registar_acao(
        db, acao=Acao.SISTEMA_ATUALIZACAO_PEDIDA, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        dados_novos={
            "versao": dados.versao, "pedido_id": pedido_id,
            "sem_backup": dados.aceito_sem_backup,
        },
        request=request,
    )
    db.commit()
    return UpdateAplicarRespostaSchema(pedido_id=pedido_id, versao=dados.versao)


def _uuid_ou_none(valor):
    try:
        return uuid.UUID(str(valor))
    except ValueError:
        return None


@router.get(
    "/updates/progresso",
    response_model=UpdateProgressoSchema,
    summary="Progresso da atualização pedida pelo interface",
    dependencies=[_SistemaVerDep],
)
def get_progresso_atualizacao(
    db: SessionDep,
    utilizador: Utilizador = Depends(get_current_user),
):
    """O que o agente do anfitrião deixou. O desfecho (concluída, falhada,
    revertida) entra na trilha na primeira leitura em que aparece."""
    desfecho = service.resultado_por_registar()
    if desfecho:
        progresso, pedido = desfecho["progresso"], desfecho["pedido"]
        concluida = progresso["estado"] == "concluido"
        registar_acao(
            db,
            acao=Acao.SISTEMA_ATUALIZACAO_CONCLUIDA if concluida else Acao.SISTEMA_ATUALIZACAO_FALHADA,
            resultado=ResultadoAcao.SUCESSO if concluida else ResultadoAcao.FALHA,
            empresa_id=_uuid_ou_none(pedido.get("empresa_id")) or utilizador.empresa_id,
            utilizador_id=_uuid_ou_none(pedido.get("utilizador_id")),
            dados_novos={
                "pedido_id": progresso["pedido_id"], "versao_origem": progresso["versao_origem"],
                "versao_alvo": progresso["versao_alvo"], "estado": progresso["estado"],
                "codigo": progresso["codigo"], "backup": progresso["backup"],
            },
        )
        db.commit()
    return UpdateProgressoSchema(**service.ler_progresso())
