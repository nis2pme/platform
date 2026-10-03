"""
Router do módulo de autenticação.
Endpoints finos — toda a lógica fica em service.py.
Rate limiting via slowapi: 5 tentativas/minuto por IP no login.
"""
import hmac
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response, status
from slowapi import Limiter
from sqlmodel import Session

from app.auth import cookies, dispositivo, schemas, service
from app.auth.models import RoleUtilizador, Utilizador
from app.config import get_settings
from app.database import get_session
from app.shared.dependencies import CurrentUserDep, require_role
from app.shared.politica_seguranca import password_min
from app.shared.utils import obter_chave_limite, obter_ip_cliente

settings = get_settings()

router = APIRouter(prefix="/auth", tags=["Autenticação"])

# Rate limiter das rotas de autenticação — chaveado pelo IP real do cliente
# (X-Real-IP do Nginx), não pelo IP interno do proxy (CWE-770), e em IPv6 pelo
# /64 inteiro, para rodar o endereço não servir de nada.
_limiter = Limiter(key_func=obter_chave_limite)


def _chave_grupo_limite(request: Request) -> str:
    """Chave do limite por GRUPO de endereços (/48 em IPv6, /24 em IPv4): o degrau
    acima do /64, para um /48 inteiro não escapar ao limite por rota."""
    from app.auth.limites_login import chave_grupo

    return chave_grupo(obter_ip_cliente(request))


# Limite por grupo, empilhado sobre o limite por endereço nas rotas de auth com
# argon2 (o login e o 2.º passo têm a sua própria camada, em app.auth.limites_login).
_LIMITE_GRUPO = f"{settings.LOGIN_GRUPO_POR_MINUTO}/minute"

# Cookie de refresh — toda a lógica (Secure por-pedido, prefixo, path) em app.auth.cookies
def _set_refresh_cookie(response: Response, request: Request, token: str) -> None:
    """Define o cookie httpOnly com o refresh token (Secure conforme o pedido)."""
    cookies.definir_cookie_refresh(response, request, token)


def _clear_refresh_cookie(response: Response) -> None:
    """Remove o cookie de refresh token (ambas as variantes)."""
    cookies.limpar_cookie_refresh(response)


def renovar_sessao_de_quem_pediu(
    db: Session, response: Response, request: Request, utilizador: Utilizador
) -> None:
    """Abre uma sessão nova para quem acabou de mudar a password ou o 2FA.

    A mudança termina todas as sessões da conta, incluindo a de quem a pediu:
    sem uma sessão nova, essa pessoa era posta fora na renovação seguinte (até
    30 minutos depois). O cookie tem o caminho `/api/auth` e pode ser posto por
    uma resposta de qualquer rota.
    """
    token = service.criar_refresh_token(db, utilizador, request)
    db.commit()  # a sessão nova tem de existir antes de o cookie chegar ao cliente
    _set_refresh_cookie(response, request, token)


def _get_refresh_token_from_cookie(request: Request) -> str:
    """Extrai o refresh token do cookie. Lança 401 se ausente."""
    token = cookies.obter_token_refresh(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão não encontrada. Por favor, faça login novamente.",
        )
    return token


def _registar_sessao_iniciada(
    db: Session, utilizador: Utilizador, request: Request, via: str
) -> None:
    """
    Regista que uma sessão foi concedida.

    Existe porque nem todos os caminhos de entrada passam pela verificação do
    código 2FA: quem ainda não tinha 2FA configurado conclui o login ao ativá-lo,
    e essa entrada não deixava rasto nenhum. A trilha mostrava a ativação do 2FA
    seguida de renovações de sessão, sem nunca dizer que alguém tinha entrado.
    """
    from app.shared.audit import Acao, ResultadoAcao, registar_acao

    registar_acao(
        db,
        acao=Acao.LOGIN_SUCESSO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        dados_novos={"email": utilizador.email, "via": via},
        request=request,
    )


