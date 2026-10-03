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
from typing import TYPE_CHECKING, Optional

import sqlalchemy as sa
from sqlalchemy import Column
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:  # só para as anotações da relação; evita o import circular
    from app.empresas.models import Empresa


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

class UtilizadorEmpresa(SQLModel, table=True):
    """Adesão de um utilizador a uma empresa — **só schema, por agora**.

    `Utilizador.empresa_id` é uma coluna: um utilizador pertence a exatamente uma
    empresa. Quem presta serviço de IT a 20 PME precisa de 20 contas, 20
    palavras-passe e 20 enrolamentos de 2FA — o que não é fricção, é um travão ao
    canal de distribuição, que para a PME é precisamente o prestador de IT.

    **O prestador de IT entra como utilizador interno da empresa que serve**, com
    os papéis que já existem e o âmbito que a matriz de capacidades já sabe dar.
    Não há papéis novos: quem avalia de fora não tem conta nenhuma aqui — usa a
    plataforma do auditor e recebe um dossiê `.nis2pme`. O que falta é só uma
    identidade poder aderir a **várias** empresas.

    `Utilizador.empresa_id` **mantém-se** como a empresa ativa da sessão, e nada a
    jusante muda: todos os filtros por `empresa_id` continuam exatamente como
    estão, e a UI pode continuar a permitir uma empresa só. O que se ganha agora é
    não ficar fechado fora do modelo — depois custa mexer na autenticação, nos
    claims do JWT, no RBAC, em todos os filtros de consulta e na UI, com clientes
    em produção e sessões vivas.
    """

    __tablename__ = "utilizador_empresa"

    utilizador_id: uuid.UUID = Field(foreign_key="utilizadores.id", primary_key=True)
    empresa_id: uuid.UUID = Field(
        foreign_key="empresas.id", primary_key=True, index=True
    )

    # O papel é POR EMPRESA: a mesma pessoa pode ser admin no seu cliente A e um
    # utilizador comum no cliente B. Guardado como texto pelo mesmo motivo do
    # histórico de controlos — um tipo enumerado na base obrigaria a uma migração
    # de tipo por cada papel novo, e a validação vive no modelo.
    papel: str = Field(max_length=30)

    # Adesão com prazo, para acessos temporários. NULL = sem validade.
    valido_ate: Optional[datetime] = Field(default=None)

    criado_por_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.UUID(as_uuid=True), nullable=True),
    )
    criado_em: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def registar_adesao(db, utilizador, *, criado_por_id=None) -> "UtilizadorEmpresa":
    """Escreve a adesão de um utilizador à empresa dele.

    Chamado em TODOS os caminhos que criam um utilizador. Sem isto a tabela fica
    **meio verdadeira** — os utilizadores anteriores à migração têm linha (foram
    retomados) e os criados depois não —, e no dia em que alguém a passar a ler
    obtém a resposta errada para os mais recentes, em silêncio. Uma tabela vazia
    é honesta; uma tabela pela metade mente.

    Idempotente: repetir não duplica (a chave primária é o par).
    """
    from sqlmodel import select as _select

    existente = db.exec(
        _select(UtilizadorEmpresa).where(
            UtilizadorEmpresa.utilizador_id == utilizador.id,
            UtilizadorEmpresa.empresa_id == utilizador.empresa_id,
        )
    ).first()
    if existente is not None:
        return existente
    adesao = UtilizadorEmpresa(
        utilizador_id=utilizador.id,
        empresa_id=utilizador.empresa_id,
        papel=getattr(utilizador.role, "value", utilizador.role),
        criado_por_id=criado_por_id,
    )
    db.add(adesao)
    return adesao


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

    # Bloqueio de conta — defesa anti-brute-force por conta (complementa o
    # rate-limit por IP). Contador de falhas dentro de uma janela deslizante
    # (tentativas_janela_inicio); ao atingir o limite a conta fica bloqueada até
    # bloqueado_ate (auto-expira). Tudo zerado no login válido.
    tentativas_falhadas: int = Field(default=0)
    # O instante é gravado com fuso — a app trabalha em UTC e a coluna tem
    # de dizer o mesmo, senão a data sai escrita de duas maneiras conforme
    # a instalação ser nova ou atualizada.
    tentativas_janela_inicio: datetime | None = Field(
        default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True)
    )
    bloqueado_ate: datetime | None = Field(
        default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True)
    )

    # 2FA — TOTP
    # O segredo TOTP é cifrado com Fernet antes de ser guardado (ISO27001 cifra em repouso)
    totp_secret_cifrado: str | None = Field(default=None)
    totp_ativo: bool = Field(default=False)
    # Último passo de 30 s aceite: um código já usado não volta a entrar.
    totp_ultimo_passo: int | None = Field(
        default=None, sa_column=Column(sa.BigInteger, nullable=True)
    )

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
    Armazenado como hash argon2id (não reversível).
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


# ---------------------------------------------------------------------------
# BloqueioIP — acumulador de falhas de login por IP (anti password-spray)
# ---------------------------------------------------------------------------

class BloqueioIP(SQLModel, table=True):
    """
    Contagem de falhas de login por IP, entre TODAS as contas (e contra emails
    inexistentes), numa janela deslizante. Ao atingir o limiar o IP fica
    bloqueado temporariamente. Uma linha por IP distinto (upsert por ip_hash).

    O IP é guardado apenas como hash SHA-256 (não reversível) — minimização de
    dados: a tabela não permite reconstruir o endereço.
    """

    __tablename__ = "login_bloqueios_ip"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        index=True,
    )

    # Hash SHA-256 do IP (hex, 64 chars) — uma linha por IP.
    ip_hash: str = Field(max_length=64, unique=True, index=True)

    # Falhas na janela atual e início da janela deslizante.
    contador: int = Field(default=0)
    janela_inicio: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False),
    )

    # Quando não None e no futuro, o IP está bloqueado.
    bloqueado_ate: datetime | None = Field(
        default=None, sa_column=Column(sa.DateTime(timezone=True), nullable=True)
    )

    atualizado_em: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(sa.DateTime(timezone=True), nullable=False),
    )
