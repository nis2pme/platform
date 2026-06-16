"""
Router do módulo de autenticação.
Endpoints finos — toda a lógica fica em service.py.
Rate limiting via slowapi: 5 tentativas/minuto por IP no login.
"""
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from slowapi import Limiter
from sqlmodel import Session

from app.auth import cookies, schemas, service
from app.auth.models import RoleUtilizador, Utilizador
from app.config import get_settings
from app.database import get_session
from app.shared.dependencies import CurrentUserDep, require_role
from app.shared.utils import obter_ip_cliente

settings = get_settings()

router = APIRouter(prefix="/auth", tags=["Autenticação"])

# Rate limiter partilhado com o main.py — chaveado pelo IP real do cliente
# (X-Real-IP do Nginx), não pelo IP interno do proxy (CWE-770).
_limiter = Limiter(key_func=obter_ip_cliente)

# Cookie de refresh — toda a lógica (Secure por-pedido, prefixo, path) em app.auth.cookies
def _set_refresh_cookie(response: Response, request: Request, token: str) -> None:
    """Define o cookie httpOnly com o refresh token (Secure conforme o pedido)."""
    cookies.definir_cookie_refresh(response, request, token)


def _clear_refresh_cookie(response: Response) -> None:
    """Remove o cookie de refresh token (ambas as variantes)."""
    cookies.limpar_cookie_refresh(response)


def _get_refresh_token_from_cookie(request: Request) -> str:
    """Extrai o refresh token do cookie. Lança 401 se ausente."""
    token = cookies.obter_token_refresh(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão não encontrada. Por favor, faça login novamente.",
        )
    return token


def _utilizador_info(db: Session, utilizador: Utilizador) -> schemas.UtilizadorInfoSchema:
    """Constrói UtilizadorInfoSchema incluindo o locale_preferido da empresa."""
    from app.empresas.models import Empresa
    empresa = db.get(Empresa, utilizador.empresa_id)
    info = schemas.UtilizadorInfoSchema.model_validate(utilizador)
    info.empresa_locale_preferido = empresa.locale_preferido if empresa else "pt"
    return info


# ---------------------------------------------------------------------------
# POST /auth/register — cria Empresa + admin (apenas modo saas)
# ---------------------------------------------------------------------------

@router.post(
    "/register",
    response_model=schemas.TokenResponseSchema,
    status_code=status.HTTP_201_CREATED,
    summary="Registar nova empresa",
)
@_limiter.limit("1/minute")
async def registar_empresa(
    dados: schemas.RegistarEmpresaSchema,
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
):
    """
    Cria uma nova empresa e o seu primeiro utilizador administrador.
    Apenas disponível quando DEPLOYMENT_MODE=saas.
    Requer consentimento explícito dos termos de serviço.
    """
    empresa, admin = service.registar_empresa_e_admin(db, dados, request)
    access_token = service.criar_access_token(admin)
    refresh_token = service.criar_refresh_token(db, admin, request)
    db.commit()  # garante que o refresh token está persistido antes da resposta

    _set_refresh_cookie(response, request, refresh_token)
    return schemas.TokenResponseSchema(
        access_token=access_token,
        utilizador=_utilizador_info(db, admin),
    )


# ---------------------------------------------------------------------------
# POST /auth/login — passo 1: email + password
# ---------------------------------------------------------------------------