def _utilizador_info(db: Session, utilizador: Utilizador) -> schemas.UtilizadorInfoSchema:
    """Constrói UtilizadorInfoSchema incluindo o locale_preferido da empresa
    e as capacidades efetivas do papel (login, 2FA e refresh passam por aqui)."""
    from app.empresas.models import Empresa
    from app.shared.capacidades import ambitos_de, capacidades_de
    empresa = db.get(Empresa, utilizador.empresa_id)
    info = schemas.UtilizadorInfoSchema.model_validate(utilizador)
    info.empresa_locale_preferido = empresa.locale_preferido if empresa else "pt"
    info.capacidades = capacidades_de(utilizador)
    info.ambitos = ambitos_de(utilizador)
    info.password_min = password_min(db, utilizador.empresa_id)
    return info


# ---------------------------------------------------------------------------
# POST /auth/register — cria Empresa + admin (apenas modo saas)
# ---------------------------------------------------------------------------

def _validar_fim_do_trial(fim) -> None:
    """O fim do trial vem da borda: tem de existir, ser futuro e não passar do
    teto. Quem tivesse o token da borda já não abre um trial até 2100."""
    from datetime import datetime, timedelta, timezone

    agora = datetime.now(timezone.utc)
    if fim is None:
        raise HTTPException(status_code=422, detail={"codigo": "trial_sem_fim"})
    fim = fim if fim.tzinfo else fim.replace(tzinfo=timezone.utc)
    if not agora < fim <= agora + timedelta(days=settings.SAAS_TRIAL_DIAS_MAX, hours=1):
        raise HTTPException(status_code=422, detail={"codigo": "trial_fim_invalido"})


