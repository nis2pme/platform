"""
Lógica de negócio do módulo de autenticação.
Todas as operações de auth passam por aqui — os routers são finos.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from fastapi import HTTPException, Request, status
from jose import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlmodel import Session, select

from app.auth.models import (
    CodigoBackup2FA,
    PasswordResetToken,
    RoleUtilizador,
    TokenRefresh,
    Utilizador,
)
from app.auth.schemas import RegistarEmpresaSchema
from app.config import get_settings
from app.empresas.models import Empresa
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.pii import cifrar_pii
from app.shared.utils import (
    decodificar_jwt_temp,
    gerar_codigos_backup,
    gerar_token_opaco,
    hash_token,
    normalizar_codigo_backup,
)

settings = get_settings()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Password hashing — argon2id (OWASP-recomendado)
# time_cost=3, memory_cost=65536 (64 MiB), parallelism=4, hash_len=32
# ---------------------------------------------------------------------------

_ph = PasswordHasher()

# Hash pré-computado para verificação constant-time quando o email não existe.
# Sem isto, o login responde mais rápido para emails inválidos (CWE-208).
_DUMMY_HASH: str = _ph.hash("NIS2PME_dummy_constant_time_protection")


def hash_password(password: str) -> str:
    """Gera hash argon2id da password com parâmetros OWASP-recomendados."""
    return _ph.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Verifica password em texto limpo contra hash argon2id."""
    try:
        return _ph.verify(hashed, plain)
    except (VerifyMismatchError, InvalidHashError):
        return False


# ---------------------------------------------------------------------------
# Fernet — cifra/decifra segredo TOTP em repouso
# ---------------------------------------------------------------------------

def _get_fernet() -> Fernet:
    return Fernet(settings.TOTP_ENCRYPTION_KEY.encode())


def cifrar_totp_secret(secret: str) -> str:
    """Cifra o segredo TOTP com Fernet antes de guardar na DB."""
    return _get_fernet().encrypt(secret.encode()).decode()


def decifrar_totp_secret(cifrado: str) -> str:
    """Decifra o segredo TOTP armazenado na DB."""
    return _get_fernet().decrypt(cifrado.encode()).decode()


# ---------------------------------------------------------------------------
# JWT — access token e temp token (2FA pending)
# ---------------------------------------------------------------------------

def criar_access_token(utilizador: Utilizador) -> str:
    """
    Cria um JWT de acesso completo com claims: sub, user_id, empresa_id, role, type.
    Expira em JWT_ACCESS_TOKEN_EXPIRE_MINUTES (default 30 min).
    """
    expira = datetime.now(timezone.utc) + timedelta(
        minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": str(utilizador.email),
        "user_id": str(utilizador.id),
        "empresa_id": str(utilizador.empresa_id),
        "role": utilizador.role.value,
        "type": "access",
        "jti": str(uuid.uuid4()),
        "exp": expira,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def criar_temp_token(utilizador: Utilizador, tipo: str) -> str:
    """
    Cria JWT temporário para fluxo de 2FA.
    tipo = "2fa_pending" | "2fa_setup_required"
    Expira em 5 minutos.
    """
    expira = datetime.now(timezone.utc) + timedelta(minutes=5)
    payload = {
        "sub": str(utilizador.email),
        "user_id": str(utilizador.id),
        "empresa_id": str(utilizador.empresa_id),
        "type": tipo,
        "jti": str(uuid.uuid4()),
        "exp": expira,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def _decodificar_temp_token(temp_token: str, tipo_esperado: str) -> dict:
    """Valida e decodifica um token temporário de 2FA. Delega em decodificar_jwt_temp."""
    return decodificar_jwt_temp(
        temp_token, settings.JWT_SECRET_KEY, tipo_esperado, settings.JWT_ALGORITHM
    )


def alterar_password_temporaria_login(
    db: Session,
    temp_token: str,
    nova_password: str,
    confirmar_nova_password: str,
    request: Request | None = None,
) -> Utilizador:
    """Altera password temporária e conclui login."""
    from app.shared.utils import validar_forca_password

    if nova_password != confirmar_nova_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="As passwords não coincidem.",
        )

    valida, mensagem = validar_forca_password(nova_password)
    if not valida:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=mensagem,
        )

    payload = _decodificar_temp_token(temp_token, "password_change_required")
    utilizador_id = uuid.UUID(payload["user_id"])
    utilizador = db.get(Utilizador, utilizador_id)
    if not utilizador or not utilizador.ativo or utilizador.deleted_at:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Utilizador inválido para esta operação.",
        )

    if not utilizador.password_temporaria_ativa:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este utilizador já não tem password temporária ativa.",
        )

    if verify_password(nova_password, utilizador.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A nova password não pode ser igual à password temporária.",
        )

    utilizador.password_hash = hash_password(nova_password)
    utilizador.password_temporaria_ativa = False
    utilizador.updated_at = datetime.now(timezone.utc)
    db.add(utilizador)

    registar_acao(
        db,
        acao=Acao.PASSWORD_ALTERADA,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        dados_novos={"origem": "password_temporaria"},
        request=request,
    )

    return utilizador


