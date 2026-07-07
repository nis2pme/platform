"""
Modelos SQLModel do módulo de empresas (tenants).
Empresa é a raiz do isolamento multi-tenant — todas as outras
entidades têm empresa_id como FK obrigatória.
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import Column, JSON
from sqlmodel import Field, Relationship, SQLModel


class DimensaoEmpresa(str, Enum):
    """
    Dimensão da empresa segundo a definição europeia de PME.
    Determina obrigações de conformidade NIS2.
    """

    MICRO = "micro"          # < 10 trabalhadores, < 2 M€ vol. negócios
    PEQUENA = "pequena"      # < 50 trabalhadores, < 10 M€
    MEDIA = "media"          # < 250 trabalhadores, < 50 M€
    GRANDE = "grande"        # >= 250 trabalhadores ou >= 50 M€


class TipoEntidade(str, Enum):
    """
    Classificação ao abrigo do DL 125/2025 (transposição NIS2).
    Classificação legal — depende do setor e dimensão da empresa.
    """

    BASE = "base"              # micro/PME sem obrigações NIS2 explícitas
    IMPORTANTE = "importante"  # Anexo II DL 125/2025
    ESSENCIAL = "essencial"    # Anexo I DL 125/2025 — requisitos mais exigentes


class NivelQNRCS(str, Enum):
    """
    Nível de conformidade QNRCS escolhido pela empresa.
    Determina os thresholds mínimos exigidos para cada controlo.
    Separado da classificação NIS2 — a empresa pode escolher um nível
    acima do mínimo legal obrigatório.

    Nota: nomes em minúsculas para coincidir com os valores guardados na BD
    (coluna VARCHAR — SQLAlchemy usa .name como chave de lookup).
    """

    basico = "basico"            # Nível mínimo — corresponde a entidade base
    substancial = "substancial"  # Nível intermédio — corresponde a entidade importante
    elevado = "elevado"          # Nível máximo — corresponde a entidade essencial


class Empresa(SQLModel, table=True):
    """
    Tenant raiz da plataforma. Cada PME cliente tem um registo aqui.
    Toda a query de dados deve filtrar por empresa_id do utilizador autenticado.
    """

    __tablename__ = "empresas"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        index=True,
    )
    nome: str = Field(max_length=500)  # cifrado em repouso — tamanho aumentado para token Fernet
    nif: str | None = Field(default=None, max_length=500)  # cifrado em repouso
    email: str | None = Field(default=None, max_length=500)  # cifrado em repouso
    website: str | None = Field(default=None, max_length=500)  # cifrado em repouso

    # Classificação NIS2
    setor: str | None = Field(default=None, max_length=100)
    dimensao: DimensaoEmpresa | None = Field(default=None)
    tipo_entidade: TipoEntidade = Field(default=TipoEntidade.BASE)

    # Nível QNRCS — escolhido pela empresa; determina os thresholds mínimos.
    # None → derivado automaticamente de tipo_entidade (fallback de retrocompatibilidade).
    nivel_qnrcs: NivelQNRCS | None = Field(default=None)

    # Estado da conta
    ativo: bool = Field(default=True)
    suspenso: bool = Field(default=False)   # suspensão administrativa (mecanismo multi-tenant)
    onboarding_completo: bool = Field(default=False)

    # Framework de conformidade ativo (V2). Quando definido, o tenant usa a camada V2.
    # Herdado automaticamente do framework com is_default=True no momento do registo.
    framework_id: uuid.UUID | None = Field(
        default=None, foreign_key="frameworks.id", nullable=True, index=True
    )

    # Locale preferido — usado em todas as queries de conteúdo textual dos frameworks
    locale_preferido: str = Field(default="pt", max_length=10)

    # Timestamps — geridos automaticamente
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deleted_at: datetime | None = Field(default=None)  # soft delete

    # Configurações da empresa — guardadas como JSON
    # config_seguranca: preferências de segurança do tenant (2FA obrigatório, sessão, etc.)
    # config_notificacoes: preferências de notificações por email do tenant
    config_seguranca: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON)
    )
    config_notificacoes: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON)
    )

    # Relationships (declaradas aqui para referência; evita imports circulares)
    utilizadores: list["Utilizador"] = Relationship(back_populates="empresa")  # type: ignore[name-defined]
