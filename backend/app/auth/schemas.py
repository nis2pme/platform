"""
Schemas Pydantic para o módulo de autenticação.
Separados dos modelos SQLModel para controlar exactamente o que entra/sai da API.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, field_validator, model_validator

from app.auth.models import RoleUtilizador
from app.empresas.models import DimensaoEmpresa, TipoEntidade
from app.shared.utils import validar_forca_password


# ---------------------------------------------------------------------------
# Registo
# ---------------------------------------------------------------------------

class RegistarEmpresaSchema(BaseModel):
    """
    Criação de nova empresa + primeiro utilizador admin.
    Requer consentimento explícito dos termos de serviço (RGPD Art. 7).
    """

    # Dados da empresa
    empresa_nome: str
    empresa_nif: str | None = None
    empresa_setor: str | None = None
    empresa_dimensao: DimensaoEmpresa | None = None
    empresa_tipo_entidade: TipoEntidade = TipoEntidade.BASE

    # Dados do admin
    admin_nome: str
    admin_email: EmailStr
    admin_password: str

    # RGPD — consentimento obrigatório
    aceitar_termos: bool
    versao_termos: str = "1.0"

    @field_validator("admin_password")
    @classmethod
    def validar_password(cls, v: str) -> str:
        valida, mensagem = validar_forca_password(v)
        if not valida:
            raise ValueError(mensagem)
        return v

    @field_validator("aceitar_termos")
    @classmethod
    def termos_obrigatorios(cls, v: bool) -> bool:
        if not v:
            raise ValueError(
                "É necessário aceitar os termos de serviço para criar uma conta."
            )
        return v


# ---------------------------------------------------------------------------
# Login (passo 1: email + password)
# ---------------------------------------------------------------------------

class LoginSchema(BaseModel):
    email: EmailStr
    password: str


class LoginResponseSchema(BaseModel):
    """
    Resposta do passo 1 do login.
    Pode indicar acesso completo, necessidade de 2FA, ou necessidade de configurar 2FA.
    """

    # Acesso completo (implementador/ceo sem 2FA obrigatório)
    access_token: str | None = None
    token_type: str = "bearer"

    # Fluxo 2FA
    requires_2fa: bool = False
    requires_2fa_setup: bool = False
    requires_password_change: bool = False

    # Token temporário para completar o passo 2 (válido 5 minutos)
    temp_token: str | None = None

    # Info do utilizador (só quando acesso completo)
    utilizador: "UtilizadorInfoSchema | None" = None


# ---------------------------------------------------------------------------
# Login (passo 2: verificação 2FA)
# ---------------------------------------------------------------------------

class Verificar2FASchema(BaseModel):
    """Verificação do código TOTP ou backup code após passo 1 do login."""

    temp_token: str
    codigo: str  # Código TOTP (6 dígitos) ou backup code (XXXX-XXXX-XXXX)


class TokenResponseSchema(BaseModel):
    """Resposta final após autenticação completa (access token + info do utilizador)."""

    access_token: str
    token_type: str = "bearer"
    utilizador: "UtilizadorInfoSchema"
    # O refresh token vai em httpOnly cookie, não no body


# ---------------------------------------------------------------------------
# Utilizador info (incluído nas respostas de auth)
# ---------------------------------------------------------------------------

class UtilizadorInfoSchema(BaseModel):
    """Informação pública do utilizador — nunca inclui password_hash ou totp_secret."""

    id: uuid.UUID
    empresa_id: uuid.UUID
    email: str
    nome: str
    role: RoleUtilizador
    totp_ativo: bool
    created_at: datetime
    empresa_locale_preferido: str = "pt"

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def decifrar_pii(cls, data):
        """Decifra campos PII cifrados em repouso antes da validação."""
        from app.shared.pii import decifrar_pii
        if hasattr(data, "nome"):
            nome = getattr(data, "nome", None)
            if nome is not None:
                object.__setattr__(data, "nome", decifrar_pii(nome))
        elif isinstance(data, dict) and "nome" in data:
            data["nome"] = decifrar_pii(data["nome"])
        return data


# ---------------------------------------------------------------------------
# /auth/me
# ---------------------------------------------------------------------------

class MeResponseSchema(BaseModel):
    """Resposta do endpoint GET /auth/me."""

    id: uuid.UUID
    empresa_id: uuid.UUID
    email: str
    nome: str
    role: RoleUtilizador
    totp_ativo: bool
    created_at: datetime
    updated_at: datetime
    empresa_locale_preferido: str = "pt"

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def decifrar_pii(cls, data):
        """Decifra campos PII cifrados em repouso antes da validação."""
        from app.shared.pii import decifrar_pii
        if hasattr(data, "nome"):
            nome = getattr(data, "nome", None)
            if nome is not None:
                object.__setattr__(data, "nome", decifrar_pii(nome))
        elif isinstance(data, dict) and "nome" in data:
            data["nome"] = decifrar_pii(data["nome"])
        return data


# ---------------------------------------------------------------------------
# Refresh token
# ---------------------------------------------------------------------------

class RefreshResponseSchema(BaseModel):
    """Novo access token após renovação via refresh cookie."""

    access_token: str
    token_type: str = "bearer"
    utilizador: UtilizadorInfoSchema


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

class ResetPasswordSolicitarSchema(BaseModel):
    """Pedido de reset de password por email."""

    email: EmailStr


class ResetPasswordConfirmarSchema(BaseModel):
    """Confirmação do reset com token e nova password."""

    token: str
    nova_password: str

    @field_validator("nova_password")
    @classmethod
    def validar_password(cls, v: str) -> str:
        valida, mensagem = validar_forca_password(v)
        if not valida:
            raise ValueError(mensagem)
        return v


# ---------------------------------------------------------------------------
# Configuração e ativação de 2FA
# ---------------------------------------------------------------------------

class Setup2FAResponseSchema(BaseModel):
    """
    Resposta da configuração de 2FA.
    Contém o segredo TOTP, URI para QR code, e backup codes.
    MOSTRAR AO UTILIZADOR APENAS UMA VEZ — não são recuperáveis depois.
    """

    totp_uri: str          # otpauth://totp/... para gerar QR code no frontend
    backup_codes: list[str]  # 10 códigos XXXX-XXXX-XXXX — mostrar uma vez


class Ativar2FASchema(BaseModel):
    """Confirmação da ativação de 2FA com código TOTP para validar o setup."""

    codigo_totp: str


class AlterarPasswordTemporariaSchema(BaseModel):
    """Troca de password temporária durante o login."""

    temp_token: str
    nova_password: str
    confirmar_nova_password: str

    @field_validator("nova_password")
    @classmethod
    def validar_nova_password(cls, v: str) -> str:
        valida, mensagem = validar_forca_password(v)
        if not valida:
            raise ValueError(mensagem)
        return v

    @field_validator("confirmar_nova_password")
    @classmethod
    def validar_confirmacao(cls, v: str, info) -> str:
        if "nova_password" in info.data and v != info.data["nova_password"]:
            raise ValueError("As passwords não coincidem.")
        return v


class Desativar2FASchema(BaseModel):
    """Desativação de 2FA — requer confirmação com password atual (apenas admin)."""

    password_atual: str


# Atualizar forward references
LoginResponseSchema.model_rebuild()
TokenResponseSchema.model_rebuild()
