"""
Serviço de importação de frameworks a partir do framework.json + locales.

Recebe o conteúdo do ficheiro unificado framework.json (já dividido nas suas
partes pelo chamador: manifest, structure, profiles) mais os locales opcionais,
e popula a base de dados com a hierarquia completa.

Estratégia de upsert: usa code (ex: "ID.AM-01") + framework registry_id
como chave natural. UUIDs são gerados pelo serviço se o registo não existir.

Não depende de nenhum painel administrativo — pode ser usado pelo seed automático
on-prem (app.setup.seed_framework) e por outras integrações internas.
"""
import uuid
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.frameworks.models import (
    ComplianceProfile,
    Control,
    ControlLocale,
    Domain,
    DomainLocale,
    Framework,
    FrameworkLocale,
    MaturityLevel,
    MaturityLevelLocale,
    ProfileLocale,
    ProfileThreshold,
    SubRequirement,
    SubRequirementLocale,
    Subdomain,
    SubdomainLocale,
)


class FrameworkImportError(Exception):
    """Erro durante a importação de um framework."""


def importar_framework(
    db: Session,
    manifest: dict,
    structure: dict,
    profiles: dict | None,
    locales: dict[str, dict],
) -> dict:
    """
    Importa um framework completo a partir do conteúdo do framework.json unificado.

    Args:
        db: Sessão da base de dados.
        manifest: Metadados do framework (tudo excepto 'profiles' e 'domains').
        structure: Hierarquia de domínios/subdomínios/controlos ({"domains": [...]}).
        profiles: Perfis de conformidade ({"profiles": [...]}, opcional).
        locales: Dicionário {locale_code: conteúdo do ficheiro} ex: {"pt": {...}}.

    Returns:
        Dicionário com estatísticas da importação.
    """
    registry_id: str = manifest.get("registry_id", "")
    version: str = manifest.get("version", "")

    if not registry_id or not version:
        raise FrameworkImportError("manifest.json deve ter registry_id e version.")

    stats: dict[str, int] = {
        "frameworks": 0,
        "domains": 0,
        "subdomains": 0,
        "controls": 0,
        "sub_requirements": 0,
        "maturity_levels": 0,
        "compliance_profiles": 0,
        "profile_thresholds": 0,
        "locales_processados": 0,
    }

    # ------------------------------------------------------------------
    # 1. Upsert Framework
    # ------------------------------------------------------------------
    framework = db.exec(
        select(Framework).where(
            Framework.registry_id == registry_id,
            Framework.version == version,
        )
    ).first()

    if framework is None:
        framework = Framework(
            id=uuid.uuid4(),
            registry_id=registry_id,
            version=version,
            framework_type=manifest.get("framework_type", "control_based"),
            display_mode=manifest.get("display_mode", "maturity"),
            features=manifest.get("features"),
            maturity_scale_min=manifest.get("maturity_scale", {}).get("min", 1),
            maturity_scale_max=manifest.get("maturity_scale", {}).get("max", 5),
            maturity_radar_max=manifest.get("maturity_scale", {}).get("radar_max"),
            default_locale=manifest.get("default_locale", "pt"),
            issuer=manifest.get("issuer"),
            region=manifest.get("region"),
            tags=manifest.get("tags"),
            ativo=True,
        )

        # Se ainda não existir default na plataforma, este passa a ser o default.
        default_existente = db.exec(
            select(Framework).where(Framework.is_default == True)  # noqa: E712
        ).first()
        framework.is_default = default_existente is None

        db.add(framework)
        stats["frameworks"] += 1
    else:
        framework.framework_type = manifest.get("framework_type", "control_based")
        framework.display_mode = manifest.get("display_mode", "maturity")
        framework.features = manifest.get("features")
        framework.maturity_scale_min = manifest.get("maturity_scale", {}).get("min", 1)
        framework.maturity_scale_max = manifest.get("maturity_scale", {}).get("max", 5)
        framework.maturity_radar_max = manifest.get("maturity_scale", {}).get("radar_max")
        framework.default_locale = manifest.get("default_locale", "pt")
        framework.issuer = manifest.get("issuer")
        framework.region = manifest.get("region")
        framework.tags = manifest.get("tags")
        framework.updated_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(framework)

    # ------------------------------------------------------------------
    # 2. Processar structure.json — domains → subdomains → controls → sub_requirements
    # ------------------------------------------------------------------
    # Mapas code → DB object (para uso nas FKs)
    domain_map: dict[str, Domain] = {}
    subdomain_map: dict[str, Subdomain] = {}
    control_map: dict[str, Control] = {}
    sub_req_map: dict[str, SubRequirement] = {}

    domains_data: list[dict] = structure.get("domains", [])

    for d_order, domain_data in enumerate(domains_data):
        domain_code: str = domain_data["code"]

        domain = db.exec(
            select(Domain).where(
                Domain.framework_id == framework.id,
                Domain.code == domain_code,
            )
        ).first()

        if domain is None:
            domain = Domain(
                id=uuid.uuid4(),
                framework_id=framework.id,
                code=domain_code,
                order=d_order,
            )
            db.add(domain)
            stats["domains"] += 1
        else:
            domain.order = d_order

        db.commit()
        db.refresh(domain)
        domain_map[domain_code] = domain

        # Subdomains
        for sd_order, subdomain_data in enumerate(domain_data.get("subdomains", [])):
            subdomain_code: str = subdomain_data["code"]

            subdomain = db.exec(
                select(Subdomain).where(
                    Subdomain.domain_id == domain.id,
                    Subdomain.code == subdomain_code,
                )
            ).first()

            if subdomain is None:
                subdomain = Subdomain(
                    id=uuid.uuid4(),
                    domain_id=domain.id,
                    code=subdomain_code,
                    order=sd_order,
                )
                db.add(subdomain)
                stats["subdomains"] += 1
            else:
                subdomain.order = sd_order

            db.commit()
            db.refresh(subdomain)
            subdomain_map[subdomain_code] = subdomain

            # Controls
            for c_order, control_data in enumerate(subdomain_data.get("controls", [])):
                control_code: str = control_data["code"]
                criticality = control_data.get("criticality", "medium")
                applicability_tags = control_data.get("applicability_tags")

                control = db.exec(
                    select(Control).where(
                        Control.subdomain_id == subdomain.id,
                        Control.code == control_code,
                    )
                ).first()

                if control is None:
                    control = Control(
                        id=uuid.uuid4(),
                        subdomain_id=subdomain.id,
                        code=control_code,
                        criticality=criticality,
                        applicability_tags=applicability_tags,
                        order=c_order,
                    )
                    db.add(control)
                    stats["controls"] += 1
                else:
                    control.criticality = criticality
                    control.applicability_tags = applicability_tags
                    control.order = c_order

                db.commit()
                db.refresh(control)
                control_map[control_code] = control

                # Sub-requirements
                for sr_data in control_data.get("sub_requirements", []):
                    sr_code: str = sr_data["code"]
                    sr = db.exec(
                        select(SubRequirement).where(
                            SubRequirement.control_id == control.id,
                            SubRequirement.code == sr_code,
                        )
                    ).first()

                    if sr is None:
                        sr = SubRequirement(
                            id=uuid.uuid4(),
                            control_id=control.id,
                            code=sr_code,
                            maturity_level=sr_data.get("maturity_level", 1),
                            order=sr_data.get("order", 1),
                            mandatory=sr_data.get("mandatory", True),
                        )
                        db.add(sr)
                        stats["sub_requirements"] += 1
                    else:
                        sr.maturity_level = sr_data.get("maturity_level", 1)
                        sr.order = sr_data.get("order", 1)
                        sr.mandatory = sr_data.get("mandatory", True)

                    db.commit()
                    db.refresh(sr)
                    sub_req_map[sr_code] = sr

                # Maturity levels (estruturais — sem conteúdo)
                for ml_data in control_data.get("maturity_levels", []):
                    level_num: int = ml_data.get("level", 1)
                    ml = db.exec(
                        select(MaturityLevel).where(
                            MaturityLevel.control_id == control.id,
                            MaturityLevel.level == level_num,
                        )
                    ).first()

                    if ml is None:
                        ml = MaturityLevel(
                            id=uuid.uuid4(),
                            control_id=control.id,
                            level=level_num,
                        )
                        db.add(ml)
                        stats["maturity_levels"] += 1

                db.commit()

    # ------------------------------------------------------------------
    # 3. Processar profiles.json
    # ------------------------------------------------------------------
    if profiles:
        for profile_data in profiles.get("profiles", []):
            profile_code: str = profile_data["code"]

            profile = db.exec(
                select(ComplianceProfile).where(
                    ComplianceProfile.framework_id == framework.id,
                    ComplianceProfile.code == profile_code,
                )
            ).first()

            if profile is None:
                profile = ComplianceProfile(
                    id=uuid.uuid4(),
                    framework_id=framework.id,
                    code=profile_code,
                    compliance_mode=profile_data.get("compliance_mode", "maturity"),
                    auto_assign_for=profile_data.get("auto_assign_for"),
                )
                db.add(profile)
                stats["compliance_profiles"] += 1
            else:
                profile.compliance_mode = profile_data.get("compliance_mode", "maturity")
                profile.auto_assign_for = profile_data.get("auto_assign_for")

            db.commit()
            db.refresh(profile)

            # Thresholds
            for threshold_data in profile_data.get("thresholds", []):
                ctrl_code: str = threshold_data["control_code"]
                min_level: int = threshold_data.get("minimum_level", 1)

                if ctrl_code not in control_map:
                    # Tentar encontrar na DB pelo code
                    ctrl = db.exec(
                        select(Control).where(Control.code == ctrl_code)
                    ).first()
                    if ctrl is None:
                        continue
                    control_map[ctrl_code] = ctrl

                ctrl_obj = control_map[ctrl_code]

                threshold = db.exec(
                    select(ProfileThreshold).where(
                        ProfileThreshold.profile_id == profile.id,
                        ProfileThreshold.control_id == ctrl_obj.id,
                    )
                ).first()

                if threshold is None:
                    threshold = ProfileThreshold(
                        id=uuid.uuid4(),
                        profile_id=profile.id,
                        control_id=ctrl_obj.id,
                        minimum_level=min_level,
                    )
                    db.add(threshold)
                    stats["profile_thresholds"] += 1
                else:
                    threshold.minimum_level = min_level

            db.commit()

    # ------------------------------------------------------------------
    # 4. Processar ficheiros de locale
    # ------------------------------------------------------------------
    for locale_code, locale_data in locales.items():
        _processar_locale(
            db=db,
            locale_code=locale_code,
            locale_data=locale_data,
            framework=framework,
            domain_map=domain_map,
            subdomain_map=subdomain_map,
            control_map=control_map,
            sub_req_map=sub_req_map,
        )
        stats["locales_processados"] += 1

    return stats