@router.post(
    "/login",
    response_model=schemas.LoginResponseSchema,
    summary="Login (passo 1: credenciais)",
)
@_limiter.limit("5/minute")
async def login(
    dados: schemas.LoginSchema,
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
):
    """
    Valida email e password. Pode exigir verificação 2FA.

    Respostas possíveis:
    - Acesso completo: inclui access_token e utilizador.
    - 2FA necessário: inclui temp_token e requires_2fa=True.
    - 2FA por configurar: inclui temp_token e requires_2fa_setup=True.
    """
    resultado = service.login_passo1(db, dados.email, dados.password, request)

    if resultado["tipo"] == "acesso_completo":
        utilizador = resultado["utilizador"]
        access_token = service.criar_access_token(utilizador)
        refresh_token = service.criar_refresh_token(db, utilizador, request)
        db.commit()  # garante que o refresh token está persistido antes da resposta
        _set_refresh_cookie(response, request, refresh_token)
        return schemas.LoginResponseSchema(
            access_token=access_token,
            utilizador=_utilizador_info(db, utilizador),
        )

    if resultado["tipo"] == "2fa_necessario":
        return schemas.LoginResponseSchema(
            requires_2fa=True,
            temp_token=resultado["temp_token"],
        )

    if resultado["tipo"] == "password_temporaria":
        return schemas.LoginResponseSchema(
            requires_password_change=True,
            temp_token=resultado["temp_token"],
        )

    # 2fa_configurar
    return schemas.LoginResponseSchema(
        requires_2fa_setup=True,
        temp_token=resultado["temp_token"],
    )


# ---------------------------------------------------------------------------
# POST /auth/login/setup-2fa/iniciar — gera segredo TOTP (durante login)
# ---------------------------------------------------------------------------

@router.post(
    "/login/setup-2fa/iniciar",
    response_model=schemas.Setup2FAResponseSchema,
    summary="Iniciar setup 2FA durante o login",
)
@_limiter.limit("5/minute")
async def iniciar_setup_2fa_login(
    request: Request,
    db: Session = Depends(get_session),
    authorization: str = Header(default=None, alias="Authorization"),
):
    """
    Gera segredo TOTP e QR code para configuração 2FA durante o fluxo de login.
    Requer temp_token (tipo 2fa_setup_required) no header Authorization: Bearer <token>.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token temporário obrigatório.",
        )
    temp_token = authorization[7:]
    payload = service._decodificar_temp_token(temp_token, "2fa_setup_required")
    utilizador_id = uuid.UUID(payload["user_id"])
    utilizador = db.get(Utilizador, utilizador_id)
    if not utilizador or not utilizador.ativo:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Utilizador não encontrado.",
        )
    totp_uri, backup_codes = service.setup_2fa(db, utilizador, request)
    return schemas.Setup2FAResponseSchema(totp_uri=totp_uri, backup_codes=backup_codes)


# ---------------------------------------------------------------------------
# POST /auth/login/setup-2fa/confirmar — ativa 2FA e emite tokens (durante login)
# ---------------------------------------------------------------------------

@router.post(
    "/login/setup-2fa/confirmar",
    response_model=schemas.TokenResponseSchema,
    summary="Confirmar setup 2FA e concluir login",
)
@_limiter.limit("5/minute")
async def confirmar_setup_2fa_login(
    dados: schemas.Ativar2FASchema,
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
    authorization: str = Header(default=None, alias="Authorization"),
):
    """
    Confirma o código TOTP, ativa 2FA e devolve access token + refresh cookie.
    Conclui o fluxo de login para utilizadores que tinham 2FA por configurar.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token temporário obrigatório.",
        )
    temp_token = authorization[7:]
    payload = service._decodificar_temp_token(temp_token, "2fa_setup_required")
    utilizador_id = uuid.UUID(payload["user_id"])
    utilizador = db.get(Utilizador, utilizador_id)
    if not utilizador or not utilizador.ativo:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Utilizador não encontrado.",
        )
    service.ativar_2fa(db, utilizador, dados.codigo_totp, request)
    access_token = service.criar_access_token(utilizador)
    refresh_token = service.criar_refresh_token(db, utilizador, request)
    db.commit()  # garante que o refresh token está persistido antes da resposta
    _set_refresh_cookie(response, request, refresh_token)
    return schemas.TokenResponseSchema(
        access_token=access_token,
        utilizador=_utilizador_info(db, utilizador),
    )


# ---------------------------------------------------------------------------
# POST /auth/login/verificar-2fa — passo 2: TOTP ou backup code
# ---------------------------------------------------------------------------

