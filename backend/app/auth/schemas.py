"""
Schemas Pydantic para o módulo de autenticação.
Separados dos modelos SQLModel para controlar exactamente o que entra/sai da API.
"""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.auth.models import RoleUtilizador
from app.empresas.models import DimensaoEmpresa, NivelQNRCS, TipoEntidade
from app.shared.utils import PASSWORD_MIN


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
    empresa_nivel_qnrcs: NivelQNRCS | None = None

    # Dados do admin
    admin_nome: str
    admin_email: EmailStr
    admin_password: str

    # RGPD — consentimento obrigatório
    aceitar_termos: bool
    versao_termos: str = "1.0"

    # O registo SaaS só cria trials: a borda de registo envia "trial", e é tudo o
    # que aceita. Um plano pago define-o o operador (superadmin), nunca quem tem o
    # token da borda.
    plano: Literal["trial"] = "trial"
    # Fim do período de avaliação, decidido pela borda de registo (é ela que
    # suspende a conta nesse dia). Só se aceita no registo SaaS — o único que
    # exige o token interno da borda. Sem ela, o gateway conta o prazo sozinho.
    trial_expira_em: datetime | None = None

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
    # Com requires_password_change: o mínimo que a troca vai exigir.
    password_min: int | None = None

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


class RegistoCriadoSchema(BaseModel):
    """Resposta do registo de uma nova empresa: conta criada, sem sessão.

    O registo não autentica — a autenticação (incluindo a configuração de 2FA)
    faz-se no fluxo de login. Assim o registo pode ser intermediado por uma
    borda externa sem que esta receba tokens de sessão da conta criada.
    """

    empresa_id: uuid.UUID
    admin_email: EmailStr
    mensagem: str = "Conta criada com sucesso."


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
    # Capacidades efetivas ("modulo.classe") derivadas do role — o frontend
    # consome esta lista (pode(modulo, classe)) e nunca duplica a matriz.
    capacidades: list[str] = []
    # Só as capacidades cujo alcance é limitado ("modulo.classe" → âmbito). As
    # que não constam alcançam qualquer registo do tenant. Campo à parte para
    # não partir clientes que só conhecem `capacidades`.
    ambitos: dict[str, str] = {}
    # Comprimento mínimo de password em vigor na empresa (criar contas, trocar a própria).
    password_min: int = PASSWORD_MIN

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def decifrar_pii(cls, data):
        """Decifra campos PII cifrados em repouso antes da validação.

        Copia os atributos para um dict em vez de mutar a entidade ORM recebida:
        mutar o objeto persistente faria o commit automático do get_session regravar
        o nome DECIFRADO (em claro) na base — corrompia a cifra em repouso e partia o
        login seguinte (texto simples não decifra → InvalidToken → 500).
        """
        from app.shared.pii import decifrar_pii
        if not isinstance(data, dict):
            data = {k: getattr(data, k) for k in cls.model_fields if hasattr(data, k)}
        if data.get("nome") is not None:
            # Um nome que não decifra (chave trocada, linha gravada em claro) sai
            # vazio: o nome é para mostrar, e não pode impedir ninguém de entrar.
            data["nome"] = decifrar_pii(data["nome"]) or ""
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
    # Capacidades efetivas ("modulo.classe") — ver UtilizadorInfoSchema.
    capacidades: list[str] = []
    ambitos: dict[str, str] = {}
    password_min: int = PASSWORD_MIN

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def decifrar_pii(cls, data):
        """Decifra campos PII cifrados em repouso antes da validação.

        Copia os atributos para um dict em vez de mutar a entidade ORM recebida:
        mutar o objeto persistente faria o commit automático do get_session regravar
        o nome DECIFRADO (em claro) na base — corrompia a cifra em repouso e partia o
        login seguinte (texto simples não decifra → InvalidToken → 500).
        """
        from app.shared.pii import decifrar_pii
        if not isinstance(data, dict):
            data = {k: getattr(data, k) for k in cls.model_fields if hasattr(data, k)}
        if data.get("nome") is not None:
            # Um nome que não decifra (chave trocada, linha gravada em claro) sai
            # vazio: o nome é para mostrar, e não pode impedir ninguém de entrar.
            data["nome"] = decifrar_pii(data["nome"]) or ""
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


class ResetPasswordRegrasSchema(BaseModel):
    """Pedido da regra de password que um reset vai exigir."""

    # O token tem 43 caracteres; o teto só impede que se mande um corpo enorme.
    token: str = Field(max_length=128)


class ResetPasswordRegrasRespostaSchema(BaseModel):
    password_min: int


class ResetPasswordConfirmarSchema(BaseModel):
    """Confirmação do reset com token e nova password."""

    token: str
    nova_password: str


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
