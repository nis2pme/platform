"""
Modelos SQLModel do módulo de frameworks de conformidade.

Hierarquia:
  Framework
    └── Domain
          └── Subdomain
                └── Control
                      └── SubRequirement   (checks verificáveis, com nível de maturidade)
                      └── MaturityLevel    (descrição narrativa de cada nível 1-5)

Conteúdo textual em tabelas *_locale separadas (separação estrutura/conteúdo).
ComplianceProfile + ProfileThreshold — define os thresholds mínimos por controlo.

As tabelas *_locale têm sempre (parent_id, locale) como par único.
"""
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import Column, JSON, Text, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel

from app.shared.enums import EstadoControlo


# ---------------------------------------------------------------------------
# Framework
# ---------------------------------------------------------------------------

class Framework(SQLModel, table=True):
    """
    Registo de um framework importado via JSON (manifest.json).
    registry_id é o identificador público (ex: 'cyfun-2025').
    version permite múltiplas versões do mesmo framework; ativo=True = versão corrente.
    """

    __tablename__ = "frameworks"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    registry_id: str = Field(max_length=100, index=True)   # ex: "cyfun-2025"
    version: str = Field(max_length=50)                    # ex: "2025.1"
    framework_type: str = Field(max_length=50)             # "control_based"
    maturity_scale_min: int = Field(default=1)
    maturity_scale_max: int = Field(default=5)
    maturity_radar_max: Optional[int] = Field(default=None)  # Escala visual do radar; None = usar maturity_scale_max
    display_mode: str = Field(default="maturity", max_length=50)
    features: Optional[dict] = Field(
        default=None, sa_column=Column("features", JSON, nullable=True)
    )
    default_locale: str = Field(default="pt", max_length=10)
    issuer: Optional[str] = Field(default=None, max_length=255)
    region: Optional[str] = Field(default=None, max_length=100)
    tags: Optional[list] = Field(
        default=None, sa_column=Column("tags_json", JSON, nullable=True)
    )
    ativo: bool = Field(default=True)
    is_default: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("registry_id", "version", name="uq_framework_registry_version"),
    )

    locales: List["FrameworkLocale"] = Relationship(back_populates="framework")
    domains: List["Domain"] = Relationship(back_populates="framework")
    profiles: List["ComplianceProfile"] = Relationship(back_populates="framework")


class FrameworkLocale(SQLModel, table=True):
    """Conteúdo textual de uma framework num locale específico."""

    __tablename__ = "framework_locales"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    framework_id: uuid.UUID = Field(foreign_key="frameworks.id", index=True)
    locale: str = Field(max_length=10)
    name: str = Field(max_length=255)
    description: str = Field(default="", sa_column=Column(Text))
    maturity_level_names: Optional[dict] = Field(
        default=None, sa_column=Column("maturity_level_names", JSON, nullable=True)
    )

    __table_args__ = (
        UniqueConstraint("framework_id", "locale", name="uq_framework_locale"),
    )

    framework: Optional["Framework"] = Relationship(back_populates="locales")


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------

class Domain(SQLModel, table=True):
    """Domínio de um framework (ex: ID, PR, DE, RS, RC no CyFun)."""

    __tablename__ = "domains"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    framework_id: uuid.UUID = Field(foreign_key="frameworks.id", index=True)
    code: str = Field(max_length=20, index=True)   # ex: "ID"
    order: int = Field(default=0)

    __table_args__ = (
        UniqueConstraint("framework_id", "code", name="uq_domain_framework_code"),
    )

    framework: Optional["Framework"] = Relationship(back_populates="domains")
    locales: List["DomainLocale"] = Relationship(back_populates="domain")
    subdomains: List["Subdomain"] = Relationship(back_populates="domain")


