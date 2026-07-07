"""
Operações de atualização de um framework já instalado.

O importer (import_service.importar_framework) faz upsert por chave natural mas
NUNCA apaga. Estas funções complementam-no para aplicar atualizações regulamentares
que envolvem remoções, reclassificações ou substituições de controlos/sub-requisitos:

  - apagar_subrequisitos_controlos: força re-verificação de controlos "recriados"
    (apaga SRs + checks dos tenants antes do reimport → UUIDs novos).
  - prune_framework_orphans: remove definições órfãs que o upsert deixa para trás
    (SRs e thresholds que já não existem na definição nova).
  - reset_estado_controlos: repõe o estado por-empresa de controlos recriados.
  - recompute_empresa_controlos: recalcula o nível de maturidade dos tenants.

São chamadas por migrações de dados (entrega on-prem) e ficam disponíveis para
futuras atualizações da framework sem reescrever a lógica.
"""
from sqlmodel import Session, select

from app.frameworks.models import (
    ComplianceProfile,
    Control,
    ControloEmpresaCheckV2,
    ControloEmpresaV2,
    Domain,
    Framework,
    ProfileThreshold,
    Subdomain,
    SubRequirement,
    SubRequirementLocale,
)
from app.shared.enums import EstadoControlo


def controls_por_codigo(
    db: Session, framework: Framework, codes
) -> dict[str, Control]:
    """Devolve {code: Control} para os controlos indicados do framework."""
    if not codes:
        return {}
    rows = db.exec(
        select(Control)
        .join(Subdomain, Control.subdomain_id == Subdomain.id)
        .join(Domain, Subdomain.domain_id == Domain.id)
        .where(Domain.framework_id == framework.id, Control.code.in_(list(codes)))
    ).all()
    return {c.code: c for c in rows}


def _apagar_sr(db: Session, sr: SubRequirement) -> None:
    for chk in db.exec(
        select(ControloEmpresaCheckV2).where(
            ControloEmpresaCheckV2.sub_requirement_id == sr.id
        )
    ).all():
        db.delete(chk)
    for loc in db.exec(
        select(SubRequirementLocale).where(
            SubRequirementLocale.sub_requirement_id == sr.id
        )
    ).all():
        db.delete(loc)
    db.delete(sr)


def apagar_subrequisitos_controlos(db: Session, framework: Framework, codes) -> int:
    """Apaga todos os SRs (+ locales + checks dos tenants) dos controlos indicados.

    Usado antes de reimportar controlos "recriados"/"substituídos": ao apagar os SRs,
    o reimport cria-os de novo com UUIDs novos e os checks concluídos não sobrevivem
    (força re-verificação pela empresa).
    """
    ctrls = controls_por_codigo(db, framework, codes)
    total = 0
    for c in ctrls.values():
        for sr in db.exec(
            select(SubRequirement).where(SubRequirement.control_id == c.id)
        ).all():
            _apagar_sr(db, sr)
            total += 1
    db.commit()
    return total


def prune_framework_orphans(
    db: Session,
    framework: Framework,
    codes,
    valid_sr_codes: dict[str, set[str]],
    valid_threshold_pairs: set[tuple[str, str]],
) -> tuple[int, int]:
    """Remove definições órfãs que o upsert do importer não apaga, para os controlos
    indicados:

      - SubRequirement (+ locale + checks) cujo code não está em valid_sr_codes[code].
      - ProfileThreshold cujo par (profile_code, control_code) não está em
        valid_threshold_pairs (ex: um controlo que deixou de exigir um nível).

    valid_sr_codes / valid_threshold_pairs derivam-se da definição nova (framework.json).
    Devolve (sr_removidos, thresholds_removidos).
    """
    ctrls = controls_por_codigo(db, framework, codes)
    removed_sr = removed_th = 0

    for code, c in ctrls.items():
        validos = valid_sr_codes.get(code, set())
        for sr in db.exec(
            select(SubRequirement).where(SubRequirement.control_id == c.id)
        ).all():
            if sr.code not in validos:
                _apagar_sr(db, sr)
                removed_sr += 1

    profiles = {
        p.id: p.code
        for p in db.exec(
            select(ComplianceProfile).where(
                ComplianceProfile.framework_id == framework.id
            )
        ).all()
    }
    for code, c in ctrls.items():
        for th in db.exec(
            select(ProfileThreshold).where(ProfileThreshold.control_id == c.id)
        ).all():
            pcode = profiles.get(th.profile_id)
            if (pcode, code) not in valid_threshold_pairs:
                db.delete(th)
                removed_th += 1

    db.commit()
    return removed_sr, removed_th


def reset_estado_controlos(db: Session, framework: Framework, codes) -> int:
    """Repõe o estado por-empresa dos controlos indicados: estado=NAO_INICIADO,
    nível 0 e aprovação limpa. Usado nos controlos recriados/substituídos, onde o
    progresso anterior deixou de ser válido."""
    ctrls = controls_por_codigo(db, framework, codes)
    control_ids = [c.id for c in ctrls.values()]
    if not control_ids:
        return 0
    total = 0
    for ce in db.exec(
        select(ControloEmpresaV2).where(
            ControloEmpresaV2.control_id.in_(control_ids)
        )
    ).all():
        ce.estado = EstadoControlo.NAO_INICIADO
        ce.nivel_maturidade_atual = 0
        ce.aprovado_por_id = None
        ce.data_aprovacao = None
        db.add(ce)
        total += 1
    db.commit()
    return total


def recompute_empresa_controlos(db: Session, framework: Framework, codes) -> int:
    """Recalcula nivel_maturidade_atual (+ snapshot de histórico) de todos os tenants
    nos controlos indicados. Reutiliza a lógica de scoring do serviço de controlos.
    Devolve o número de empresas processadas."""
    from app.controlos.service import _recalcular_nivel
    from app.empresas.models import Empresa

    ctrls = controls_por_codigo(db, framework, codes)
    empresas = db.exec(select(Empresa).where(Empresa.deleted_at.is_(None))).all()
    for empresa in empresas:
        for c in ctrls.values():
            ce = db.exec(
                select(ControloEmpresaV2).where(
                    ControloEmpresaV2.empresa_id == empresa.id,
                    ControloEmpresaV2.control_id == c.id,
                )
            ).first()
            if ce is not None:
                _recalcular_nivel(db, ce, c, empresa, framework)
    db.commit()
    return len(empresas)
