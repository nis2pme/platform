"""
Schemas Pydantic/SQLModel para o módulo de utilizadores.
Cobrem leitura, criação, atualização de perfil, mudança de role e anonimização.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, field_validator, model_validator

from app.auth.models import RoleUtilizador
from app.shared.utils import validar_forca_password


# ---------------------------------------------------------------------------
# Resposta — utilizador
# ---------------------------------------------------------------------------


class UtilizadorSchema(BaseModel):
    """Representação segura de um utilizador (sem dados sensíveis)."""

    id: uuid.UUID
    empresa_id: uuid.UUID
    email: str
    nome: str
    role: RoleUtilizador
    ativo: bool
    totp_ativo: bool
    password_temporaria_ativa: bool
    consentimento_termos_at: datetime | None
    consentimento_termos_versao: str | None
    anonimizado_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def decifrar_pii(cls, data):
        """Decifra campos PII cifrados em repouso antes da validação."""
        from app.shared.pii import decifrar_pii
        if hasattr(data, "nome"):
            # Objeto SQLModel — converte para dict mutável
            nome = getattr(data, "nome", None)
            if nome is not None:
                object.__setattr__(data, "nome", decifrar_pii(nome))
        elif isinstance(data, dict) and "nome" in data:
            data["nome"] = decifrar_pii(data["nome"])
        return data


class ListaUtilizadoresSchema(BaseModel):
    """Lista paginada de utilizadores."""

    total: int
    utilizadores: list[UtilizadorSchema]


# ---------------------------------------------------------------------------
# Criação de utilizador (admin cria dentro da mesma empresa)
# ---------------------------------------------------------------------------


class CriarUtilizadorSchema(BaseModel):
    """
    Payload para criação de novo utilizador por um admin.
    O admin pode criar qualquer role exceto admin (não pode criar admin peer).
    """

    email: EmailStr
    nome: str
    password: str
    role: RoleUtilizador

    @field_validator("email")
    @classmethod
    def normalizar_email(cls, v: str) -> str:
        return v.lower().strip()

    @field_validator("nome")
    @classmethod
    def validar_nome(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Nome deve ter pelo menos 2 caracteres.")
        return v

    @field_validator("password")
    @classmethod
    def validar_password(cls, v: str) -> str:
        if len(v) < 10:
            raise ValueError("A password deve ter pelo menos 10 caracteres.")
        if not any(c.isdigit() for c in v):
            raise ValueError("A password deve conter pelo menos um número.")
        if not any(c.isupper() for c in v):
            raise ValueError("A password deve conter pelo menos uma maiúscula.")
        return v

    @field_validator("role")
    @classmethod
    def validar_role(cls, v: RoleUtilizador) -> RoleUtilizador:
        # Admin não pode criar outro admin via este endpoint —
        # o primeiro admin é criado no registo da empresa.
        if v == RoleUtilizador.ADMIN:
            raise ValueError(
                "Não é possível criar um utilizador com role admin por este endpoint. "
                "Use o registo da empresa para criar o admin inicial."
            )
        return v


# ---------------------------------------------------------------------------
# Atualização de perfil (self ou admin)
# ---------------------------------------------------------------------------


class AtualizarPerfilSchema(BaseModel):
    """
    Atualização de dados básicos do perfil.
    Só o próprio utilizador ou admin podem alterar.
    """

    nome: str | None = None
    role: RoleUtilizador | None = None
    ativo: bool | None = None

    @field_validator("nome")
    @classmethod
    def validar_nome(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if len(v) < 2:
                raise ValueError("Nome deve ter pelo menos 2 caracteres.")
        return v



# ---------------------------------------------------------------------------
# Alteração de role (admin only)
# ---------------------------------------------------------------------------


class AlterarRoleSchema(BaseModel):
    """Payload para alteração de role de um utilizador pelo admin."""

    novo_role: RoleUtilizador

    @field_validator("novo_role")
    @classmethod
    def validar_role(cls, v: RoleUtilizador) -> RoleUtilizador:
        if v == RoleUtilizador.ADMIN:
            raise ValueError(
                "Não é possível promover um utilizador a admin por este endpoint."
            )
        return v


# ---------------------------------------------------------------------------
# Alteração de password (self — requer password atual)
# ---------------------------------------------------------------------------


class AlterarPasswordSchema(BaseModel):
    """
    Payload para o utilizador alterar a sua própria password.
    Requer password atual como verificação extra.
    """

    password_atual: str
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


# ---------------------------------------------------------------------------
# Resposta de anonimização RGPD
# ---------------------------------------------------------------------------


class ResultadoAnonimizacaoSchema(BaseModel):
    """Confirmação de anonimização bem-sucedida."""

    mensagem: str
    utilizador_id: uuid.UUID
    anonimizado_at: datetime


class ResultadoResetPasswordAdminSchema(BaseModel):
    """Resposta após reset administrativo de password."""

    mensagem: str
    password_temporaria: str


class ResultadoResetMFAAdminSchema(BaseModel):
    """Resposta após reset administrativo de MFA."""

    mensagem: str


# ---------------------------------------------------------------------------
# Lista de implementadores (para delegação de controlos)
# ---------------------------------------------------------------------------


class ImplementadorSchema(BaseModel):
    """Utilizador com role implementador — usado para atribuição de controlos."""

    id: uuid.UUID
    nome: str
    email: str
    ativo: bool

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
