"""
Modelos SQLModel do módulo de autenticação:
  - Utilizador
  - TokenRefresh (refresh tokens revoáveis lado-servidor)
  - PasswordResetToken (tokens de reset de password, single-use)
  - CodigoBackup2FA (backup codes para 2FA)
"""
import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlmodel import Field, Relationship, SQLModel


class RoleUtilizador(str, Enum):
    """Roles de acesso RBAC da plataforma."""

    ADMIN = "admin"
    SUBADMIN = "subadmin"
    IMPLEMENTADOR = "implementador"
    AUDITOR = "auditor"
    CEO = "ceo"


# ---------------------------------------------------------------------------
# Utilizador
# ---------------------------------------------------------------------------

class Utilizador(SQLModel, table=True):
    """
    Utilizador da plataforma, ligado a uma Empresa (tenant).
    Contém campos de consentimento RGPD e suporte a TOTP 2FA.
    """

    __tablename__ = "utilizadores"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        index=True,
    )

    # Tenant
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    # Identidade
    email: str = Field(max_length=255, unique=True, index=True)
    nome: str = Field(max_length=500)  # cifrado em repouso — tamanho aumentado para token Fernet
    password_hash: str = Field(max_length=255)

    # RBAC
    role: RoleUtilizador = Field(default=RoleUtilizador.IMPLEMENTADOR)

    # Estado da conta
    ativo: bool = Field(default=True)
    password_temporaria_ativa: bool = Field(default=False)

    # 2FA — TOTP
    # O segredo TOTP é cifrado com Fernet antes de ser guardado (ISO27001 cifra em repouso)
    totp_secret_cifrado: str | None = Field(default=None)
    totp_ativo: bool = Field(default=False)

    # RGPD — consentimento explícito (Art. 7 RGPD)
    consentimento_termos_at: datetime | None = Field(default=None)
    consentimento_termos_versao: str | None = Field(default=None, max_length=20)

    # RGPD — direito ao apagamento (anonimização, não eliminação física)
    anonimizado_at: datetime | None = Field(default=None)

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deleted_at: datetime | None = Field(default=None)  # soft delete

    # Relationships
    empresa: "Empresa" = Relationship(back_populates="utilizadores")  # type: ignore[name-defined]
    refresh_tokens: list["TokenRefresh"] = Relationship(back_populates="utilizador")
    password_reset_tokens: list["PasswordResetToken"] = Relationship(
        back_populates="utilizador"
    )
    codigos_backup: list["CodigoBackup2FA"] = Relationship(back_populates="utilizador")


# ---------------------------------------------------------------------------
# TokenRefresh — refresh tokens revoáveis lado-servidor
# ---------------------------------------------------------------------------

class TokenRefresh(SQLModel, table=True):
    """
    Refresh token opaco armazenado como hash SHA-256.
    Permite revogação lado-servidor (logout, sessões comprometidas).
    Um utilizador pode ter múltiplas sessões ativas em simultâneo.
    """

    __tablename__ = "refresh_tokens"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        index=True,
    )

    utilizador_id: uuid.UUID = Field(foreign_key="utilizadores.id", index=True)

    # Hash SHA-256 do token opaco (o token em texto limpo vai para o cookie httpOnly)
    token_hash: str = Field(max_length=64, unique=True, index=True)

    # Contexto da sessão (cifrado em repouso com PII_ENCRYPTION_KEY)
    ip_address: str | None = Field(default=None, max_length=200)
    user_agent: str | None = Field(default=None, max_length=700)

    expires_at: datetime = Field(index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Revogação — quando não None, o token foi invalidado (logout ou rotação)
    revogado_at: datetime | None = Field(default=None)

    # Relationship
    utilizador: Utilizador = Relationship(back_populates="refresh_tokens")


# ---------------------------------------------------------------------------
# PasswordResetToken — tokens single-use para reset de password
# ---------------------------------------------------------------------------

class PasswordResetToken(SQLModel, table=True):
    """
    Token de reset de password enviado por email.
    Single-use: marcado como usado após validação.
    Expira em 1 hora.
    """

    __tablename__ = "password_reset_tokens"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        index=True,
    )

    utilizador_id: uuid.UUID = Field(foreign_key="utilizadores.id", index=True)

    # Hash SHA-256 do token (o token em texto limpo vai no link de email)
    token_hash: str = Field(max_length=64, unique=True, index=True)

    # Contexto da origem do pedido (cifrado em repouso com PII_ENCRYPTION_KEY)
    ip_address: str | None = Field(default=None, max_length=200)

    expires_at: datetime = Field(index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Uso — quando não None, o token foi consumido
    usado_at: datetime | None = Field(default=None)

    # Relationship
    utilizador: Utilizador = Relationship(back_populates="password_reset_tokens")


# ---------------------------------------------------------------------------
# CodigoBackup2FA — backup codes para acesso quando 2FA não disponível
# ---------------------------------------------------------------------------

class CodigoBackup2FA(SQLModel, table=True):
    """
    Código de backup para 2FA.
    Gerados no momento de ativação do TOTP — 10 por utilizador.
    Formato: XXXX-XXXX-XXXX (mostrado ao utilizador UMA única vez).
    Armazenado como hash bcrypt (não reversível).
    """

    __tablename__ = "codigos_backup_2fa"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        index=True,
    )

    utilizador_id: uuid.UUID = Field(foreign_key="utilizadores.id", index=True)

    # Hash bcrypt do código (o código em texto limpo é mostrado ao utilizador uma vez)
    codigo_hash: str = Field(max_length=255)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Quando não None, este código já foi usado e não pode ser reutilizado
    usado_at: datetime | None = Field(default=None)

    # Relationship
    utilizador: Utilizador = Relationship(back_populates="codigos_backup")