# ---------------------------------------------------------------------------
# Refresh tokens — opacos, armazenados como hash SHA-256
# ---------------------------------------------------------------------------

def criar_refresh_token(
    db: Session,
    utilizador: Utilizador,
    request: Request | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> str:
    """
    Gera um refresh token opaco, guarda o hash na DB, e devolve o token em texto limpo.
    O token deve ser enviado ao cliente em httpOnly cookie.
    """
    token = gerar_token_opaco(32)
    token_hash = hash_token(token)

    ip = ip_address
    ua = user_agent
    if request:
        from app.shared.audit import _extrair_ip
        ip = _extrair_ip(request)
        ua = request.headers.get("user-agent", "")[:500]

    expira = datetime.now(timezone.utc) + timedelta(
        days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS
    )

    refresh = TokenRefresh(
        utilizador_id=utilizador.id,
        token_hash=token_hash,
        ip_address=cifrar_pii(ip),
        user_agent=cifrar_pii(ua),
        expires_at=expira.replace(tzinfo=None),  # armazenar sem tz na DB
    )
    db.add(refresh)
    return token


def revogar_refresh_token(db: Session, token: str) -> bool:
    """
    Revoga um refresh token (logout). Devolve True se encontrado e revogado.
    """
    token_hash = hash_token(token)
    refresh = db.exec(
        select(TokenRefresh).where(TokenRefresh.token_hash == token_hash)
    ).first()

    if not refresh:
        return False

    refresh.revogado_at = datetime.now(timezone.utc)
    db.add(refresh)
    return True


def validar_refresh_token(db: Session, token: str) -> Utilizador | None:
    """
    Valida um refresh token: existe na DB, não revogado, não expirado.
    Devolve o Utilizador associado ou None se inválido.
    """
    token_hash = hash_token(token)
    refresh = db.exec(
        select(TokenRefresh).where(
            TokenRefresh.token_hash == token_hash,
            TokenRefresh.revogado_at.is_(None),  # type: ignore[union-attr]
            TokenRefresh.expires_at > datetime.now(timezone.utc),
        )
    ).first()

    if not refresh:
        logger.info("Refresh token inválido: não encontrado, revogado ou expirado")
        return None

    utilizador = db.get(Utilizador, refresh.utilizador_id)
    if not utilizador:
        logger.info("Refresh token válido mas utilizador %s não encontrado", refresh.utilizador_id)
        return None
    if not utilizador.ativo:
        logger.info("Refresh token válido mas utilizador %s está inativo", utilizador.id)
        return None

    return utilizador


# ---------------------------------------------------------------------------
# Registo: Empresa + admin num único passo atómico
# ---------------------------------------------------------------------------

def registar_empresa_e_admin(
    db: Session,
    dados: RegistarEmpresaSchema,
    request: Request | None = None,
) -> tuple[Empresa, Utilizador]:
    """
    Cria uma nova Empresa e o seu primeiro utilizador admin atomicamente.
    Apenas disponível quando DEPLOYMENT_MODE=saas.

    Returns:
        Tupla (Empresa, Utilizador) criados.

    Raises:
        HTTPException 409 se o email já estiver registado.
        HTTPException 403 se modo onprem.
    """
    if settings.DEPLOYMENT_MODE == "onprem":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registo público não disponível neste modo de instalação.",
        )

    # Verifica email único
    existente = db.exec(
        select(Utilizador).where(Utilizador.email == dados.admin_email.lower())
    ).first()
    if existente:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este email já está registado.",
        )

    # Cria Empresa
    empresa = Empresa(
        nome=cifrar_pii(dados.empresa_nome),
        nif=cifrar_pii(dados.empresa_nif),
        setor=dados.empresa_setor,
        dimensao=dados.empresa_dimensao,
        tipo_entidade=dados.empresa_tipo_entidade,
    )
    db.add(empresa)
    db.flush()  # obter empresa.id sem commit

    # Cria admin
    agora = datetime.now(timezone.utc)
    admin = Utilizador(
        empresa_id=empresa.id,
        email=dados.admin_email.lower(),
        nome=cifrar_pii(dados.admin_nome),
        password_hash=hash_password(dados.admin_password),
        role=RoleUtilizador.ADMIN,
        consentimento_termos_at=agora,
        consentimento_termos_versao=dados.versao_termos,
    )
    db.add(admin)
    db.flush()  # obter admin.id

    registar_acao(
        db,
        acao=Acao.EMPRESA_REGISTADA,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa.id,
        utilizador_id=admin.id,
        entidade_tipo="Empresa",
        entidade_id=empresa.id,
        dados_novos={"admin_email": admin.email},
        request=request,
    )

    # Inicializa registos ControloEmpresaV2 para o framework da empresa
    # Import local para evitar circular import (controlos → empresas → auth)
    from app.controlos.service import inicializar_controlos_empresa
    inicializar_controlos_empresa(db, empresa.id)

    return empresa, admin