def _processar_locale(
    db: Session,
    locale_code: str,
    locale_data: dict,
    framework: Framework,
    domain_map: dict[str, Domain],
    subdomain_map: dict[str, Subdomain],
    control_map: dict[str, Control],
    sub_req_map: dict[str, SubRequirement],
) -> None:
    """Upsert de todos os registos de locale para um código de idioma."""

    # Framework locale
    fw_locale_data = locale_data.get("framework", {})
    # maturity_level_names vive no top-level da locale, não dentro de "framework"
    maturity_level_names = locale_data.get("maturity_level_names")
    if fw_locale_data or maturity_level_names:
        fw_locale_data_merged = dict(fw_locale_data)
        if maturity_level_names:
            fw_locale_data_merged["maturity_level_names"] = maturity_level_names
        _upsert_framework_locale(db, framework, locale_code, fw_locale_data_merged)

    # Domain locales
    for domain_code, domain_locale in locale_data.get("domains", {}).items():
        if domain_code in domain_map:
            _upsert_domain_locale(db, domain_map[domain_code], locale_code, domain_locale)

    # Subdomain locales
    for subdomain_code, subdomain_locale in locale_data.get("subdomains", {}).items():
        if subdomain_code in subdomain_map:
            _upsert_subdomain_locale(db, subdomain_map[subdomain_code], locale_code, subdomain_locale)

    # Control locales + sub-requirement locales + maturity level locales
    controls_locale = locale_data.get("controls", {})
    for control_code, control_locale in controls_locale.items():
        if control_code not in control_map:
            continue

        ctrl = control_map[control_code]
        _upsert_control_locale(db, ctrl, locale_code, control_locale)

        # Sub-requirement locales
        for sr_code, sr_locale in control_locale.get("sub_requirements", {}).items():
            if sr_code in sub_req_map:
                _upsert_sub_requirement_locale(db, sub_req_map[sr_code], locale_code, sr_locale)

        # Maturity level locales
        maturity_levels_locale = control_locale.get("maturity_levels", {})
        for level_str, ml_locale in maturity_levels_locale.items():
            try:
                level_num = int(level_str)
            except ValueError:
                continue

            ml = db.exec(
                select(MaturityLevel).where(
                    MaturityLevel.control_id == ctrl.id,
                    MaturityLevel.level == level_num,
                )
            ).first()

            if ml is not None:
                _upsert_maturity_level_locale(db, ml, locale_code, ml_locale)

    # Profile locales
    profiles_locale = locale_data.get("profiles", {})
    for profile_code, profile_locale in profiles_locale.items():
        profile = db.exec(
            select(ComplianceProfile).where(
                ComplianceProfile.framework_id == framework.id,
                ComplianceProfile.code == profile_code,
            )
        ).first()

        if profile is not None:
            _upsert_profile_locale(db, profile, locale_code, profile_locale)

    db.commit()