def _exigir_token_da_borda(x_internal_token: str = Header(default=None, alias="X-Internal-Token")) -> None:
    """Em SaaS, o registo só aceita a borda de registo. Verificado como dependência
    para correr antes da validação do corpo: sem token, 404 — nunca um 422 que
    revele que a rota existe."""
    if settings.DEPLOYMENT_MODE == "saas":
        esperado = settings.SAAS_TRIAL_INTERNAL_TOKEN
        if (
            not esperado
            or not x_internal_token
            or not hmac.compare_digest(x_internal_token, esperado)
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


@router.post(
    "/register",
    response_model=schemas.RegistoCriadoSchema,
    status_code=status.HTTP_201_CREATED,
    summary="Registar nova empresa",
    dependencies=[Depends(_exigir_token_da_borda)],
)
@_limiter.limit("1/minute")
def registar_empresa(
    dados: schemas.RegistarEmpresaSchema,
    request: Request,
    db: Session = Depends(get_session, scope="function"),
):
    """
    Cria uma nova empresa e o seu primeiro utilizador administrador.
    Apenas disponível quando DEPLOYMENT_MODE=saas.
    Requer consentimento explícito dos termos de serviço.

    Em modo SaaS o registo público só é alcançável através da borda de signup,
    que se autentica com um token interno. Sem token (ou errado) responde 404 —
    não revela a existência do endpoint a quem o tente atingir diretamente.

    O registo não autentica: devolve 201 sem token nem cookie de sessão. A conta
    autentica-se a seguir pelo fluxo de login (onde se configura o 2FA). Assim a
    borda que intermedeia o registo nunca recebe a sessão da conta criada.
    """
    if settings.DEPLOYMENT_MODE == "saas":
        _validar_fim_do_trial(dados.trial_expira_em)

    empresa, admin = service.registar_empresa_e_admin(db, dados, request)
    if settings.DEPLOYMENT_MODE == "saas":
        # O plano pedido fica na empresa ANTES do commit: se o gateway falhar a
        # seguir, o tick de reconciliação sabe o que voltar a pedir.
        from app.premium.provisioning import utc_sem_fuso

        empresa.plano = "trial"
        empresa.trial_expira_em = utc_sem_fuso(dados.trial_expira_em)
        db.add(empresa)
    db.commit()

    # Provisionar o plano do tenant (SaaS) — best-effort, fora da transação do registo.
    # O core não escreve entitlements: pede ao gateway (escritor único) sobre mTLS interno.
    # Se falhar, NÃO quebra o signup — a IA fica indisponível até o tick reconciliar.
    if settings.DEPLOYMENT_MODE == "saas":
        from app.premium.provisioning import provisionar_e_registar

        # A rota é `def`: corre inteira no threadpool (hash da password, os
        # controlos da empresa nova, o provisionamento) sem parar o event loop,
        # e a sessão da base fica numa só thread.
        provisionar_e_registar(db, empresa, "trial", expira_em=empresa.trial_expira_em)

    return schemas.RegistoCriadoSchema(empresa_id=empresa.id, admin_email=admin.email)


# ---------------------------------------------------------------------------
# POST /auth/login — passo 1: email + password
# ---------------------------------------------------------------------------

@router.post(
    "/login",
    response_model=schemas.LoginResponseSchema,
    summary="Login (passo 1: credenciais)",
)
def login(
    dados: schemas.LoginSchema,
    request: Request,
    response: Response,
    db: Session = Depends(get_session, scope="function"),
):
    """
    Valida email e password. Pode exigir verificação 2FA.

    Os limites por endereço e por grupo, o cookie de dispositivo e a prova de
    trabalho vivem em `service.login_passo1` (camadas 1–3), para o cookie poder
    dispensar os limites por endereço a quem já se autenticou por completo neste
    dispositivo. Por isso esta rota não leva o decorador do slowapi.

    Respostas possíveis:
    - Acesso completo: inclui access_token e utilizador.
    - 2FA necessário: inclui temp_token e requires_2fa=True.
    - 2FA por configurar: inclui temp_token e requires_2fa_setup=True.
    """
    resultado = service.login_passo1(
        db, dados.email, dados.password, request,
        cookie_dispositivo=dispositivo.obter(request),
    )

    if resultado["tipo"] == "acesso_completo":
        utilizador = resultado["utilizador"]
        # Hoje o 2FA é universal e este ramo não é alcançado. Fica registado à
        # mesma: se alguma vez voltar a haver entrada direta, não pode ser a
        # única forma de entrar que não deixa rasto.
        _registar_sessao_iniciada(db, utilizador, request, "credenciais")
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
            password_min=resultado["password_min"],
        )

    # 2fa_configurar
    return schemas.LoginResponseSchema(
        requires_2fa_setup=True,
        temp_token=resultado["temp_token"],
    )


# ---------------------------------------------------------------------------
# POST /auth/login/setup-2fa/iniciar — gera segredo TOTP (durante login)
# ---------------------------------------------------------------------------

def _utilizador_do_token_de_configuracao(db: Session, temp_token: str, request: Request) -> Utilizador:
    """A conta a que o token de configuração do 2FA se refere, se ainda o puder usar.

    O token vale 5 minutos e só serve para uma configuração: depois de o 2FA
    ficar ativo, recusa-se. Sem isto, quem o tivesse podia, nesses minutos, pôr
    um segredo seu por cima do que o dono acabou de ativar (e apagar-lhe os
    códigos de recuperação). A conta tem também de continuar a poder entrar.
    """
    payload = service._decodificar_temp_token(temp_token, "2fa_setup_required")
    utilizador = db.get(Utilizador, uuid.UUID(payload["user_id"]))
    if not utilizador:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Utilizador não encontrado.",
        )
    if utilizador.totp_ativo:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token temporário inválido para esta operação.",
        )
    service.exigir_conta_utilizavel(db, utilizador, request)
    return utilizador