# ---------------------------------------------------------------------------
# Login passo 1: validação email + password
# ---------------------------------------------------------------------------

def login_passo1(
    db: Session, email: str, password: str, request: Request | None = None
) -> dict:
    """
    Valida credenciais e determina o próximo passo do fluxo de autenticação.

    Returns um dict com:
            - "tipo": "acesso_completo" | "2fa_necessario" | "2fa_configurar" | "password_temporaria"
      - "access_token" (se acesso_completo)
      - "temp_token" (se 2fa)
      - "utilizador" (se acesso_completo)
    """
    ip = None
    if request:
        from app.shared.audit import _extrair_ip
        ip = _extrair_ip(request)

    utilizador = db.exec(
        select(Utilizador).where(Utilizador.email == email.lower())
    ).first()

    # Constant-time: verify_password é SEMPRE chamado, mesmo quando o utilizador não existe.
    # Sem isto, a ausência de bcrypt (~200ms) revela que o email não está registado (CWE-208).
    password_valida = verify_password(
        password,
        utilizador.password_hash if utilizador else _DUMMY_HASH,
    )

    # Resposta HTTP genérica para não revelar se o email existe (anti-enumeração CWE-208).
    # O log interno inclui empresa_id/utilizador_id quando o utilizador é encontrado —
    # isso não expõe informação ao atacante (a resposta HTTP é idêntica nos dois casos).
    if not utilizador or not password_valida:
        _motivo = "email_nao_encontrado" if not utilizador else "password_incorreta"
        registar_acao(
            db,
            acao=Acao.LOGIN_FALHA,
            resultado=ResultadoAcao.FALHA,
            empresa_id=utilizador.empresa_id if utilizador else None,
            utilizador_id=utilizador.id if utilizador else None,
            entidade_tipo="Utilizador",
            dados_novos={"email": email, "motivo": _motivo},
            request=request,
            force_commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais inválidas.",
        )

    if not utilizador.ativo or utilizador.deleted_at:
        registar_acao(
            db,
            acao=Acao.LOGIN_FALHA,
            resultado=ResultadoAcao.FALHA,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            dados_novos={"email": utilizador.email, "motivo": "conta_inativa"},
            request=request,
            force_commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Conta desativada. Contacte o administrador.",
        )

    if utilizador.password_temporaria_ativa:
        temp_token = criar_temp_token(utilizador, "password_change_required")
        return {"tipo": "password_temporaria", "temp_token": temp_token}

    # Verifica se a empresa da qual este utilizador faz parte está suspensa pelo superadmin
    empresa = db.get(Empresa, utilizador.empresa_id)
    if empresa and empresa.suspenso:
        registar_acao(
            db,
            acao=Acao.LOGIN_FALHA,
            resultado=ResultadoAcao.FALHA,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            dados_novos={"email": utilizador.email, "motivo": "empresa_suspensa"},
            request=request,
            force_commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A sua conta está temporariamente suspensa. Contacte o suporte.",
        )

    # 2FA obrigatório para todos os roles (Feature: MFA universal)
    if utilizador.totp_ativo:
        # Passo 2: verificar TOTP
        temp_token = criar_temp_token(utilizador, "2fa_pending")
        return {"tipo": "2fa_necessario", "temp_token": temp_token}

    # Qualquer utilizador sem 2FA configurado — forçar setup
    temp_token = criar_temp_token(utilizador, "2fa_setup_required")
    return {"tipo": "2fa_configurar", "temp_token": temp_token}


