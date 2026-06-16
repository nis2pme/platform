from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.empresas.models import Empresa
from app.frameworks.models import (
    ComplianceProfile,
    Control,
    ControloEmpresaV2,
    Domain,
    Framework,
    ProfileThreshold,
    Subdomain,
)

LocaleModel = TypeVar("LocaleModel")

PREFERRED_DEFAULT_FRAMEWORKS = ("qnrcs-2026", "cyfun-2025")

# Cache do framework padrão: evita queries repetidas em endpoints de alta frequência.
# Guarda apenas o UUID — cada request ainda carrega o objeto via db.get() (lookup por PK).
_framework_cache: dict[str, tuple[uuid.UUID, float]] = {}
_FRAMEWORK_CACHE_TTL = 900  # 15 minutos


def invalidar_cache_framework() -> None:
    """Invalida o cache do framework padrão. Chamar após alterações via superadmin."""
    _framework_cache.clear()


@dataclass(frozen=True)
class ControlHierarchyRow:
    ce: ControloEmpresaV2
    control: Control
    subdomain: Subdomain
    domain: Domain


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _framework_directories(frameworks_root: Path) -> list[Path]:
    if not frameworks_root.exists():
        return []

    ordered: list[Path] = []
    seen: set[Path] = set()
    for registry_id in PREFERRED_DEFAULT_FRAMEWORKS:
        path = frameworks_root / registry_id
        if path.is_dir():
            ordered.append(path)
            seen.add(path)

    for path in sorted(frameworks_root.iterdir(), key=lambda item: item.name.lower()):
        if path.is_dir() and path not in seen:
            ordered.append(path)
    return ordered


def _load_framework_bundle(
    framework_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None, dict[str, dict[str, Any]]]:
    """Carrega o bundle de um framework a partir de framework.json + locales/*.json."""
    fw_path = framework_dir / "framework.json"
    if not fw_path.exists():
        raise FileNotFoundError(
            f"framework.json não encontrado em {framework_dir}. "
            "O formato legado (manifest.json + structure.json + profiles.json) "
            "já não é suportado."
        )
    data = json.loads(fw_path.read_text(encoding="utf-8"))

    # Separa os blocos internos do ficheiro unificado
    manifest = {k: v for k, v in data.items() if k not in ("profiles", "domains")}
    structure = {"domains": data.get("domains", [])}
    profiles: dict[str, Any] | None = (
        {"profiles": data["profiles"]} if "profiles" in data else None
    )

    locales: dict[str, dict[str, Any]] = {}
    locales_dir = framework_dir / "locales"
    if locales_dir.exists():
        for locale_file in sorted(locales_dir.glob("*.json")):
            locales[locale_file.stem] = json.loads(
                locale_file.read_text(encoding="utf-8")
            )

    return manifest, structure, profiles, locales


def _ensure_default_flag(db: Session) -> Framework | None:
    ativos = db.exec(
        select(Framework).where(Framework.ativo.is_(True))
    ).all()
    if not ativos:
        return None

    atual = next((framework for framework in ativos if framework.is_default), None)
    if atual:
        return atual

    preferred = next(
        (
            framework
            for registry_id in PREFERRED_DEFAULT_FRAMEWORKS
            for framework in ativos
            if framework.registry_id == registry_id
        ),
        ativos[0],
    )

    for framework in ativos:
        framework.is_default = framework.id == preferred.id
        db.add(framework)
    db.commit()
    db.refresh(preferred)
    return preferred


def resolver_framework_empresa(db: Session, empresa: Empresa) -> Framework:
    """Devolve o framework activo da empresa, atribuindo o default se necessário.

    Não inicializa controlos — responsabilidade do chamador quando aplicável.
    Ponto único de resolução partilhado pela camada de controlos e evidências.
    """
    framework = db.get(Framework, empresa.framework_id) if empresa.framework_id else None
    if framework and framework.ativo:
        return framework

    default = get_default_framework(db)
    if not default:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Nenhum framework ativo disponível. Importe um framework no superadmin.",
        )

    empresa.framework_id = default.id
    empresa.updated_at = datetime.now(timezone.utc)
    db.add(empresa)
    return default


def ensure_framework_catalog(db: Session) -> Framework | None:
    """Devolve o framework padrão activo a partir da base de dados.

    Não acede ao filesystem nem executa importações.
    O bootstrapping inicial é da responsabilidade do seed (bootstrap_framework_catalog)
    e do Superadmin Portal. Chamadas em runtime (requests de utilizadores) apenas lêem
    o que já existe na DB.
    """
    return _ensure_default_flag(db)