class DomainLocale(SQLModel, table=True):
    """Conteúdo textual de um domínio num locale específico."""

    __tablename__ = "domain_locales"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    domain_id: uuid.UUID = Field(foreign_key="domains.id", index=True)
    locale: str = Field(max_length=10)
    name: str = Field(max_length=255)
    description: str = Field(default="", sa_column=Column(Text))

    __table_args__ = (
        UniqueConstraint("domain_id", "locale", name="uq_domain_locale"),
    )

    domain: Optional["Domain"] = Relationship(back_populates="locales")


# ---------------------------------------------------------------------------
# Subdomain
# ---------------------------------------------------------------------------

class Subdomain(SQLModel, table=True):
    """Subdomínio dentro de um domínio (ex: ID.AM, ID.GV, PR.AC)."""

    __tablename__ = "subdomains"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    domain_id: uuid.UUID = Field(foreign_key="domains.id", index=True)
    code: str = Field(max_length=20, index=True)   # ex: "ID.AM"
    order: int = Field(default=0)

    __table_args__ = (
        UniqueConstraint("domain_id", "code", name="uq_subdomain_domain_code"),
    )

    domain: Optional["Domain"] = Relationship(back_populates="subdomains")
    locales: List["SubdomainLocale"] = Relationship(back_populates="subdomain")
    controls: List["Control"] = Relationship(back_populates="subdomain")


class SubdomainLocale(SQLModel, table=True):
    """Conteúdo textual de um subdomínio num locale específico."""

    __tablename__ = "subdomain_locales"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    subdomain_id: uuid.UUID = Field(foreign_key="subdomains.id", index=True)
    locale: str = Field(max_length=10)
    name: str = Field(max_length=255)
    description: str = Field(default="", sa_column=Column(Text))

    __table_args__ = (
        UniqueConstraint("subdomain_id", "locale", name="uq_subdomain_locale"),
    )

    subdomain: Optional["Subdomain"] = Relationship(back_populates="locales")


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------

class Control(SQLModel, table=True):
    """
    Medida/controlo de segurança dentro de um subdomínio.
    criticality:
      "critical" — bloqueia o score global se abaixo do mínimo
      "high"     — penaliza mas não bloqueia
      "medium" / "low" — comportamento normal
    """

    __tablename__ = "controls"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    subdomain_id: uuid.UUID = Field(foreign_key="subdomains.id", index=True)
    code: str = Field(max_length=30, index=True)    # ex: "ID.AM-01"
    criticality: str = Field(default="medium", max_length=20)
    applicability_tags: Optional[list] = Field(
        default=None,
        sa_column=Column("applicability_tags_json", JSON, nullable=True),
    )
    order: int = Field(default=0)

    __table_args__ = (
        UniqueConstraint("subdomain_id", "code", name="uq_control_subdomain_code"),
    )

    subdomain: Optional["Subdomain"] = Relationship(back_populates="controls")
    locales: List["ControlLocale"] = Relationship(back_populates="control")
    sub_requirements: List["SubRequirement"] = Relationship(back_populates="control")
    maturity_levels: List["MaturityLevel"] = Relationship(back_populates="control")
    controlos_empresa: List["ControloEmpresaV2"] = Relationship(back_populates="control")
    thresholds: List["ProfileThreshold"] = Relationship(back_populates="control")


class ControlLocale(SQLModel, table=True):
    """Conteúdo textual de um controlo num locale específico."""

    __tablename__ = "control_locales"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    control_id: uuid.UUID = Field(foreign_key="controls.id", index=True)
    locale: str = Field(max_length=10)
    title: str = Field(max_length=255)
    description: str = Field(default="", sa_column=Column(Text))
    implementation_guide: Optional[list] = Field(
        default=None,
        sa_column=Column("implementation_guide_json", JSON, nullable=True),
    )
    evidence_examples: Optional[list] = Field(
        default=None,
        sa_column=Column("evidence_examples_json", JSON, nullable=True),
    )

    __table_args__ = (
        UniqueConstraint("control_id", "locale", name="uq_control_locale"),
    )

    control: Optional["Control"] = Relationship(back_populates="locales")