# ---------------------------------------------------------------------------
# Login passo 2: verificação TOTP / backup code
# ---------------------------------------------------------------------------

def login_passo2(
    db: Session, temp_token: str, codigo: str, request: Request | None = None
) -> Utilizador:
    """
    Valida o código TOTP ou backup code após passo 1.
    Devolve o Utilizador autenticado ou lança 401.
    """
    import pyotp

    payload = _decodificar_temp_token(temp_token, "2fa_pending")
    utilizador_id = uuid.UUID(payload["user_id"])

    utilizador = db.get(Utilizador, utilizador_id)
    if not utilizador or not utilizador.totp_ativo or not utilizador.totp_secret_cifrado:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Configuração 2FA inválida.",
        )

    secret = decifrar_totp_secret(utilizador.totp_secret_cifrado)
    totp = pyotp.TOTP(secret)

    # Tenta TOTP primeiro
    codigo_limpo = codigo.strip().replace(" ", "")
    if totp.verify(codigo_limpo, valid_window=1):
        registar_acao(
            db,
            acao=Acao.FA2_VERIFICADO,
            resultado=ResultadoAcao.SUCESSO,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            dados_novos={"email": utilizador.email},
            request=request,
        )
        registar_acao(
            db,
            acao=Acao.LOGIN_SUCESSO,
            resultado=ResultadoAcao.SUCESSO,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            dados_novos={"email": utilizador.email},
            request=request,
        )
        return utilizador

    # Tenta backup code
    codigo_normalizado = normalizar_codigo_backup(codigo_limpo)
    codigos = db.exec(
        select(CodigoBackup2FA).where(
            CodigoBackup2FA.utilizador_id == utilizador_id,
            CodigoBackup2FA.usado_at.is_(None),  # type: ignore[union-attr]
        )
    ).all()

    for c in codigos:
        if verify_password(codigo_normalizado, c.codigo_hash):
            c.usado_at = datetime.now(timezone.utc)
            db.add(c)
            registar_acao(
                db,
                acao=Acao.BACKUP_CODE_USADO,
                resultado=ResultadoAcao.SUCESSO,
                empresa_id=utilizador.empresa_id,
                utilizador_id=utilizador.id,
                entidade_id=c.id,
                request=request,
            )
            registar_acao(
                db,
                acao=Acao.LOGIN_SUCESSO,
                resultado=ResultadoAcao.SUCESSO,
                empresa_id=utilizador.empresa_id,
                utilizador_id=utilizador.id,
                dados_novos={"email": utilizador.email},
                request=request,
            )
            return utilizador

    # Falhou
    registar_acao(
        db,
        acao=Acao.FA2_FALHOU,
        resultado=ResultadoAcao.FALHA,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        dados_novos={"email": utilizador.email, "motivo": "codigo_invalido"},
        request=request,
        force_commit=True,
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Código 2FA inválido.",
    )