# ---------------------------------------------------------------------------
# Helpers de upsert por entidade + locale
# ---------------------------------------------------------------------------

def _upsert_framework_locale(
    db: Session, framework: Framework, locale: str, data: dict
) -> None:
    obj = db.exec(
        select(FrameworkLocale).where(
            FrameworkLocale.framework_id == framework.id,
            FrameworkLocale.locale == locale,
        )
    ).first()
    if obj is None:
        db.add(FrameworkLocale(
            id=uuid.uuid4(),
            framework_id=framework.id,
            locale=locale,
            name=data.get("name", ""),
            description=data.get("description", ""),
            maturity_level_names=data.get("maturity_level_names"),
        ))
    else:
        obj.name = data.get("name", obj.name)
        obj.description = data.get("description", obj.description)
        if "maturity_level_names" in data:
            obj.maturity_level_names = data["maturity_level_names"]


def _upsert_domain_locale(
    db: Session, domain: Domain, locale: str, data: dict
) -> None:
    obj = db.exec(
        select(DomainLocale).where(
            DomainLocale.domain_id == domain.id,
            DomainLocale.locale == locale,
        )
    ).first()
    if obj is None:
        db.add(DomainLocale(
            id=uuid.uuid4(),
            domain_id=domain.id,
            locale=locale,
            name=data.get("name", ""),
            description=data.get("description", ""),
        ))
    else:
        obj.name = data.get("name", obj.name)
        obj.description = data.get("description", obj.description)