# ---------------------------------------------------------------------------
# SubRequirement  (checks verificáveis por nível de maturidade)
# ---------------------------------------------------------------------------

class SubRequirement(SQLModel, table=True):
    """
    Item verificável dentro de um controlo, associado a um nível de maturidade.
    mandatory=True bloqueia progressão para o nível seguinte se não concluído.
    """

    __tablename__ = "sub_requirements"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    control_id: uuid.UUID = Field(foreign_key="controls.id", index=True)
    code: str = Field(max_length=50, index=True)    # ex: "ID.AM-01.SR-001"
    maturity_level: int = Field(ge=1, le=5)         # a que nível pertence
    order: int = Field(ge=1, le=10)                 # ordem dentro do nível
    mandatory: bool = Field(default=True)

    __table_args__ = (
        UniqueConstraint("control_id", "code", name="uq_sub_requirement_control_code"),
    )

    control: Optional["Control"] = Relationship(back_populates="sub_requirements")
    locales: List["SubRequirementLocale"] = Relationship(back_populates="sub_requirement")
    empresa_checks: List["ControloEmpresaCheckV2"] = Relationship(
        back_populates="sub_requirement"
    )


class SubRequirementLocale(SQLModel, table=True):
    """Conteúdo textual de um sub-requisito num locale específico."""

    __tablename__ = "sub_requirement_locales"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    sub_requirement_id: uuid.UUID = Field(foreign_key="sub_requirements.id", index=True)
    locale: str = Field(max_length=10)
    description: str = Field(sa_column=Column(Text))

    __table_args__ = (
        UniqueConstraint("sub_requirement_id", "locale", name="uq_sub_requirement_locale"),
    )

    sub_requirement: Optional["SubRequirement"] = Relationship(back_populates="locales")


# ---------------------------------------------------------------------------
# MaturityLevel  (descrição narrativa de cada nível 1-5 por controlo)
# ---------------------------------------------------------------------------

class MaturityLevel(SQLModel, table=True):
    """
    Descrição narrativa do que significa estar no nível N de um controlo.
    Puramente informativa — não afeta o scoring (determinado pelos SubRequirements).
    """

    __tablename__ = "maturity_levels"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    control_id: uuid.UUID = Field(foreign_key="controls.id", index=True)
    level: int = Field(ge=1, le=5)

    __table_args__ = (
        UniqueConstraint("control_id", "level", name="uq_maturity_level_control_level"),
    )

    control: Optional["Control"] = Relationship(back_populates="maturity_levels")
    locales: List["MaturityLevelLocale"] = Relationship(back_populates="maturity_level")


class MaturityLevelLocale(SQLModel, table=True):
    """Conteúdo textual de um nível de maturidade num locale específico."""

    __tablename__ = "maturity_level_locales"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    maturity_level_id: uuid.UUID = Field(foreign_key="maturity_levels.id", index=True)
    locale: str = Field(max_length=10)
    description: str = Field(sa_column=Column(Text))
    indicators: Optional[list] = Field(
        default=None,
        sa_column=Column("indicators_json", JSON, nullable=True),
    )
    evidence_examples: Optional[list] = Field(
        default=None,
        sa_column=Column("evidence_examples_json", JSON, nullable=True),
    )
    tip: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )
    effort: Optional[str] = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
    )

    __table_args__ = (
        UniqueConstraint("maturity_level_id", "locale", name="uq_maturity_level_locale"),
    )

    maturity_level: Optional["MaturityLevel"] = Relationship(back_populates="locales")


# ---------------------------------------------------------------------------
# ComplianceProfile + ProfileLocale + ProfileThreshold
# ---------------------------------------------------------------------------