# ---------------------------------------------------------------------------
# Refresh: troca refresh token por novo access token
# ---------------------------------------------------------------------------

def renovar_access_token(
    db: Session, refresh_token: str, request: Request | None = None
) -> tuple[str, str, Utilizador]:
    """
    Valida o refresh token do cookie e emite novo access token + novo refresh token.
    O refresh token anterior é revogado (rotação — CWE-384).
    Lança 401 se o token for inválido, revogado ou expirado.

    Returns:
        Tupla (access_token, novo_refresh_token, utilizador).
    """
    utilizador = validar_refresh_token(db, refresh_token)
    if not utilizador:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão expirada. Por favor, faça login novamente.",
        )

    # Rotação: revogar token antigo e emitir novo
    revogar_refresh_token(db, refresh_token)
    novo_refresh_token = criar_refresh_token(db, utilizador, request)

    registar_acao(
        db,
        acao=Acao.REFRESH_TOKEN,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        request=request,
    )
    return criar_access_token(utilizador), novo_refresh_token, utilizador


# ---------------------------------------------------------------------------
# Logout: revoga refresh token
# ---------------------------------------------------------------------------

def logout(
    db: Session,
    refresh_token: str,
    utilizador: Utilizador,
    request: Request | None = None,
) -> None:
    """Revoga o refresh token e regista o logout no AuditLog."""
    revogar_refresh_token(db, refresh_token)
    registar_acao(
        db,
        acao=Acao.LOGOUT,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        dados_novos={"email": utilizador.email},
        request=request,
    )


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

async def solicitar_reset_password(
    db: Session, email: str, request: Request | None = None
) -> None:
    """
    Cria token de reset e envia email via provedor configurado (SMTP ou Brevo).
    Resposta sempre genérica — não revela se o email existe (evita user enumeration).
    """
    from app.shared.email import enviar_email_reset_password

    utilizador = db.exec(
        select(Utilizador).where(Utilizador.email == email.lower())
    ).first()

    if utilizador and utilizador.ativo and not utilizador.deleted_at:
        # Gerar token
        token = gerar_token_opaco(32)
        token_hash = hash_token(token)
        expira = datetime.now(timezone.utc) + timedelta(hours=1)

        ip = None
        if request:
            from app.shared.audit import _extrair_ip
            ip = _extrair_ip(request)

        reset_token = PasswordResetToken(
            utilizador_id=utilizador.id,
            token_hash=token_hash,
            ip_address=cifrar_pii(ip) if ip else None,
            expires_at=expira,
        )
        db.add(reset_token)

        registar_acao(
            db,
            acao=Acao.PASSWORD_RESET_PEDIDO,
            resultado=ResultadoAcao.SUCESSO,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            request=request,
        )
        db.commit()

        link = f"{settings.APP_URL}/reset-password?token={token}"
        await enviar_email_reset_password(email, link)


def confirmar_reset_password(
    db: Session, token: str, nova_password: str, request: Request | None = None
) -> None:
    """
    Valida o token de reset e atualiza a password.
    Lança 400 se inválido, expirado ou já usado.
    """
    token_hash = hash_token(token)
    reset = db.exec(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.usado_at.is_(None),  # type: ignore[union-attr]
            PasswordResetToken.expires_at > datetime.now(timezone.utc),
        )
    ).first()

    if not reset:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token inválido ou expirado.",
        )

    utilizador = db.get(Utilizador, reset.utilizador_id)
    if not utilizador or not utilizador.ativo:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token inválido.",
        )

    # Atualizar password
    utilizador.password_hash = hash_password(nova_password)
    utilizador.updated_at = datetime.now(timezone.utc)
    reset.usado_at = datetime.now(timezone.utc)

    db.add(utilizador)
    db.add(reset)

    # Revogar todas as sessões ativas (segurança pós-reset)
    sessoes_ativas = db.exec(
        select(TokenRefresh).where(
            TokenRefresh.utilizador_id == utilizador.id,
            TokenRefresh.revogado_at.is_(None),  # type: ignore[union-attr]
        )
    ).all()
    agora = datetime.now(timezone.utc)
    for sessao in sessoes_ativas:
        sessao.revogado_at = agora
        db.add(sessao)

    registar_acao(
        db,
        acao=Acao.PASSWORD_RESET_CONFIRMADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        request=request,
    )