@router.post(
    "/login/setup-2fa/iniciar",
    response_model=schemas.Setup2FAResponseSchema,
    summary="Iniciar setup 2FA durante o login",
)
@_limiter.limit("5/minute")
@_limiter.limit(_LIMITE_GRUPO, key_func=_chave_grupo_limite)
def iniciar_setup_2fa_login(
    request: Request,
    db: Session = Depends(get_session, scope="function"),
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
    utilizador = _utilizador_do_token_de_configuracao(db, authorization[7:], request)
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
@_limiter.limit(_LIMITE_GRUPO, key_func=_chave_grupo_limite)
def confirmar_setup_2fa_login(
    dados: schemas.Ativar2FASchema,
    request: Request,
    response: Response,
    db: Session = Depends(get_session, scope="function"),
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
    utilizador = _utilizador_do_token_de_configuracao(db, authorization[7:], request)
    service.ativar_2fa(db, utilizador, dados.codigo_totp, request)
    _registar_sessao_iniciada(db, utilizador, request, "2fa_configurado")
    access_token = service.criar_access_token(utilizador)
    refresh_token = service.criar_refresh_token(db, utilizador, request)
    db.commit()  # garante que o refresh token está persistido antes da resposta
    _set_refresh_cookie(response, request, refresh_token)
    # Login completo (com 2FA acabado de configurar): reconhece-se o dispositivo.
    dispositivo.definir(response, request, utilizador.id)
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
def verificar_2fa(
    dados: schemas.Verificar2FASchema,
    request: Request,
    response: Response,
    db: Session = Depends(get_session, scope="function"),
):
    """Verifica o código TOTP ou backup code após passo 1 do login.

    Os limites vivem em `service.login_passo2` (camadas 1–3), como no passo 1."""
    utilizador = service.login_passo2(
        db, dados.temp_token, dados.codigo, request,
        cookie_dispositivo=dispositivo.obter(request),
    )
    access_token = service.criar_access_token(utilizador)
    refresh_token = service.criar_refresh_token(db, utilizador, request)
    db.commit()  # garante que o refresh token está persistido antes da resposta
    _set_refresh_cookie(response, request, refresh_token)
    # Login completo (com 2FA): reconhece-se o dispositivo para as próximas vezes.
    dispositivo.definir(response, request, utilizador.id)
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
@_limiter.limit(_LIMITE_GRUPO, key_func=_chave_grupo_limite)
def alterar_password_temporaria_login(
    dados: schemas.AlterarPasswordTemporariaSchema,
    request: Request,
    response: Response,
    db: Session = Depends(get_session, scope="function"),
):
    """
    Altera a password temporária e aplica a política de MFA universal do passo 1:
    - Com TOTP ativo → passo 2 (verificar código)
    - Sem TOTP → forçar setup de 2FA (qualquer role)
    """
    # O serviço confirma também que a conta e a empresa ainda podem entrar.
    utilizador = service.alterar_password_temporaria_login(
        db,
        dados.temp_token,
        dados.nova_password,
        dados.confirmar_nova_password,
        request,
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
def renovar_token(
    request: Request,
    response: Response,
    db: Session = Depends(get_session, scope="function"),
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
def logout(
    request: Request,
    response: Response,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
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
def me(utilizador: CurrentUserDep, db: Session = Depends(get_session, scope="function")):
    """Devolve os dados do utilizador autenticado."""
    from app.empresas.models import Empresa
    from app.shared.capacidades import ambitos_de, capacidades_de
    empresa = db.get(Empresa, utilizador.empresa_id)
    info = schemas.MeResponseSchema.model_validate(utilizador)
    info.empresa_locale_preferido = empresa.locale_preferido if empresa else "pt"
    info.capacidades = capacidades_de(utilizador)
    info.ambitos = ambitos_de(utilizador)
    info.password_min = password_min(db, utilizador.empresa_id)
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
@_limiter.limit(f"{settings.LOGIN_RESET_GRUPO_POR_MINUTO}/minute", key_func=_chave_grupo_limite)
def solicitar_reset(
    dados: schemas.ResetPasswordSolicitarSchema,
    request: Request,
    tarefas: BackgroundTasks,
    db: Session = Depends(get_session, scope="function"),
):
    """
    Envia email de recuperação de password.
    Resposta sempre 202 independente de o email existir (evita user enumeration).

    O limite por rota (por /64) tem, empilhado, um limite por grupo (/48, /24)
    para um alojamento inteiro não martelar o envio; ambos são independentes de a
    conta existir, por isso não são oráculos.

    Excepção: se o servidor não tiver nenhum serviço de email configurado
    (EMAIL_ENABLED=false), responde 503 — esta é uma condição global da
    instalação, não revela nada sobre utilizadores específicos.
    """
    # Lido fresco: o wizard de setup pode ativar o email (grava no .env e invalida
    # a cache de get_settings) sem reiniciar o processo — uma cópia de settings
    # capturada no import ficaria presa no valor antigo.
    if not get_settings().EMAIL_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="email_nao_configurado",
        )

    service.solicitar_reset_password(db, dados.email, tarefas, request)
    return {"mensagem": "Se o email estiver registado, receberá instruções em breve."}


# ---------------------------------------------------------------------------
# POST /auth/password-reset/regras
# ---------------------------------------------------------------------------

@router.post(
    "/password-reset/regras",
    response_model=schemas.ResetPasswordRegrasRespostaSchema,
    summary="Regra de password que o reset vai exigir",
)
@_limiter.limit("10/minute")
def regras_reset(
    dados: schemas.ResetPasswordRegrasSchema,
    request: Request,
    db: Session = Depends(get_session, scope="function"),
):
    """O mínimo da empresa da conta, para o ecrã do reset o mostrar antes de a
    pessoa escrever. O token vai no corpo, não no URL, para não ficar nos registos."""
    return schemas.ResetPasswordRegrasRespostaSchema(
        password_min=service.regras_reset_password(db, dados.token)
    )


# ---------------------------------------------------------------------------
# POST /auth/password-reset/confirmar
# ---------------------------------------------------------------------------

@router.post(
    "/password-reset/confirmar",
    status_code=status.HTTP_200_OK,
    summary="Confirmar reset de password",
)
def confirmar_reset(
    dados: schemas.ResetPasswordConfirmarSchema,
    request: Request,
    db: Session = Depends(get_session, scope="function"),
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
@_limiter.limit("5/minute")
def configurar_2fa(
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """
    Gera novo segredo TOTP e 10 backup codes.
    Os backup codes são mostrados APENAS UMA VEZ — guardar em lugar seguro.
    O 2FA não fica ativo até chamar POST /2fa/ativar.

    Só com o 2FA inativo (409 se já estiver ativo). Limitada por IP como a
    configuração durante o login: cada pedido paga dez hashes argon2.
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
def ativar_2fa(
    dados: schemas.Ativar2FASchema,
    request: Request,
    response: Response,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """
    Confirma a ativação do 2FA com um código TOTP válido da app autenticadora.
    Após este passo, o 2FA fica ativo e será exigido em futuros logins.
    As outras sessões da conta terminam; quem ativou continua com uma nova.
    """
    service.ativar_2fa(db, utilizador, dados.codigo_totp, request)
    renovar_sessao_de_quem_pediu(db, response, request, utilizador)
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
@_limiter.limit("5/minute")
def desativar_2fa(
    dados: schemas.Desativar2FASchema,
    request: Request,
    response: Response,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """
    Desativa 2FA após confirmação com password atual.
    Apenas disponível para administradores. Limitada por IP: cada pedido paga
    uma verificação argon2. As outras sessões da conta terminam; quem desativou
    continua com uma nova.
    """
    service.desativar_2fa(db, utilizador, dados.password_atual, request)
    renovar_sessao_de_quem_pediu(db, response, request, utilizador)
    return {"mensagem": "Autenticação de dois fatores desativada."}