def bootstrap_framework_catalog(db: Session) -> Framework | None:
    """Importa frameworks da pasta frameworks/ para a DB se ainda não existirem.

    EXCLUSIVO para uso em scripts de bootstrapping (seed.py, migrações iniciais).
    NÃO deve ser chamado a partir de endpoints HTTP — operações de filesystem e
    INSERTs de configuração de plataforma não devem acontecer dentro de requests
    de utilizadores.
    """
    default = _ensure_default_flag(db)
    if default:
        return default

    frameworks_root = _workspace_root() / "frameworks"
    if not frameworks_root.exists():
        return None

    from app.frameworks.import_service import importar_framework

    import logging
    _log = logging.getLogger(__name__)

    imported_any = False
    for framework_dir in _framework_directories(frameworks_root):
        try:
            manifest, structure, profiles, locales = _load_framework_bundle(framework_dir)
        except FileNotFoundError:
            _log.debug(
                "bootstrap_framework_catalog: a ignorar '%s' (sem framework.json).",
                framework_dir.name,
            )
            continue
        try:
            importar_framework(db, manifest, structure, profiles, locales)
            imported_any = True
            _log.info(
                "bootstrap_framework_catalog: framework '%s' importado.",
                framework_dir.name,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "bootstrap_framework_catalog: falha ao importar '%s': %s",
                framework_dir.name,
                exc,
            )

    if not imported_any:
        return None

    return _ensure_default_flag(db)


def get_default_framework(db: Session) -> Framework | None:
    """Retorna o framework padrão activo, usando cache com TTL de 15 minutos."""
    now = time.time()
    cached = _framework_cache.get("default_id")
    if cached is not None:
        fw_id, cached_at = cached
        if now - cached_at < _FRAMEWORK_CACHE_TTL:
            fw = db.get(Framework, fw_id)
            if fw and fw.ativo and fw.is_default:
                return fw
        # Cache desatualizado ou framework foi alterado — limpar e recalcular
        _framework_cache.pop("default_id", None)

    result = ensure_framework_catalog(db)
    if result is not None:
        _framework_cache["default_id"] = (result.id, now)
    return result


# Mapeamento tipo_entidade (NIS2 legal) → nivel_qnrcs (QNRCS), para fallback quando
# nivel_qnrcs não está definido na empresa.
_TIPO_TO_QNRCS = {
    "base": "basico",
    "importante": "substancial",
    "essencial": "elevado",
}


def load_thresholds_map(
    db: Session,
    framework: Framework,
    empresa: Empresa,
) -> dict[uuid.UUID, int]:
    # Usa nivel_qnrcs se definido; senão, deriva de tipo_entidade via _TIPO_TO_QNRCS.
    # auto_assign_for nos ComplianceProfiles usa terminologia QNRCS: basico/substancial/elevado.
    if empresa.nivel_qnrcs:
        assign_for = (
            empresa.nivel_qnrcs.value
            if hasattr(empresa.nivel_qnrcs, "value")
            else empresa.nivel_qnrcs
        )
    else:
        tipo = (
            empresa.tipo_entidade.value
            if hasattr(empresa.tipo_entidade, "value")
            else empresa.tipo_entidade
        )
        assign_for = _TIPO_TO_QNRCS.get(tipo, "basico")

    profile = db.exec(
        select(ComplianceProfile).where(
            ComplianceProfile.framework_id == framework.id,
            ComplianceProfile.auto_assign_for == assign_for,
        )
    ).first()

    if not profile:
        return {}

    thresholds = db.exec(
        select(ProfileThreshold).where(ProfileThreshold.profile_id == profile.id)
    ).all()
    return {
        threshold.control_id: threshold.minimum_level for threshold in thresholds
    }


def load_company_control_rows(
    db: Session,
    empresa_id: uuid.UUID,
    framework_id: uuid.UUID,
    implementador_id: uuid.UUID | None = None,
    dominio_id: uuid.UUID | None = None,
) -> list[ControlHierarchyRow]:
    stmt = (
        select(ControloEmpresaV2, Control, Subdomain, Domain)
        .join(Control, ControloEmpresaV2.control_id == Control.id)
        .join(Subdomain, Control.subdomain_id == Subdomain.id)
        .join(Domain, Subdomain.domain_id == Domain.id)
        .where(
            ControloEmpresaV2.empresa_id == empresa_id,
            Domain.framework_id == framework_id,
        )
        .order_by(Domain.order, Subdomain.order, Control.order)
    )

    if implementador_id is not None:
        stmt = stmt.where(ControloEmpresaV2.implementador_id == implementador_id)
    if dominio_id is not None:
        stmt = stmt.where(Domain.id == dominio_id)

    resultados = db.exec(stmt).all()
    return [
        ControlHierarchyRow(
            ce=ce,
            control=control,
            subdomain=subdomain,
            domain=domain,
        )
        for ce, control, subdomain, domain in resultados
    ]


def load_preferred_locales(
    db: Session,
    model: type[LocaleModel],
    key_field: str,
    ids: list[uuid.UUID] | set[uuid.UUID],
    locale: str,
    default_locale: str,
) -> dict[uuid.UUID, LocaleModel]:
    ids_list = list(ids)
    if not ids_list:
        return {}

    key_column = getattr(model, key_field)
    rows = db.exec(
        select(model).where(
            key_column.in_(ids_list),
            model.locale.in_([locale, default_locale]),
        )
    ).all()

    preferred: dict[uuid.UUID, LocaleModel] = {}
    for row in rows:
        item_id = getattr(row, key_field)
        current = preferred.get(item_id)
        if current is None or (
            getattr(current, "locale", default_locale) != locale
            and getattr(row, "locale", default_locale) == locale
        ):
            preferred[item_id] = row

    return preferred