class ComplianceProfile(SQLModel, table=True):
    """
    Perfil de conformidade que define os thresholds mínimos de cada controlo.
    auto_assign_for: tipo_entidade que auto-associa este perfil (ex: "essencial").
    """

    __tablename__ = "compliance_profiles"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    framework_id: uuid.UUID = Field(foreign_key="frameworks.id", index=True)
    code: str = Field(max_length=100, index=True)
    compliance_mode: str = Field(default="maturity", max_length=50)
    auto_assign_for: Optional[str] = Field(default=None, max_length=50)

    __table_args__ = (
        UniqueConstraint("framework_id", "code", name="uq_compliance_profile_code"),
    )

    framework: Optional["Framework"] = Relationship(back_populates="profiles")
    locales: List["ProfileLocale"] = Relationship(back_populates="profile")
    thresholds: List["ProfileThreshold"] = Relationship(back_populates="profile")


class ProfileLocale(SQLModel, table=True):
    """Conteúdo textual de um perfil de conformidade num locale específico."""

    __tablename__ = "profile_locales"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    profile_id: uuid.UUID = Field(foreign_key="compliance_profiles.id", index=True)
    locale: str = Field(max_length=10)
    name: str = Field(max_length=255)
    description: str = Field(default="", sa_column=Column(Text))

    __table_args__ = (
        UniqueConstraint("profile_id", "locale", name="uq_profile_locale"),
    )

    profile: Optional["ComplianceProfile"] = Relationship(back_populates="locales")


class ProfileThreshold(SQLModel, table=True):
    """Nível mínimo de maturidade exigido por um perfil para um controlo específico."""

    __tablename__ = "profile_thresholds"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    profile_id: uuid.UUID = Field(foreign_key="compliance_profiles.id", index=True)
    control_id: uuid.UUID = Field(foreign_key="controls.id", index=True)
    minimum_level: int = Field(ge=1, le=5)

    __table_args__ = (
        UniqueConstraint("profile_id", "control_id", name="uq_profile_threshold"),
    )

    profile: Optional["ComplianceProfile"] = Relationship(back_populates="thresholds")
    control: Optional["Control"] = Relationship(back_populates="thresholds")


# ---------------------------------------------------------------------------
# ControloEmpresaV2  (estado de um controlo por empresa)
# ---------------------------------------------------------------------------

class ControloEmpresaV2(SQLModel, table=True):
    """
    Estado de implementação de um Control para uma empresa específica.
    Versão nova que referencia Control (nova tabela) e tem framework_id.
    """

    __tablename__ = "controlos_empresa_v2"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)
    framework_id: uuid.UUID = Field(foreign_key="frameworks.id", index=True)
    control_id: uuid.UUID = Field(foreign_key="controls.id", index=True)

    estado: EstadoControlo = Field(default=EstadoControlo.NAO_INICIADO)
    nivel_maturidade_atual: int = Field(default=0)

    implementador_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="utilizadores.id", nullable=True, index=True
    )
    aprovado_por_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="utilizadores.id", nullable=True
    )
    data_aprovacao: Optional[datetime] = Field(default=None)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    control: Optional["Control"] = Relationship(back_populates="controlos_empresa")
    checks_empresa: List["ControloEmpresaCheckV2"] = Relationship(
        back_populates="controlo_empresa"
    )


class ControloEmpresaCheckV2(SQLModel, table=True):
    """
    Registo de quais sub-requisitos uma empresa já concluiu.
    Versão nova que referencia SubRequirement em vez de ControloNivelCheck.
    """

    __tablename__ = "controlo_empresa_checks_v2"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    controlo_empresa_id: uuid.UUID = Field(
        foreign_key="controlos_empresa_v2.id", index=True
    )
    sub_requirement_id: uuid.UUID = Field(
        foreign_key="sub_requirements.id", index=True
    )
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    concluido: bool = Field(default=False)
    concluido_por_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="utilizadores.id", nullable=True
    )
    concluido_at: Optional[datetime] = Field(default=None)

    controlo_empresa: Optional["ControloEmpresaV2"] = Relationship(
        back_populates="checks_empresa"
    )
    sub_requirement: Optional["SubRequirement"] = Relationship(
        back_populates="empresa_checks"
    )