@router.post(
    "/login/verificar-2fa",
    response_model=schemas.TokenResponseSchema,
    summary="Login (passo 2: verificação 2FA)",
)
@_limiter.limit("5/minute")
async def verificar_2fa(
    dados: schemas.Verificar2FASchema,
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
):
    """Verifica o código TOTP ou backup code após passo 1 do login."""
    utilizador = service.login_passo2(db, dados.temp_token, dados.codigo, request)
    access_token = service.criar_access_token(utilizador)
    refresh_token = service.criar_refresh_token(db, utilizador, request)
    db.commit()  # garante que o refresh token está persistido antes da resposta
    _set_refresh_cookie(response, request, refresh_token)
    return schemas.TokenResponseSchema(
        access_token=access_token,
        utilizador=_utilizador_info(db, utilizador),
    )


@router.post(
    "/login/alterar-password-temporaria",
    response_model=schemas.LoginResponseSchema,
    summary="Concluir login com password temporária",
)
@_limiter.limit("5/minute")
async def alterar_password_temporaria_login(
    dados: schemas.AlterarPasswordTemporariaSchema,
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
):
    """
    Altera a password temporária e aplica a política de MFA universal do passo 1:
    - Com TOTP ativo → passo 2 (verificar código)
    - Sem TOTP → forçar setup de 2FA (qualquer role)
    """
    utilizador = service.alterar_password_temporaria_login(
        db,
        dados.temp_token,
        dados.nova_password,
        dados.confirmar_nova_password,
        request,
    )

    # Verificar empresa suspensa
    from app.empresas.models import Empresa as EmpresaModel
    empresa = db.get(EmpresaModel, utilizador.empresa_id)
    if empresa and empresa.suspenso:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A sua conta está temporariamente suspensa. Contacte o suporte.",
        )

    # MFA universal (igual ao login_passo1): todos os roles necessitam de 2FA.
    # Com TOTP ativo → verificar código; sem TOTP → forçar configuração.
    # A alteração da password é persistida pelo commit automático de get_session.
    if utilizador.totp_ativo:
        temp_token = service.criar_temp_token(utilizador, "2fa_pending")
        return schemas.LoginResponseSchema(
            requires_2fa=True,
            temp_token=temp_token,
        )

    temp_token = service.criar_temp_token(utilizador, "2fa_setup_required")
    return schemas.LoginResponseSchema(
        requires_2fa_setup=True,
        temp_token=temp_token,
    )


# ---------------------------------------------------------------------------
# POST /auth/refresh — renova access token via cookie
# ---------------------------------------------------------------------------

@router.post(
    "/refresh",
    response_model=schemas.RefreshResponseSchema,
    summary="Renovar access token",
)
async def renovar_token(
    request: Request,
    response: Response,
    db: Session = Depends(get_session),
):
    """
    Usa o refresh token do cookie httpOnly para emitir um novo access token.
    O refresh token anterior é revogado e um novo é emitido (rotação — CWE-384).
    Não requer Authorization header.
    """
    refresh_token = _get_refresh_token_from_cookie(request)
    access_token, novo_refresh_token, utilizador = service.renovar_access_token(
        db, refresh_token, request
    )
    db.commit()  # garante que o novo refresh token está persistido antes da resposta
    _set_refresh_cookie(response, request, novo_refresh_token)
    return schemas.RefreshResponseSchema(
        access_token=access_token,
        utilizador=_utilizador_info(db, utilizador),
    )


# ---------------------------------------------------------------------------
# POST /auth/logout — revoga refresh token
# ---------------------------------------------------------------------------

