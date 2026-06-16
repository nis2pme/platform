"""
Router do módulo de setup inicial (on-prem).
Dois endpoints públicos (sem autenticação).
"""
import logging

from fastapi import APIRouter, Depends, Request, Response, status
from sqlmodel import Session

from app.config import get_settings
from app.database import get_session
from app.setup import service, session
from app.setup.schemas import (
    ModoHttps,
    SetupConfigurarSchema,
    SetupEmailRespostaSchema,
    SetupEmailSchema,
    SetupHttpsRespostaSchema,
    SetupHttpsSchema,
    SetupIniciarSchema,
    SetupRespostaSchema,
    SetupStatusSchema,
)
from app.shared.dependencies import require_role
from app.auth.models import RoleUtilizador, Utilizador

router = APIRouter(tags=["Setup"])
settings = get_settings()
logger = logging.getLogger(__name__)


@router.get(
    "/setup/status",
    response_model=SetupStatusSchema,
    summary="Estado de configuração da instalação",
)
def get_setup_status(db: Session = Depends(get_session)):
    """
    Endpoint público — frontend chama isto na primeira visita para saber se
    deve mostrar o wizard de configuração ou o ecrã de login normal.
    """
    return SetupStatusSchema(
        configurado=service.verificar_setup_completo(db),
        deployment_mode=settings.DEPLOYMENT_MODE,
    )


@router.get(
    "/setup/tls",
    summary="Estado TLS atual (on-prem)",
)
def get_tls_status():
    """
    Devolve o modo TLS atual e metadados do certificado instalado (se houver),
    para o wizard se adaptar (manter / substituir / atrás de proxy) sem forçar
    reenvio de um certificado já ativo. Público (lido pré-setup); só on-prem.
    Os dados do certificado (emissor/validade) não são sensíveis — são
    apresentados no próprio handshake TLS.
    """
    from fastapi import HTTPException
    from app.setup.https_service import inspecionar_certificado_ativo

    if settings.DEPLOYMENT_MODE != "onprem":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Estado TLS só disponível em modo on-prem.",
        )
    return {"modo": settings.TLS_MODE, "cert": inspecionar_certificado_ativo()}


@router.post(
    "/setup/iniciar",
    response_model=SetupIniciarSchema,
    summary="Reclamar a sessão de setup (on-prem)",
)
def iniciar_setup(
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
):
    """
    Chamado pelo wizard ao abrir. O primeiro cliente reclama a sessão e recebe um
    cookie httpOnly; clientes seguintes recebem estado='ocupado'. Público (pré-setup).
    - 403 se DEPLOYMENT_MODE != onprem.
    - 410 se a instalação já foi configurada.
    """
    from fastapi import HTTPException

    if settings.DEPLOYMENT_MODE != "onprem":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Este endpoint só está disponível em modo on-prem.",
        )
    if service.verificar_setup_completo(db):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Esta instalação já foi configurada. Use o login normal.",
        )

    token, ocupado = session.reclamar(session.ler_cookie(request))
    if ocupado:
        return SetupIniciarSchema(estado="ocupado")
    session.definir_cookie(response, request, token)
    return SetupIniciarSchema(estado="disponivel")


@router.post(
    "/setup/configurar",
    response_model=SetupRespostaSchema,
    status_code=status.HTTP_201_CREATED,
    summary="Configurar instalação on-prem (primeira execução)",
)
def configurar_instalacao(
    dados: SetupConfigurarSchema,
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
):
    """
    Cria a primeira empresa e o primeiro utilizador admin.
    - Retorna 403 se DEPLOYMENT_MODE != onprem.
    - Retorna 410 se o sistema já foi configurado.
    - Em sucesso, devolve access_token + seta refresh_token em cookie httpOnly.
    - Se SMTP não configurado, inclui 'aviso_smtp' na resposta.
    """
    # Sessão de setup (anti-corrida ao primeiro admin): exigir o cookie da sessão
    # reclamada via POST /setup/iniciar. Só relevante em on-prem.
    if settings.DEPLOYMENT_MODE == "onprem" and not session.validar(session.ler_cookie(request)):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sessão de setup inválida ou expirada. Recarregue a página para reiniciar o assistente.",
        )

    resultado = service.executar_setup(db, dados, request)
    session.limpar()
    session.limpar_cookie(response)

    # Preferência de verificação de atualizações escolhida no wizard
    from app.setup.env_file import atualizar_env
    atualizar_env({"VERIFY_UPDATES": "true" if dados.verificar_atualizacoes else "false"})

    # Nota: NÃO se emite sessão aqui. O setup devolve um temp_token e o wizard
    # conclui com o enrolamento de 2FA obrigatório (/auth/login/setup-2fa/confirmar),
    # que é quem emite o access token + refresh cookie.
    return SetupRespostaSchema(**resultado)


@router.post(
    "/setup/configurar-https",
    response_model=SetupHttpsRespostaSchema,
    summary="Configurar HTTPS no nginx (on-prem)",
)
def configurar_https(
    dados: SetupHttpsSchema,
    db: Session = Depends(get_session),
    utilizador: Utilizador = Depends(require_role(RoleUtilizador.ADMIN)),
):
    """
    Configura o nginx do container frontend com o modo HTTPS escolhido:
    - none: HTTP simples
    - self_signed: gera certificado autoassinado
    - custom: usa certificado e chave PEM fornecidos pelo utilizador

    Requer role admin (altera infraestrutura TLS). O sistema deve estar configurado.
    Escreve a config no volume partilhado e sinaliza o nginx para recarregar.
    """
    from app.setup.https_service import configurar_nginx_https

    if settings.DEPLOYMENT_MODE != "onprem":
        from fastapi import HTTPException
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Configuração HTTPS só disponível em modo on-prem.",
        )

    resultado = configurar_nginx_https(
        modo=dados.modo.value,
        cert_pem=dados.cert_pem,
        key_pem=dados.key_pem,
        app_url=get_settings().APP_URL,
    )

    # Manter o APP_URL coerente com o esquema agora servido — afeta os links de
    # reset por email e a deteção de domínio para o HSTS (#5).
    from urllib.parse import urlparse, urlunparse
    from app.setup.env_file import atualizar_env

    parsed = urlparse(get_settings().APP_URL)
    novo_scheme = "http" if dados.modo == ModoHttps.NENHUM else "https"
    if parsed.netloc and parsed.scheme != novo_scheme:
        atualizar_env({"APP_URL": urlunparse((novo_scheme, parsed.netloc, "", "", "", ""))})

    return SetupHttpsRespostaSchema(**resultado)


@router.post(
    "/setup/configurar-email",
    response_model=SetupEmailRespostaSchema,
    summary="Configurar SMTP (on-prem)",
)
def configurar_email(
    dados: SetupEmailSchema,
    utilizador: Utilizador = Depends(require_role(RoleUtilizador.ADMIN)),
):
    """
    Configura (ou desativa) o envio de email via SMTP, persistindo as
    variáveis SMTP_*/EMAIL_* no .env e aplicando-as imediatamente.

    Requer role admin (controla o servidor de saída de email). Apenas em modo on-prem.
    """
    from app.setup import email_service

    if settings.DEPLOYMENT_MODE != "onprem":
        from fastapi import HTTPException
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Configuração de email só disponível em modo on-prem.",
        )

    resultado = email_service.configurar_email_smtp(dados)
    return SetupEmailRespostaSchema(**resultado)