def _upsert_subdomain_locale(
    db: Session, subdomain: Subdomain, locale: str, data: dict
) -> None:
    obj = db.exec(
        select(SubdomainLocale).where(
            SubdomainLocale.subdomain_id == subdomain.id,
            SubdomainLocale.locale == locale,
        )
    ).first()
    if obj is None:
        db.add(SubdomainLocale(
            id=uuid.uuid4(),
            subdomain_id=subdomain.id,
            locale=locale,
            name=data.get("name", ""),
            description=data.get("description", ""),
        ))
    else:
        obj.name = data.get("name", obj.name)
        obj.description = data.get("description", obj.description)


def _upsert_control_locale(
    db: Session, control: Control, locale: str, data: dict
) -> None:
    obj = db.exec(
        select(ControlLocale).where(
            ControlLocale.control_id == control.id,
            ControlLocale.locale == locale,
        )
    ).first()
    if obj is None:
        db.add(ControlLocale(
            id=uuid.uuid4(),
            control_id=control.id,
            locale=locale,
            title=data.get("title", ""),
            description=data.get("description", ""),
            implementation_guide=data.get("implementation_guide"),
            evidence_examples=data.get("evidence_examples"),
        ))
    else:
        obj.title = data.get("title", obj.title)
        obj.description = data.get("description", obj.description)
        if "implementation_guide" in data:
            obj.implementation_guide = data.get("implementation_guide")
        elif "maturity_levels" in data:
            obj.implementation_guide = None

        if "evidence_examples" in data:
            obj.evidence_examples = data.get("evidence_examples")
        elif "maturity_levels" in data:
            obj.evidence_examples = None


def _upsert_sub_requirement_locale(
    db: Session, sr: SubRequirement, locale: str, data: dict
) -> None:
    obj = db.exec(
        select(SubRequirementLocale).where(
            SubRequirementLocale.sub_requirement_id == sr.id,
            SubRequirementLocale.locale == locale,
        )
    ).first()
    if obj is None:
        db.add(SubRequirementLocale(
            id=uuid.uuid4(),
            sub_requirement_id=sr.id,
            locale=locale,
            description=data.get("description", ""),
        ))
    else:
        obj.description = data.get("description", obj.description)


def _upsert_maturity_level_locale(
    db: Session, ml: MaturityLevel, locale: str, data: dict
) -> None:
    evidence_examples = data.get("evidence")
    if evidence_examples is None:
        evidence_examples = data.get("evidence_examples")

    obj = db.exec(
        select(MaturityLevelLocale).where(
            MaturityLevelLocale.maturity_level_id == ml.id,
            MaturityLevelLocale.locale == locale,
        )
    ).first()
    if obj is None:
        db.add(MaturityLevelLocale(
            id=uuid.uuid4(),
            maturity_level_id=ml.id,
            locale=locale,
            description=data.get("description", ""),
            indicators=data.get("indicators"),
            evidence_examples=evidence_examples,
            tip=data.get("tip"),
            effort=data.get("effort"),
        ))
    else:
        obj.description = data.get("description", obj.description)
        obj.indicators = data.get("indicators", obj.indicators)
        obj.evidence_examples = evidence_examples
        obj.tip = data.get("tip", obj.tip)
        obj.effort = data.get("effort", obj.effort)


def _upsert_profile_locale(
    db: Session, profile: ComplianceProfile, locale: str, data: dict
) -> None:
    obj = db.exec(
        select(ProfileLocale).where(
            ProfileLocale.profile_id == profile.id,
            ProfileLocale.locale == locale,
        )
    ).first()
    if obj is None:
        db.add(ProfileLocale(
            id=uuid.uuid4(),
            profile_id=profile.id,
            locale=locale,
            name=data.get("name", ""),
            description=data.get("description", ""),
        ))
    else:
        obj.name = data.get("name", obj.name)
        obj.description = data.get("description", obj.description)