@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Logout",
)
async def logout(
    request: Request,
    response: Response,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """Revoga o refresh token e limpa o cookie. Requer access token válido."""
    try:
        refresh_token = _get_refresh_token_from_cookie(request)
        service.logout(db, refresh_token, utilizador, request)
    except HTTPException:
        pass  # mesmo sem cookie, fazemos logout "best effort"
    _clear_refresh_cookie(response)


# ---------------------------------------------------------------------------
# GET /auth/me — utilizador atual
# ---------------------------------------------------------------------------

@router.get(
    "/me",
    response_model=schemas.MeResponseSchema,
    summary="Informação do utilizador atual",
)
async def me(utilizador: CurrentUserDep, db: Session = Depends(get_session)):
    """Devolve os dados do utilizador autenticado."""
    from app.empresas.models import Empresa
    empresa = db.get(Empresa, utilizador.empresa_id)
    info = schemas.MeResponseSchema.model_validate(utilizador)
    info.empresa_locale_preferido = empresa.locale_preferido if empresa else "pt"
    return info


# ---------------------------------------------------------------------------
# POST /auth/password-reset/solicitar
# ---------------------------------------------------------------------------

@router.post(
    "/password-reset/solicitar",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Solicitar reset de password",
)
@_limiter.limit("3/minute")
async def solicitar_reset(
    dados: schemas.ResetPasswordSolicitarSchema,
    request: Request,
    db: Session = Depends(get_session),
):
    """
    Envia email de recuperação de password.
    Resposta sempre 202 independente de o email existir (evita user enumeration).

    Excepção: se o servidor não tiver nenhum serviço de email configurado
    (EMAIL_ENABLED=false), responde 503 — esta é uma condição global da
    instalação, não revela nada sobre utilizadores específicos.
    """
    if not settings.EMAIL_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="email_nao_configurado",
        )

    await service.solicitar_reset_password(db, dados.email, request)
    return {"mensagem": "Se o email estiver registado, receberá instruções em breve."}


# ---------------------------------------------------------------------------
# POST /auth/password-reset/confirmar
# ---------------------------------------------------------------------------

@router.post(
    "/password-reset/confirmar",
    status_code=status.HTTP_200_OK,
    summary="Confirmar reset de password",
)
async def confirmar_reset(
    dados: schemas.ResetPasswordConfirmarSchema,
    request: Request,
    db: Session = Depends(get_session),
):
    """Valida o token de reset e atualiza a password. Invalida todas as sessões ativas."""
    service.confirmar_reset_password(db, dados.token, dados.nova_password, request)
    return {"mensagem": "Password atualizada com sucesso. Por favor, faça login novamente."}


# ---------------------------------------------------------------------------
# POST /auth/2fa/configurar — gera segredo TOTP e backup codes
# ---------------------------------------------------------------------------

@router.post(
    "/2fa/configurar",
    response_model=schemas.Setup2FAResponseSchema,
    summary="Configurar autenticação de dois fatores",
)
async def configurar_2fa(
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """
    Gera novo segredo TOTP e 10 backup codes.
    Os backup codes são mostrados APENAS UMA VEZ — guardar em lugar seguro.
    O 2FA não fica ativo até chamar POST /2fa/ativar.
    """
    totp_uri, backup_codes = service.setup_2fa(db, utilizador, request)
    return schemas.Setup2FAResponseSchema(
        totp_uri=totp_uri,
        backup_codes=backup_codes,
    )


# ---------------------------------------------------------------------------
# POST /auth/2fa/ativar — confirma ativação do 2FA
# ---------------------------------------------------------------------------

@router.post(
    "/2fa/ativar",
    status_code=status.HTTP_200_OK,
    summary="Ativar autenticação de dois fatores",
)
async def ativar_2fa(
    dados: schemas.Ativar2FASchema,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """
    Confirma a ativação do 2FA com um código TOTP válido da app autenticadora.
    Após este passo, o 2FA fica ativo e será exigido em futuros logins.
    """
    service.ativar_2fa(db, utilizador, dados.codigo_totp, request)
    return {"mensagem": "Autenticação de dois fatores ativada com sucesso."}


# ---------------------------------------------------------------------------
# DELETE /auth/2fa — desativa 2FA (apenas admin)
# ---------------------------------------------------------------------------

@router.delete(
    "/2fa",
    status_code=status.HTTP_200_OK,
    summary="Desativar autenticação de dois fatores",
    dependencies=[Depends(require_role(RoleUtilizador.ADMIN))],
)
async def desativar_2fa(
    dados: schemas.Desativar2FASchema,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """
    Desativa 2FA após confirmação com password atual.
    Apenas disponível para administradores.
    """
    service.desativar_2fa(db, utilizador, dados.password_atual, request)
    return {"mensagem": "Autenticação de dois fatores desativada."}