# ---------------------------------------------------------------------------
# Configuração e ativação de 2FA (TOTP)
# ---------------------------------------------------------------------------

def setup_2fa(
    db: Session, utilizador: Utilizador, request: Request | None = None
) -> tuple[str, list[str]]:
    """
    Gera novo segredo TOTP e backup codes.
    O 2FA NÃO fica ativo — requer confirmação via ativar_2fa().

    Returns:
        Tuple (totp_uri, backup_codes) — mostrar ao utilizador UMA vez.
    """
    import pyotp

    # Gerar segredo TOTP novo
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)

    # URI para QR code no frontend (otpauth://totp/...)
    totp_uri = totp.provisioning_uri(
        name=utilizador.email,
        issuer_name=settings.APP_NAME,
    )

    # Guardar segredo cifrado (ainda não ativo)
    utilizador.totp_secret_cifrado = cifrar_totp_secret(secret)
    utilizador.updated_at = datetime.now(timezone.utc)
    db.add(utilizador)

    # Gerar backup codes e guardar hashes
    codigos_texto = gerar_codigos_backup(10)

    # Remover backup codes antigos se existirem
    codigos_antigos = db.exec(
        select(CodigoBackup2FA).where(
            CodigoBackup2FA.utilizador_id == utilizador.id
        )
    ).all()
    for c in codigos_antigos:
        db.delete(c)

    # Guardar novos hashes
    for codigo in codigos_texto:
        normalizado = normalizar_codigo_backup(codigo)
        codigo_hash = hash_password(normalizado)
        db.add(
            CodigoBackup2FA(
                utilizador_id=utilizador.id,
                codigo_hash=codigo_hash,
            )
        )

    return totp_uri, codigos_texto


def ativar_2fa(
    db: Session,
    utilizador: Utilizador,
    codigo_totp: str,
    request: Request | None = None,
) -> None:
    """
    Valida o código TOTP e ativa o 2FA para o utilizador.
    Lança 400 se o código for inválido.
    """
    import pyotp

    if not utilizador.totp_secret_cifrado:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configure primeiro o 2FA antes de o ativar.",
        )

    secret = decifrar_totp_secret(utilizador.totp_secret_cifrado)
    totp = pyotp.TOTP(secret)

    if not totp.verify(codigo_totp.strip(), valid_window=1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Código TOTP inválido. Verifique a hora do seu dispositivo e tente novamente.",
        )

    utilizador.totp_ativo = True
    utilizador.updated_at = datetime.now(timezone.utc)
    db.add(utilizador)

    registar_acao(
        db,
        acao=Acao.FA2_ATIVADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        dados_novos={"email": utilizador.email},
        request=request,
    )


def desativar_2fa(
    db: Session,
    utilizador: Utilizador,
    password_atual: str,
    request: Request | None = None,
) -> None:
    """
    Desativa 2FA para o utilizador após confirmação com password.
    Apenas para utilizadores com role admin (verificado no router).
    """
    if not verify_password(password_atual, utilizador.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password incorreta.",
        )

    utilizador.totp_ativo = False
    utilizador.totp_secret_cifrado = None
    utilizador.updated_at = datetime.now(timezone.utc)
    db.add(utilizador)

    # Apagar backup codes
    codigos = db.exec(
        select(CodigoBackup2FA).where(
            CodigoBackup2FA.utilizador_id == utilizador.id
        )
    ).all()
    for c in codigos:
        db.delete(c)

    registar_acao(
        db,
        acao=Acao.FA2_DESATIVADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        request=request,
    )
