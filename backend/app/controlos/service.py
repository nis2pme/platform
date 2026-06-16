"""Serviço principal do módulo de controlos."""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from statistics import StatisticsError, mode

from fastapi import HTTPException, Request, status
from sqlalchemy import func
from sqlmodel import Session, select

from app.auth.models import RoleUtilizador, Utilizador
from app.controlos.models import (
    DecisaoAuditor,
    HistoricoMaturidade,
    RelatorioAuditoria,
)
from app.controlos.schemas import (
    ControloDetalheSchema,
    ControloListaSchema,
    ControloNivelCheckSchema,
    DashboardScoreSchema,
    DominioSchema,
    RelatorioAuditoriaSchema,
    ResumoControlosSchema,
    ScoreDominioSchema,
)
from app.empresas.models import Empresa
from app.frameworks.models import (
    Control,
    ControlLocale,
    ControloEmpresaCheckV2,
    ControloEmpresaV2,
    Domain,
    DomainLocale,
    EstadoControlo as EstadoControloV2,
    Framework,
    FrameworkLocale,
    MaturityLevel,
    MaturityLevelLocale,
    SubRequirement,
    SubRequirementLocale,
    Subdomain,
    SubdomainLocale,
)
from app.frameworks.runtime import (
    ControlHierarchyRow,
    load_company_control_rows,
    load_preferred_locales,
    load_thresholds_map,
    resolver_framework_empresa,
)
from app.notificacoes.service import criar_notificacao
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.enums import EstadoControlo as EstadoControloSchema
from app.shared.pii import cifrar_pii, decifrar_pii
from app.shared.scoring import (
    calcular_nivel_controlo,
    calcular_score_dominio,
    calcular_score_global,
)
from app.shared.utils import resolver_locale


def _estado_schema(
    estado: EstadoControloV2 | None,
) -> EstadoControloSchema | None:
    if estado is None:
        return None
    return EstadoControloSchema(estado.value)


def _load_user_map(
    db: Session,
    ids: set[uuid.UUID],
) -> dict[uuid.UUID, Utilizador]:
    if not ids:
        return {}

    utilizadores = db.exec(
        select(Utilizador).where(Utilizador.id.in_(list(ids)))
    ).all()
    return {utilizador.id: utilizador for utilizador in utilizadores}


def _get_user_display_name(
    utilizadores_map: dict[uuid.UUID, Utilizador],
    utilizador_id: uuid.UUID | None,
) -> str | None:
    if utilizador_id is None:
        return None

    utilizador = utilizadores_map.get(utilizador_id)
    if utilizador is None:
        return None

    return decifrar_pii(utilizador.nome)


def _get_nivel_minimo(
    thresholds_map: dict[uuid.UUID, int],
    control_id: uuid.UUID,
    framework: Framework,
    primeiro_nivel_map: dict[uuid.UUID, int] | None = None,
) -> int:
    if control_id in thresholds_map:
        return thresholds_map[control_id]
    if primeiro_nivel_map:
        return primeiro_nivel_map.get(control_id, framework.maturity_scale_min)
    return framework.maturity_scale_min


def _load_subrequirements_por_controlo(
    db: Session,
    control_ids: set[uuid.UUID],
) -> dict[uuid.UUID, list[SubRequirement]]:
    if not control_ids:
        return {}

    subrequirements = db.exec(
        select(SubRequirement)
        .where(SubRequirement.control_id.in_(list(control_ids)))
        .order_by(
            SubRequirement.control_id,
            SubRequirement.maturity_level,
            SubRequirement.order,
        )
    ).all()

    agrupados: dict[uuid.UUID, list[SubRequirement]] = defaultdict(list)
    for sub_requirement in subrequirements:
        agrupados[sub_requirement.control_id].append(sub_requirement)
    return dict(agrupados)


def _load_checks_concluidos_por_controlo_empresa(
    db: Session,
    controlo_empresa_ids: set[uuid.UUID],
) -> dict[uuid.UUID, set[uuid.UUID]]:
    if not controlo_empresa_ids:
        return {}

    checks = db.exec(
        select(ControloEmpresaCheckV2).where(
            ControloEmpresaCheckV2.controlo_empresa_id.in_(
                list(controlo_empresa_ids)
            ),
            ControloEmpresaCheckV2.concluido.is_(True),
        )
    ).all()

    agrupados: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for check in checks:
        agrupados[check.controlo_empresa_id].add(check.sub_requirement_id)
    return dict(agrupados)


def _progresso_conformidade(
    controlo_empresa_id: uuid.UUID,
    control_id: uuid.UUID,
    nivel_minimo: int,
    obrigatorio_perfil: bool,
    subrequirements_por_controlo: dict[uuid.UUID, list[SubRequirement]],
    checks_concluidos_por_controlo_empresa: dict[uuid.UUID, set[uuid.UUID]],
) -> float:
    """Progresso 0-100 sobre os checks exigidos até ao nível mínimo."""
    if not obrigatorio_perfil:
        return 0.0

    checks_relevantes = [
        sub_requirement.id
        for sub_requirement in subrequirements_por_controlo.get(control_id, [])
        if sub_requirement.maturity_level <= nivel_minimo
    ]
    if not checks_relevantes:
        return 0.0

    checks_concluidos = checks_concluidos_por_controlo_empresa.get(
        controlo_empresa_id,
        set(),
    )
    total_concluidos = sum(
        1 for check_id in checks_relevantes if check_id in checks_concluidos
    )
    return min(
        math.floor(total_concluidos / len(checks_relevantes) * 100),
        100,
    )


def _load_primeiro_nivel_controlo(
    db: Session,
    control_ids: set[uuid.UUID],
) -> dict[uuid.UUID, int]:
    if not control_ids:
        return {}

    rows = db.exec(
        select(
            SubRequirement.control_id,
            func.min(SubRequirement.maturity_level),
        )
        .where(SubRequirement.control_id.in_(list(control_ids)))
        .group_by(SubRequirement.control_id)
    ).all()

    return {
        control_id: int(primeiro_nivel)
        for control_id, primeiro_nivel in rows
        if primeiro_nivel is not None
    }


def _load_ultimo_nivel_controlo(
    db: Session,
    control_ids: set[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Retorna o nível máximo (último) de maturidade definido por controlo.

    Usado para limitar o nível mínimo exigido ao máximo alcançável:
    um controlo que só tem checks de nível 1 não pode ser penalizado por
    perfis que exigem nível 2 ou 3 (não existe mais nada a implementar).
    """
    if not control_ids:
        return {}

    rows = db.exec(
        select(
            SubRequirement.control_id,
            func.max(SubRequirement.maturity_level),
        )
        .where(SubRequirement.control_id.in_(list(control_ids)))
        .group_by(SubRequirement.control_id)
    ).all()

    return {
        control_id: int(ultimo_nivel)
        for control_id, ultimo_nivel in rows
        if ultimo_nivel is not None
    }


def _get_nivel_alvo_qnrcs(empresa: Empresa) -> int | None:
    nivel_qnrcs = empresa.nivel_qnrcs
    if nivel_qnrcs is None:
        nivel_qnrcs = empresa.tipo_entidade
    if hasattr(nivel_qnrcs, "value"):
        nivel_qnrcs = nivel_qnrcs.value

    mapa = {
        "basico": 1,
        "base": 1,
        "substancial": 2,
        "importante": 2,
        "elevado": 3,
        "essencial": 3,
    }
    return mapa.get(nivel_qnrcs)


def _is_obrigatorio_perfil(
    empresa: Empresa,
    framework: Framework,
    control_id: uuid.UUID,
    thresholds_map: dict[uuid.UUID, int],
    primeiro_nivel: int | None,
) -> bool:
    """Indica se o controlo já entrou no perfil ativo da empresa.

    Em QNRCS, os perfis são cumulativos pelo primeiro nível real do controlo:
    Básico inclui controlos que começam no nível 1; Substancial inclui 1 e 2;
    Elevado inclui 1, 2 e 3. Se existir `thresholds_map`, ele funciona apenas
    como guarda adicional para excluir controlos fora do profile importado.
    """
    if framework.registry_id.startswith("qnrcs-"):
        if thresholds_map and control_id not in thresholds_map:
            return False

        nivel_alvo = _get_nivel_alvo_qnrcs(empresa)
        if nivel_alvo is None:
            return True

        if primeiro_nivel is None:
            return False

        return primeiro_nivel <= nivel_alvo

    if thresholds_map:
        return control_id in thresholds_map

    return True


def calcular_nivel_minimo_global(
    thresholds_map: dict[uuid.UUID, int],
    framework: Framework,
) -> int:
    """Resumo global do mínimo exigido, alinhado com o perfil ativo."""
    if thresholds_map:
        try:
            return mode(thresholds_map.values())
        except StatisticsError:
            return min(thresholds_map.values())
    return framework.maturity_scale_min


def _is_controlo_conforme(
    row: ControlHierarchyRow,
    thresholds_map: dict[uuid.UUID, int],
    framework: Framework,
    ultimo_nivel_map: dict[uuid.UUID, int] | None = None,
) -> bool:
    nivel_minimo = _get_nivel_minimo(
        thresholds_map,
        row.control.id,
        framework,
    )
    # Limita o mínimo exigido ao nível máximo alcançável do controlo.
    # Ex: controlo com apenas checks de nível 1 não pode ser exigido ao nível 2.
    if ultimo_nivel_map:
        ultimo = ultimo_nivel_map.get(row.control.id)
        if ultimo is not None:
            nivel_minimo = min(nivel_minimo, ultimo)
    return row.ce.nivel_maturidade_atual >= nivel_minimo


def _score_conformidade_continuo(nivel_atual: int, nivel_minimo: int) -> float:
    """Fórmula contínua ponderada: min(nivel_atual, nivel_minimo) / nivel_minimo.

    Devolve 0.0–1.0. Valor 1.0 significa conformidade plena; valores intermédios
    refletem progresso parcial. Controlo com nivel_minimo=0 devolve 1.0 (trivialmente
    conforme, caso de framework sem threshold definido).
    """
    if nivel_minimo <= 0:
        return 1.0
    return min(nivel_atual, nivel_minimo) / nivel_minimo


def _get_rows_obrigatorios_perfil(
    db: Session,
    rows: list[ControlHierarchyRow],
    empresa: Empresa,
    framework: Framework,
    thresholds_map: dict[uuid.UUID, int],
) -> list[ControlHierarchyRow]:
    primeiro_nivel_map = _load_primeiro_nivel_controlo(
        db,
        {row.control.id for row in rows},
    )

    return [
        row
        for row in rows
        if _is_obrigatorio_perfil(
            empresa,
            framework,
            row.control.id,
            thresholds_map,
            primeiro_nivel_map.get(row.control.id),
        )
    ]


def obter_ids_controlos_obrigatorios_perfil_v2(
    db: Session,
    rows: list[ControlHierarchyRow],
    empresa: Empresa,
    framework: Framework,
    thresholds_map: dict[uuid.UUID, int],
) -> set[uuid.UUID]:
    """Devolve os IDs dos controlos que já entraram no perfil ativo."""
    return {
        row.control.id
        for row in _get_rows_obrigatorios_perfil(
            db,
            rows,
            empresa,
            framework,
            thresholds_map,
        )
    }


def calcular_conformidade_global_v2(
    db: Session,
    rows: list[ControlHierarchyRow],
    empresa: Empresa,
    framework: Framework,
    thresholds_map: dict[uuid.UUID, int],
) -> tuple[float, int, int]:
    """Conformidade global — fórmula ponderada por nível mínimo (Pw).

    P_w = sum(min(nivel_atual_i, nivel_minimo_i)) / sum(nivel_minimo_i) × 100

    Cada nível-passo exigido vale igualmente 1 unidade, independentemente do
    controlo a que pertence. Alinha-se com o modelo gated e com a filosofia de
    implementação em camadas prescritas pela NIS2 / DL 125/2025 / QNRCS:
    os perfis Básico → Substancial → Elevado são estágios sequenciais, pelo que
    cada achievement de nível dentro de um controlo é uma unidade de trabalho
    distinta e de igual valor.

    Retorna (percentagem, n_conformes_plenos, total_nivel_passos_exigidos).
    n_conformes_plenos = controlos onde nivel_atual >= nivel_minimo (para relatórios).
    """
    rows_obrigatorios = _get_rows_obrigatorios_perfil(
        db,
        rows,
        empresa,
        framework,
        thresholds_map,
    )
    if not rows_obrigatorios:
        return 0.0, 0, 0
    nivel_minimos = [
        _get_nivel_minimo(thresholds_map, row.control.id, framework)
        for row in rows_obrigatorios
    ]
    soma_conquistados = sum(
        min(row.ce.nivel_maturidade_atual, nivel_minimos[i])
        for i, row in enumerate(rows_obrigatorios)
    )
    soma_exigidos = sum(nivel_minimos)
    conformes_plenos = sum(
        1
        for i, row in enumerate(rows_obrigatorios)
        if row.ce.nivel_maturidade_atual >= nivel_minimos[i]
    )
    percentagem = math.floor(soma_conquistados / soma_exigidos * 100) if soma_exigidos else 0
    return percentagem, conformes_plenos, soma_exigidos


def _ensure_empresa_framework(db: Session, empresa: Empresa) -> Framework:
    framework = resolver_framework_empresa(db, empresa)
    inicializar_controlos_empresa_v2(db, empresa.id, framework.id)
    return framework


def _build_control_row_stmt(
    empresa_id: uuid.UUID,
    framework_id: uuid.UUID,
):
    return (
        select(ControloEmpresaV2, Control, Subdomain, Domain)
        .join(Control, ControloEmpresaV2.control_id == Control.id)
        .join(Subdomain, Control.subdomain_id == Subdomain.id)
        .join(Domain, Subdomain.domain_id == Domain.id)
        .where(
            ControloEmpresaV2.empresa_id == empresa_id,
            Domain.framework_id == framework_id,
        )
    )


def _load_control_row(
    db: Session,
    empresa_id: uuid.UUID,
    framework_id: uuid.UUID,
    *,
    control_id: uuid.UUID | None = None,
    controlo_empresa_id: uuid.UUID | None = None,
    implementador_id: uuid.UUID | None = None,
) -> ControlHierarchyRow | None:
    stmt = _build_control_row_stmt(empresa_id, framework_id)

    if control_id is not None:
        stmt = stmt.where(Control.id == control_id)
    if controlo_empresa_id is not None:
        stmt = stmt.where(ControloEmpresaV2.id == controlo_empresa_id)
    if implementador_id is not None:
        stmt = stmt.where(ControloEmpresaV2.implementador_id == implementador_id)

    resultado = db.exec(stmt).first()
    if resultado is None:
        return None

    ce, control, subdomain, domain = resultado
    return ControlHierarchyRow(
        ce=ce,
        control=control,
        subdomain=subdomain,
        domain=domain,
    )


def _get_ce_or_404(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    empresa_id: uuid.UUID,
) -> ControloEmpresaV2:
    ce = db.get(ControloEmpresaV2, controlo_empresa_id)
    if not ce or ce.empresa_id != empresa_id:
        raise HTTPException(status_code=404, detail="Controlo não encontrado.")
    return ce


def _validar_acesso_leitura(ce: ControloEmpresaV2, utilizador: Utilizador) -> None:
    if utilizador.role == RoleUtilizador.IMPLEMENTADOR and ce.implementador_id != utilizador.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sem acesso a este controlo.",
        )


def _validar_acesso_escrita(ce: ControloEmpresaV2, utilizador: Utilizador) -> None:
    _validar_acesso_leitura(ce, utilizador)

    if utilizador.role == RoleUtilizador.CEO:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Perfil CEO não pode alterar controlos.",
        )

    if utilizador.role == RoleUtilizador.AUDITOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Use os endpoints /aprovar ou /reprovar para aprovação.",
        )


def _build_controlo_lista_schema(
    row: ControlHierarchyRow,
    empresa: Empresa,
    framework: Framework,
    thresholds_map: dict[uuid.UUID, int],
    primeiro_nivel_map: dict[uuid.UUID, int],
    subrequirements_por_controlo: dict[uuid.UUID, list[SubRequirement]],
    checks_concluidos_por_controlo_empresa: dict[uuid.UUID, set[uuid.UUID]],
    control_locales: dict[uuid.UUID, ControlLocale],
    domain_locales: dict[uuid.UUID, DomainLocale],
    utilizadores_map: dict[uuid.UUID, Utilizador],
) -> ControloListaSchema:
    nivel_minimo = _get_nivel_minimo(thresholds_map, row.control.id, framework, primeiro_nivel_map)
    obrigatorio_perfil = _is_obrigatorio_perfil(
        empresa,
        framework,
        row.control.id,
        thresholds_map,
        primeiro_nivel_map.get(row.control.id),
    )

    return ControloListaSchema(
        id=row.control.id,
        codigo=row.control.code,
        titulo=(
            control_locales[row.control.id].title
            if row.control.id in control_locales
            else row.control.code
        ),
        descricao_simples=(
            control_locales[row.control.id].description
            if row.control.id in control_locales
            else ""
        ),
        critico=row.control.criticality == "critical",
        dominio_id=row.domain.id,
        dominio_codigo=row.domain.code,
        dominio_nome=(
            domain_locales[row.domain.id].name
            if row.domain.id in domain_locales
            else row.domain.code
        ),
        ordem=row.control.order,
        controlo_empresa_id=row.ce.id,
        estado=_estado_schema(row.ce.estado),
        nivel_maturidade_atual=row.ce.nivel_maturidade_atual,
        nivel_minimo=nivel_minimo,
        em_conformidade=row.ce.nivel_maturidade_atual >= nivel_minimo,
        score_conformidade=_score_conformidade_continuo(
            row.ce.nivel_maturidade_atual, nivel_minimo
        ),
        obrigatorio_perfil=obrigatorio_perfil,
        progresso_conformidade=_progresso_conformidade(
            row.ce.id,
            row.control.id,
            nivel_minimo,
            obrigatorio_perfil,
            subrequirements_por_controlo,
            checks_concluidos_por_controlo_empresa,
        ),
        implementador_id=row.ce.implementador_id,
        implementador_nome=_get_user_display_name(
            utilizadores_map,
            row.ce.implementador_id,
        ),
    )


def _rows_por_dominio(
    rows: list[ControlHierarchyRow],
) -> dict[uuid.UUID, list[ControlHierarchyRow]]:
    agrupados: dict[uuid.UUID, list[ControlHierarchyRow]] = defaultdict(list)
    for row in rows:
        agrupados[row.domain.id].append(row)
    return agrupados


def _filtrar_rows_visiveis_dashboard(
    rows: list[ControlHierarchyRow],
    utilizador: Utilizador | None,
) -> list[ControlHierarchyRow]:
    if utilizador is None or utilizador.role != RoleUtilizador.IMPLEMENTADOR:
        return rows

    return [
        row
        for row in rows
        if row.ce.implementador_id == utilizador.id
    ]


def _construir_resumo_controlos(
    rows: list[ControlHierarchyRow],
) -> ResumoControlosSchema:
    resumo = {
        "total": len(rows),
        "nao_iniciados": 0,
        "em_progresso": 0,
        "implementados": 0,
        "aprovados": 0,
    }

    for row in rows:
        estado = row.ce.estado.value if row.ce.estado else None
        if estado == "nao_iniciado":
            resumo["nao_iniciados"] += 1
        elif estado == "em_progresso":
            resumo["em_progresso"] += 1
        elif estado == "implementado":
            resumo["implementados"] += 1
        elif estado == "aprovado":
            resumo["aprovados"] += 1

    return ResumoControlosSchema(**resumo)


def _guardar_snapshot(db: Session, empresa: Empresa, framework: Framework) -> None:
    rows = load_company_control_rows(db, empresa.id, framework.id)
    thresholds_map = load_thresholds_map(db, framework, empresa)
    rows_obrigatorios_ids = obter_ids_controlos_obrigatorios_perfil_v2(
        db,
        rows,
        empresa,
        framework,
        thresholds_map,
    )
    grouped = _rows_por_dominio(rows)

    scores_dominio: dict[str, int] = {}
    controlos_criticos: list[ControloEmpresaV2] = []

    for domain_id, domain_rows in grouped.items():
        domain = domain_rows[0].domain
        ces_dom = [row.ce for row in domain_rows]
        scores_dominio[domain.code] = calcular_score_dominio(ces_dom)

        domain_rows_obrigatorios = [
            row
            for row in domain_rows
            if row.control.id in rows_obrigatorios_ids
        ]

        total_dom = len(domain_rows_obrigatorios)
        if total_dom:
            mins_dom = [
                _get_nivel_minimo(thresholds_map, row.control.id, framework)
                for row in domain_rows_obrigatorios
            ]
            soma_exig_dom = sum(mins_dom)
            soma_conq_dom = sum(
                min(row.ce.nivel_maturidade_atual, mins_dom[i])
                for i, row in enumerate(domain_rows_obrigatorios)
            )
            percentagem_dom = math.floor(soma_conq_dom / soma_exig_dom * 100) if soma_exig_dom else 0
            # Cap 99%: math says 100% mas mínimos não estão todos implementado/aprovado
            if percentagem_dom == 100 and any(
                row.ce.estado not in (EstadoControloV2.IMPLEMENTADO, EstadoControloV2.APROVADO)
                for row in domain_rows_obrigatorios
            ):
                percentagem_dom = 99
        else:
            percentagem_dom = 0
        db.add(
            HistoricoMaturidade(
                empresa_id=empresa.id,
                dominio_id=domain_id,
                nivel_maturidade=float(scores_dominio[domain.code]),
                percentagem_conformidade=percentagem_dom,
            )
        )

        controlos_criticos.extend(
            row.ce for row in domain_rows if row.control.criticality == "critical"
        )

    percentagem_global, _, _ = calcular_conformidade_global_v2(
        db,
        rows,
        empresa,
        framework,
        thresholds_map,
    )
    # Cap 99%: math says 100% mas mínimos não estão todos implementado/aprovado
    rows_obrigatorios_list = [row for row in rows if row.control.id in rows_obrigatorios_ids]
    if percentagem_global == 100 and any(
        row.ce.estado not in (EstadoControloV2.IMPLEMENTADO, EstadoControloV2.APROVADO)
        for row in rows_obrigatorios_list
    ):
        percentagem_global = 99
    score_global = calcular_score_global(scores_dominio, controlos_criticos)

    db.add(
        HistoricoMaturidade(
            empresa_id=empresa.id,
            dominio_id=None,
            nivel_maturidade=float(score_global),
            percentagem_conformidade=percentagem_global,
        )
    )


def _recalcular_nivel(
    db: Session,
    ce: ControloEmpresaV2,
    control: Control,
    empresa: Empresa,
    framework: Framework,
) -> int:
    checks_definidos = db.exec(
        select(SubRequirement)
        .where(SubRequirement.control_id == control.id)
        .order_by(SubRequirement.maturity_level, SubRequirement.order)
    ).all()
    checks_empresa = db.exec(
        select(ControloEmpresaCheckV2).where(
            ControloEmpresaCheckV2.controlo_empresa_id == ce.id,
            ControloEmpresaCheckV2.concluido.is_(True),
        )
    ).all()
    concluidos = {check.sub_requirement_id for check in checks_empresa}
    novo_nivel = calcular_nivel_controlo(
        concluidos,
        checks_definidos,
        max_nivel=framework.maturity_scale_max,
    )

    if novo_nivel != ce.nivel_maturidade_atual:
        ce.nivel_maturidade_atual = novo_nivel
        ce.updated_at = datetime.now(timezone.utc)
        db.add(ce)
        _guardar_snapshot(db, empresa, framework)

    return novo_nivel


def _build_niveis_conteudo(
    framework: Framework,
    control_locale: ControlLocale | None,
    maturity_levels: list[MaturityLevel],
    maturity_level_locales: dict[uuid.UUID, MaturityLevelLocale],
) -> dict[str, dict]:
    conteudo: dict[str, dict] = {}

    for maturity_level in maturity_levels:
        locale_data = maturity_level_locales.get(maturity_level.id)
        key = str(maturity_level.level)

        indicadores = locale_data.indicators if locale_data else None
        evidencias = locale_data.evidence_examples if locale_data else None

        if (
            maturity_level.level == framework.maturity_scale_min
            and control_locale is not None
        ):
            if not indicadores:
                indicadores = control_locale.implementation_guide
            if not evidencias:
                evidencias = control_locale.evidence_examples

        entry: dict[str, object] = {}
        if locale_data and locale_data.description:
            entry["descricao"] = locale_data.description
        elif (
            maturity_level.level == framework.maturity_scale_min
            and control_locale is not None
            and control_locale.description
        ):
            entry["descricao"] = control_locale.description

        if indicadores:
            entry["indicadores"] = indicadores
            entry["guia_implementacao"] = indicadores

        if evidencias:
            entry["evidencias"] = evidencias
            entry["exemplos_evidencias"] = evidencias

        if locale_data and locale_data.tip:
            entry["dica_pratica"] = locale_data.tip

        if locale_data and locale_data.effort:
            entry["esforco_estimado"] = locale_data.effort

        if entry:
            conteudo[key] = entry

    if conteudo:
        return conteudo

    if control_locale and (
        control_locale.description
        or control_locale.implementation_guide
        or control_locale.evidence_examples
    ):
        base_key = str(framework.maturity_scale_min)
        conteudo[base_key] = {
            "descricao": control_locale.description or "",
            "indicadores": control_locale.implementation_guide or [],
            "guia_implementacao": control_locale.implementation_guide or [],
            "evidencias": control_locale.evidence_examples or [],
            "exemplos_evidencias": control_locale.evidence_examples or [],
        }

    return conteudo


def _notificar_implementador(
    *,
    db: Session,
    empresa_id: uuid.UUID,
    utilizador_id: uuid.UUID,
    titulo: str,
    mensagem: str,
    controlo_empresa_id: uuid.UUID,
) -> None:
    criar_notificacao(
        db,
        empresa_id=empresa_id,
        utilizador_id=utilizador_id,
        tipo="controlo_decisao_auditoria",
        titulo=titulo,
        mensagem=mensagem,
        controlo_empresa_id=controlo_empresa_id,
    )


def inicializar_controlos_empresa_v2(
    db: Session,
    empresa_id: uuid.UUID,
    framework_id: uuid.UUID,
) -> int:
    control_ids = db.exec(
        select(Control.id)
        .join(Subdomain, Control.subdomain_id == Subdomain.id)
        .join(Domain, Subdomain.domain_id == Domain.id)
        .where(Domain.framework_id == framework_id)
    ).all()

    if not control_ids:
        return 0

    existentes = set(
        db.exec(
            select(ControloEmpresaV2.control_id).where(
                ControloEmpresaV2.empresa_id == empresa_id,
                ControloEmpresaV2.framework_id == framework_id,
            )
        ).all()
    )

    criados = 0
    for control_id in control_ids:
        if control_id in existentes:
            continue
        db.add(
            ControloEmpresaV2(
                empresa_id=empresa_id,
                framework_id=framework_id,
                control_id=control_id,
            )
        )
        criados += 1

    return criados


def inicializar_controlos_empresa(db: Session, empresa_id: uuid.UUID) -> int:
    empresa = db.get(Empresa, empresa_id)
    if not empresa:
        raise HTTPException(status_code=404, detail="Empresa não encontrada.")

    framework = _ensure_empresa_framework(db, empresa)
    return inicializar_controlos_empresa_v2(db, empresa.id, framework.id)


def listar_dominios(
    db: Session,
    empresa_id: uuid.UUID,
    empresa: Empresa,
    locale: str | None = None,
) -> list[DominioSchema]:
    framework = _ensure_empresa_framework(db, empresa)
    locale = locale or resolver_locale(empresa, framework)

    rows = load_company_control_rows(db, empresa_id, framework.id)
    grouped = _rows_por_dominio(rows)
    domain_locales = load_preferred_locales(
        db,
        DomainLocale,
        "domain_id",
        grouped.keys(),
        locale,
        framework.default_locale,
    )

    resultado: list[DominioSchema] = []
    for domain_rows in grouped.values():
        domain = domain_rows[0].domain
        domain_locale = domain_locales.get(domain.id)
        resultado.append(
            DominioSchema(
                id=domain.id,
                codigo=domain.code,
                nome=domain_locale.name if domain_locale else domain.code,
                descricao=(
                    domain_locale.description if domain_locale else ""
                ),
                ordem=domain.order,
                score=calcular_score_dominio([row.ce for row in domain_rows]),
            )
        )

    return resultado


def listar_controlos(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    dominio_id: uuid.UUID | None = None,
    locale: str | None = None,
) -> list[ControloListaSchema]:
    framework = _ensure_empresa_framework(db, empresa)
    locale = locale or resolver_locale(empresa, framework)

    implementador_id = (
        utilizador.id if utilizador.role == RoleUtilizador.IMPLEMENTADOR else None
    )
    rows = load_company_control_rows(
        db,
        empresa.id,
        framework.id,
        implementador_id,
        dominio_id,
    )

    thresholds_map = load_thresholds_map(db, framework, empresa)
    primeiro_nivel_map = _load_primeiro_nivel_controlo(
        db,
        {row.control.id for row in rows},
    )
    subrequirements_por_controlo = _load_subrequirements_por_controlo(
        db,
        {row.control.id for row in rows},
    )
    checks_concluidos_por_controlo_empresa = (
        _load_checks_concluidos_por_controlo_empresa(
            db,
            {row.ce.id for row in rows},
        )
    )
    control_locales = load_preferred_locales(
        db,
        ControlLocale,
        "control_id",
        {row.control.id for row in rows},
        locale,
        framework.default_locale,
    )
    domain_locales = load_preferred_locales(
        db,
        DomainLocale,
        "domain_id",
        {row.domain.id for row in rows},
        locale,
        framework.default_locale,
    )
    utilizadores_map = _load_user_map(
        db,
        {
            row.ce.implementador_id
            for row in rows
            if row.ce.implementador_id is not None
        },
    )

    return [
        _build_controlo_lista_schema(
            row,
            empresa,
            framework,
            thresholds_map,
            primeiro_nivel_map,
            subrequirements_por_controlo,
            checks_concluidos_por_controlo_empresa,
            control_locales,
            domain_locales,
            utilizadores_map,
        )
        for row in rows
    ]


def get_controlo_lista_item(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    controlo_empresa_id: uuid.UUID,
    locale: str | None = None,
) -> ControloListaSchema:
    framework = _ensure_empresa_framework(db, empresa)
    locale = locale or resolver_locale(empresa, framework)

    implementador_id = (
        utilizador.id if utilizador.role == RoleUtilizador.IMPLEMENTADOR else None
    )
    row = _load_control_row(
        db,
        empresa.id,
        framework.id,
        controlo_empresa_id=controlo_empresa_id,
        implementador_id=implementador_id,
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Controlo não disponível para esta empresa.",
        )

    thresholds_map = load_thresholds_map(db, framework, empresa)
    primeiro_nivel_map = _load_primeiro_nivel_controlo(db, {row.control.id})
    subrequirements_por_controlo = _load_subrequirements_por_controlo(
        db,
        {row.control.id},
    )
    checks_concluidos_por_controlo_empresa = (
        _load_checks_concluidos_por_controlo_empresa(
            db,
            {row.ce.id},
        )
    )
    control_locales = load_preferred_locales(
        db,
        ControlLocale,
        "control_id",
        {row.control.id},
        locale,
        framework.default_locale,
    )
    domain_locales = load_preferred_locales(
        db,
        DomainLocale,
        "domain_id",
        {row.domain.id},
        locale,
        framework.default_locale,
    )
    utilizadores_map = _load_user_map(
        db,
        {row.ce.implementador_id} if row.ce.implementador_id else set(),
    )

    return _build_controlo_lista_schema(
        row,
        empresa,
        framework,
        thresholds_map,
        primeiro_nivel_map,
        subrequirements_por_controlo,
        checks_concluidos_por_controlo_empresa,
        control_locales,
        domain_locales,
        utilizadores_map,
    )


def get_controlo_detalhe(
    db: Session,
    empresa: Empresa,
    controlo_id: uuid.UUID,
    utilizador: Utilizador,
    locale: str | None = None,
) -> ControloDetalheSchema:
    framework = _ensure_empresa_framework(db, empresa)
    locale = locale or resolver_locale(empresa, framework)

    implementador_id = (
        utilizador.id if utilizador.role == RoleUtilizador.IMPLEMENTADOR else None
    )
    row = _load_control_row(
        db,
        empresa.id,
        framework.id,
        control_id=controlo_id,
        implementador_id=implementador_id,
    )
    if row is None:
        row = _load_control_row(
            db,
            empresa.id,
            framework.id,
            controlo_empresa_id=controlo_id,
            implementador_id=implementador_id,
        )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Controlo não disponível para esta empresa.",
        )

    control_locale = load_preferred_locales(
        db,
        ControlLocale,
        "control_id",
        {row.control.id},
        locale,
        framework.default_locale,
    ).get(row.control.id)
    domain_locale = load_preferred_locales(
        db,
        DomainLocale,
        "domain_id",
        {row.domain.id},
        locale,
        framework.default_locale,
    ).get(row.domain.id)
    subdomain_locale = load_preferred_locales(
        db,
        SubdomainLocale,
        "subdomain_id",
        {row.subdomain.id},
        locale,
        framework.default_locale,
    ).get(row.subdomain.id)

    thresholds_map = load_thresholds_map(db, framework, empresa)

    sr_rows = db.exec(
        select(SubRequirement, SubRequirementLocale)
        .outerjoin(
            SubRequirementLocale,
            (SubRequirementLocale.sub_requirement_id == SubRequirement.id)
            & (SubRequirementLocale.locale.in_([locale, framework.default_locale])),
        )
        .where(SubRequirement.control_id == row.control.id)
        .order_by(SubRequirement.maturity_level, SubRequirement.order)
    ).all()

    sub_requirements: list[SubRequirement] = []
    sub_requirement_locales: dict[uuid.UUID, SubRequirementLocale] = {}
    seen_sr_ids: set[uuid.UUID] = set()
    for sr, sr_loc in sr_rows:
        if sr.id not in seen_sr_ids:
            sub_requirements.append(sr)
            seen_sr_ids.add(sr.id)
        if sr_loc is not None:
            current = sub_requirement_locales.get(sr.id)
            if current is None or (
                getattr(current, "locale", framework.default_locale) != locale
                and sr_loc.locale == locale
            ):
                sub_requirement_locales[sr.id] = sr_loc

    checks_empresa = db.exec(
        select(ControloEmpresaCheckV2).where(
            ControloEmpresaCheckV2.controlo_empresa_id == row.ce.id
        )
    ).all()
    checks_map = {
        check.sub_requirement_id: check for check in checks_empresa
    }

    ml_rows = db.exec(
        select(MaturityLevel, MaturityLevelLocale)
        .outerjoin(
            MaturityLevelLocale,
            (MaturityLevelLocale.maturity_level_id == MaturityLevel.id)
            & (MaturityLevelLocale.locale.in_([locale, framework.default_locale])),
        )
        .where(MaturityLevel.control_id == row.control.id)
        .order_by(MaturityLevel.level)
    ).all()

    maturity_levels: list[MaturityLevel] = []
    maturity_level_locales: dict[uuid.UUID, MaturityLevelLocale] = {}
    seen_ml_ids: set[uuid.UUID] = set()
    for ml, ml_loc in ml_rows:
        if ml.id not in seen_ml_ids:
            maturity_levels.append(ml)
            seen_ml_ids.add(ml.id)
        if ml_loc is not None:
            current = maturity_level_locales.get(ml.id)
            if current is None or (
                getattr(current, "locale", framework.default_locale) != locale
                and ml_loc.locale == locale
            ):
                maturity_level_locales[ml.id] = ml_loc
    maturity_level_by_level = {
        maturity_level.level: maturity_level_locales[maturity_level.id]
        for maturity_level in maturity_levels
        if maturity_level.id in maturity_level_locales
    }

    utilizador_ids = {
        check.concluido_por_id
        for check in checks_empresa
        if check.concluido_por_id is not None
    }
    if row.ce.implementador_id:
        utilizador_ids.add(row.ce.implementador_id)
    if row.ce.aprovado_por_id:
        utilizador_ids.add(row.ce.aprovado_por_id)
    utilizadores_map = _load_user_map(db, utilizador_ids)

    checks_por_nivel: dict[int, list[ControloNivelCheckSchema]] = defaultdict(list)
    for sub_requirement in sub_requirements:
        check_empresa = checks_map.get(sub_requirement.id)
        locale_data = sub_requirement_locales.get(sub_requirement.id)
        maturity_locale = maturity_level_by_level.get(sub_requirement.maturity_level)
        descricao = sub_requirement.code
        if locale_data:
            descricao = locale_data.description
        elif maturity_locale and maturity_locale.indicators:
            indicator_index = sub_requirement.order - 1
            if indicator_index < len(maturity_locale.indicators):
                descricao = maturity_locale.indicators[indicator_index]

        concluido_por_nome = None
        if check_empresa and check_empresa.concluido_por_id:
            concluido_por = utilizadores_map.get(check_empresa.concluido_por_id)
            concluido_por_nome = (
                decifrar_pii(concluido_por.nome) if concluido_por else None
            )

        checks_por_nivel[sub_requirement.maturity_level].append(
            ControloNivelCheckSchema(
                id=sub_requirement.id,
                nivel=sub_requirement.maturity_level,
                ordem=sub_requirement.order,
                descricao=descricao,
                obrigatorio=sub_requirement.mandatory,
                concluido=bool(check_empresa and check_empresa.concluido),
                concluido_at=check_empresa.concluido_at if check_empresa else None,
                concluido_por_nome=concluido_por_nome,
            )
        )

    ultimo_relatorio = db.exec(
        select(RelatorioAuditoria)
        .where(RelatorioAuditoria.controlo_empresa_v2_id == row.ce.id)
        .order_by(RelatorioAuditoria.created_at.desc())
        .limit(1)
    ).first()

    ultimo_relatorio_schema = None
    if ultimo_relatorio:
        ultimo_relatorio_schema = RelatorioAuditoriaSchema(
            id=ultimo_relatorio.id,
            auditor_id=ultimo_relatorio.auditor_id,
            auditor_nome=ultimo_relatorio.auditor_nome,
            decisao=ultimo_relatorio.decisao,
            texto=ultimo_relatorio.texto,
            created_at=ultimo_relatorio.created_at,
        )

    return ControloDetalheSchema(
        id=row.control.id,
        codigo=row.control.code,
        titulo=control_locale.title if control_locale else row.control.code,
        descricao_simples=(
            control_locale.description if control_locale else ""
        ),
        critico=row.control.criticality == "critical",
        dominio_id=row.domain.id,
        dominio_codigo=row.domain.code,
        dominio_nome=domain_locale.name if domain_locale else row.domain.code,
        ordem=row.control.order,
        subdomain_id=row.subdomain.id,
        subdomain_codigo=row.subdomain.code,
        subdomain_nome=(
            subdomain_locale.name if subdomain_locale else row.subdomain.code
        ),
        niveis_conteudo=_build_niveis_conteudo(
            framework,
            control_locale,
            maturity_levels,
            maturity_level_locales,
        )
        or None,
        mapeamento=None,
        controlo_empresa_id=row.ce.id,
        estado=_estado_schema(row.ce.estado),
        nivel_maturidade_atual=row.ce.nivel_maturidade_atual,
        nivel_minimo=_get_nivel_minimo(
            thresholds_map, row.control.id, framework
        ),
        em_conformidade=(
            row.ce.nivel_maturidade_atual
            >= _get_nivel_minimo(thresholds_map, row.control.id, framework)
        ),
        score_conformidade=_score_conformidade_continuo(
            row.ce.nivel_maturidade_atual,
            _get_nivel_minimo(thresholds_map, row.control.id, framework),
        ),
        implementador_id=row.ce.implementador_id,
        implementador_nome=_get_user_display_name(
            utilizadores_map,
            row.ce.implementador_id,
        ),
        aprovado_por_id=row.ce.aprovado_por_id,
        aprovado_por_nome=_get_user_display_name(
            utilizadores_map,
            row.ce.aprovado_por_id,
        ),
        data_aprovacao=row.ce.data_aprovacao,
        ultimo_relatorio_auditoria=ultimo_relatorio_schema,
        checks_por_nivel=dict(checks_por_nivel),
    )


def concluir_check(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    check_id: uuid.UUID,
    empresa: Empresa,
    utilizador: Utilizador,
    request: Request | None = None,
) -> int:
    framework = _ensure_empresa_framework(db, empresa)
    ce = _get_ce_or_404(db, controlo_empresa_id, empresa.id)
    _validar_acesso_escrita(ce, utilizador)

    sub_requirement = db.get(SubRequirement, check_id)
    if not sub_requirement or sub_requirement.control_id != ce.control_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Check inválido para este controlo.",
        )

    control = db.get(Control, ce.control_id)
    check_empresa = db.exec(
        select(ControloEmpresaCheckV2).where(
            ControloEmpresaCheckV2.controlo_empresa_id == ce.id,
            ControloEmpresaCheckV2.sub_requirement_id == sub_requirement.id,
        )
    ).first()

    if check_empresa and check_empresa.concluido:
        return ce.nivel_maturidade_atual

    if not check_empresa:
        check_empresa = ControloEmpresaCheckV2(
            controlo_empresa_id=ce.id,
            sub_requirement_id=sub_requirement.id,
            empresa_id=empresa.id,
        )

    check_empresa.concluido = True
    check_empresa.concluido_por_id = utilizador.id
    check_empresa.concluido_at = datetime.now(timezone.utc)
    db.add(check_empresa)

    if ce.estado == EstadoControloV2.NAO_INICIADO:
        ce.estado = EstadoControloV2.EM_PROGRESSO
        ce.updated_at = datetime.now(timezone.utc)
        db.add(ce)

    novo_nivel = _recalcular_nivel(db, ce, control, empresa, framework)
    registar_acao(
        db,
        acao=Acao.CONTROLO_CHECK_CONCLUIDO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa.id,
        utilizador_id=utilizador.id,
        entidade_tipo="ControloEmpresaCheckV2",
        entidade_id=check_empresa.id,
        dados_novos={
            "check_id": str(check_id),
            "controlo_codigo": control.code if control else None,
            "novo_nivel": novo_nivel,
        },
        request=request,
    )
    return novo_nivel


def reverter_check(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    check_id: uuid.UUID,
    empresa: Empresa,
    utilizador: Utilizador,
    request: Request | None = None,
) -> int:
    framework = _ensure_empresa_framework(db, empresa)
    ce = _get_ce_or_404(db, controlo_empresa_id, empresa.id)
    _validar_acesso_escrita(ce, utilizador)

    check_empresa = db.exec(
        select(ControloEmpresaCheckV2).where(
            ControloEmpresaCheckV2.controlo_empresa_id == ce.id,
            ControloEmpresaCheckV2.sub_requirement_id == check_id,
        )
    ).first()
    if not check_empresa or not check_empresa.concluido:
        return ce.nivel_maturidade_atual

    check_empresa.concluido = False
    check_empresa.concluido_por_id = None
    check_empresa.concluido_at = None
    db.add(check_empresa)

    control = db.get(Control, ce.control_id)
    novo_nivel = _recalcular_nivel(db, ce, control, empresa, framework)
    registar_acao(
        db,
        acao=Acao.CONTROLO_CHECK_REVERTIDO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa.id,
        utilizador_id=utilizador.id,
        entidade_tipo="ControloEmpresaCheckV2",
        entidade_id=check_empresa.id,
        dados_novos={
            "check_id": str(check_id),
            "controlo_codigo": control.code if control else None,
            "novo_nivel": novo_nivel,
        },
        request=request,
    )
    return novo_nivel


def alterar_estado(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    novo_estado: EstadoControloSchema,
    empresa: Empresa,
    utilizador: Utilizador,
    request: Request | None = None,
) -> ControloEmpresaV2:
    ce = _get_ce_or_404(db, controlo_empresa_id, empresa.id)
    _validar_acesso_escrita(ce, utilizador)

    if utilizador.role == RoleUtilizador.IMPLEMENTADOR:
        permitidos = {
            EstadoControloV2.EM_PROGRESSO,
            EstadoControloV2.IMPLEMENTADO,
        }
        if EstadoControloV2(novo_estado.value) not in permitidos:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Implementador só pode mudar estado para "
                    "'em_progresso' ou 'implementado'."
                ),
            )

    control = db.get(Control, ce.control_id)
    estado_anterior = ce.estado.value
    ce.estado = EstadoControloV2(novo_estado.value)
    ce.updated_at = datetime.now(timezone.utc)
    db.add(ce)

    registar_acao(
        db,
        acao=Acao.CONTROLO_ESTADO_ALTERADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa.id,
        utilizador_id=utilizador.id,
        entidade_tipo="ControloEmpresaV2",
        entidade_id=ce.id,
        dados_anteriores={"estado": estado_anterior},
        dados_novos={
            "estado": novo_estado.value,
            "controlo_codigo": control.code if control else None,
        },
        request=request,
    )
    return ce


def aprovar_controlo(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    empresa: Empresa,
    utilizador: Utilizador,
    texto_relatorio: str,
    request: Request | None = None,
) -> ControloEmpresaV2:
    if utilizador.role != RoleUtilizador.AUDITOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas auditores podem aprovar controlos.",
        )

    ce = _get_ce_or_404(db, controlo_empresa_id, empresa.id)
    if ce.estado != EstadoControloV2.IMPLEMENTADO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Apenas controlos no estado 'implementado' podem ser aprovados.",
        )

    control = db.get(Control, ce.control_id)
    ce.estado = EstadoControloV2.APROVADO
    ce.aprovado_por_id = utilizador.id
    ce.data_aprovacao = datetime.now(timezone.utc)
    ce.updated_at = datetime.now(timezone.utc)
    db.add(ce)

    relatorio = RelatorioAuditoria(
        controlo_empresa_v2_id=ce.id,
        empresa_id=empresa.id,
        auditor_id=utilizador.id,
        auditor_nome=utilizador.nome,  # já cifrado (Utilizador.nome é PII)
        decisao=DecisaoAuditor.APROVADO,
        texto=cifrar_pii(texto_relatorio) or texto_relatorio,  # fallback: cifrar_pii nunca retorna None para input não-None
    )
    db.add(relatorio)

    registar_acao(
        db,
        acao=Acao.CONTROLO_APROVADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa.id,
        utilizador_id=utilizador.id,
        entidade_tipo="ControloEmpresaV2",
        entidade_id=ce.id,
        dados_novos={"controlo_codigo": control.code if control else None},
        request=request,
    )

    if ce.implementador_id:
        _notificar_implementador(
            db=db,
            empresa_id=empresa.id,
            utilizador_id=ce.implementador_id,
            titulo="Controlo aprovado",
            mensagem=(
                f"O controlo {control.code} foi aprovado pelo auditor {decifrar_pii(utilizador.nome)}."
            ),
            controlo_empresa_id=ce.id,
        )

    return ce


def reprovar_controlo(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    empresa: Empresa,
    utilizador: Utilizador,
    texto_relatorio: str,
    nota: str | None = None,
    request: Request | None = None,
) -> ControloEmpresaV2:
    if utilizador.role != RoleUtilizador.AUDITOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas auditores podem reprovar controlos.",
        )

    ce = _get_ce_or_404(db, controlo_empresa_id, empresa.id)
    if ce.estado not in (
        EstadoControloV2.IMPLEMENTADO,
        EstadoControloV2.APROVADO,
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Apenas controlos 'implementado' ou 'aprovado' podem ser reprovados.",
        )

    control = db.get(Control, ce.control_id)
    ce.estado = EstadoControloV2.NAO_APROVADO
    ce.aprovado_por_id = None
    ce.data_aprovacao = None
    ce.updated_at = datetime.now(timezone.utc)
    db.add(ce)

    relatorio = RelatorioAuditoria(
        controlo_empresa_v2_id=ce.id,
        empresa_id=empresa.id,
        auditor_id=utilizador.id,
        auditor_nome=utilizador.nome,  # já cifrado (Utilizador.nome é PII)
        decisao=DecisaoAuditor.NAO_APROVADO,
        texto=cifrar_pii(texto_relatorio) or texto_relatorio,  # fallback: cifrar_pii nunca retorna None para input não-None
    )
    db.add(relatorio)

    registar_acao(
        db,
        acao=Acao.CONTROLO_NAO_APROVADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa.id,
        utilizador_id=utilizador.id,
        entidade_tipo="ControloEmpresaV2",
        entidade_id=ce.id,
        dados_novos={
            "controlo_codigo": control.code if control else None,
            "nota": nota,
        },
        request=request,
    )

    if ce.implementador_id:
        _notificar_implementador(
            db=db,
            empresa_id=empresa.id,
            utilizador_id=ce.implementador_id,
            titulo="Controlo não aprovado",
            mensagem=(
                f"O controlo {control.code} foi reprovado pelo auditor {decifrar_pii(utilizador.nome)}."
            ),
            controlo_empresa_id=ce.id,
        )

    return ce


def get_historico_relatorios(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    empresa: Empresa,
    utilizador: Utilizador,
    *,
    limite: int | None = None,
    offset: int = 0,
) -> list[RelatorioAuditoriaSchema]:
    ce = _get_ce_or_404(db, controlo_empresa_id, empresa.id)
    _validar_acesso_leitura(ce, utilizador)

    stmt = (
        select(RelatorioAuditoria)
        .where(RelatorioAuditoria.controlo_empresa_v2_id == ce.id)
        .order_by(RelatorioAuditoria.created_at.desc())
    )
    if offset:
        stmt = stmt.offset(offset)
    if limite is not None:
        stmt = stmt.limit(limite)

    relatorios = db.exec(stmt).all()
    return [
        RelatorioAuditoriaSchema(
            id=relatorio.id,
            auditor_id=relatorio.auditor_id,
            auditor_nome=relatorio.auditor_nome,
            decisao=relatorio.decisao,
            texto=relatorio.texto,
            created_at=relatorio.created_at,
        )
        for relatorio in relatorios
    ]


def delegar_controlo(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    implementador_id: uuid.UUID | None,
    empresa: Empresa,
    utilizador: Utilizador,
    request: Request | None = None,
) -> ControloEmpresaV2:
    if utilizador.role not in (RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas administradores podem delegar controlos.",
        )

    ce = _get_ce_or_404(db, controlo_empresa_id, empresa.id)
    control = db.get(Control, ce.control_id)
    impl_anterior = str(ce.implementador_id) if ce.implementador_id else None

    impl_nome = None
    impl_email = None
    if implementador_id is not None:
        implementador = db.get(Utilizador, implementador_id)
        if not implementador or implementador.empresa_id != empresa.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Implementador não encontrado nesta empresa.",
            )
        if implementador.role != RoleUtilizador.IMPLEMENTADOR:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "O utilizador selecionado não tem o perfil de implementador."
                ),
            )
        impl_nome = implementador.nome
        impl_email = implementador.email

    ce.implementador_id = implementador_id
    ce.updated_at = datetime.now(timezone.utc)
    db.add(ce)

    registar_acao(
        db,
        acao=(
            Acao.UTILIZADOR_DELEGACAO_ATRIBUIDA
            if implementador_id is not None
            else Acao.UTILIZADOR_DELEGACAO_REMOVIDA
        ),
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa.id,
        utilizador_id=utilizador.id,
        entidade_tipo="ControloEmpresaV2",
        entidade_id=ce.id,
        dados_anteriores={"implementador_id": impl_anterior},
        dados_novos={
            "implementador_id": str(implementador_id) if implementador_id else None,
            "implementador_email": impl_email,
            "controlo_codigo": control.code if control else None,
        },
        request=request,
    )

    return ce


def delegar_controlos_lote(
    db: Session,
    implementador_id: uuid.UUID,
    adicionar_ids: list[uuid.UUID],
    remover_ids: list[uuid.UUID],
    empresa: Empresa,
    utilizador: Utilizador,
    request: Request | None = None,
) -> int:
    if utilizador.role not in (RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas administradores podem delegar controlos.",
        )

    adicionar_unicos = list(dict.fromkeys(adicionar_ids))
    adicionar_set = set(adicionar_unicos)
    remover_unicos = [
        controlo_id
        for controlo_id in dict.fromkeys(remover_ids)
        if controlo_id not in adicionar_set
    ]

    alterados = 0
    for controlo_empresa_id in adicionar_unicos:
        delegar_controlo(
            db,
            controlo_empresa_id,
            implementador_id,
            empresa,
            utilizador,
            request,
        )
        alterados += 1

    for controlo_empresa_id in remover_unicos:
        delegar_controlo(
            db,
            controlo_empresa_id,
            None,
            empresa,
            utilizador,
            request,
        )
        alterados += 1

    return alterados


def calcular_dashboard(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador | None = None,
    locale: str | None = None,
) -> DashboardScoreSchema:
    framework = _ensure_empresa_framework(db, empresa)
    locale = locale or resolver_locale(empresa, framework)

    rows = load_company_control_rows(db, empresa.id, framework.id)
    rows_visiveis = _filtrar_rows_visiveis_dashboard(rows, utilizador)
    rows_visiveis_ids = {row.ce.id for row in rows_visiveis}
    grouped = _rows_por_dominio(rows)
    thresholds_map = load_thresholds_map(db, framework, empresa)
    rows_obrigatorios_ids = obter_ids_controlos_obrigatorios_perfil_v2(
        db,
        rows,
        empresa,
        framework,
        thresholds_map,
    )
    ultimo_nivel_map = _load_ultimo_nivel_controlo(
        db,
        {row.control.id for row in rows},
    )
    subrequirements_por_controlo = _load_subrequirements_por_controlo(
        db,
        {row.control.id for row in rows_visiveis},
    )
    checks_concluidos_por_controlo_empresa = (
        _load_checks_concluidos_por_controlo_empresa(
            db,
            rows_visiveis_ids,
        )
    )

    framework_locale = load_preferred_locales(
        db,
        FrameworkLocale,
        "framework_id",
        {framework.id},
        locale,
        framework.default_locale,
    ).get(framework.id)
    domain_locales = load_preferred_locales(
        db,
        DomainLocale,
        "domain_id",
        grouped.keys(),
        locale,
        framework.default_locale,
    )
    control_locales = load_preferred_locales(
        db,
        ControlLocale,
        "control_id",
        {row.control.id for row in rows},
        locale,
        framework.default_locale,
    )

    dominios_schema: list[ScoreDominioSchema] = []
    scores_dominio: dict[str, int] = {}
    controlos_criticos: list[ControloEmpresaV2] = []
    criticos_em_falta: list[ControloListaSchema] = []
    # Resumo conta apenas os controlos obrigatórios para o perfil da empresa
    rows_visiveis_obrigatorios = [
        row for row in rows_visiveis if row.control.id in rows_obrigatorios_ids
    ]
    resumo_controlos = _construir_resumo_controlos(rows_visiveis_obrigatorios)

    for domain_rows in grouped.values():
        domain = domain_rows[0].domain
        domain_locale = domain_locales.get(domain.id)
        domain_rows_obrigatorios = [
            row
            for row in domain_rows
            if row.control.id in rows_obrigatorios_ids
        ]
        total = len(domain_rows_obrigatorios)
        conformes = sum(
            1
            for row in domain_rows_obrigatorios
            if _is_controlo_conforme(row, thresholds_map, framework, ultimo_nivel_map)
        )
        if total:
            mins = [
                _get_nivel_minimo(thresholds_map, row.control.id, framework)
                for row in domain_rows_obrigatorios
            ]
            soma_exig = sum(mins)
            soma_conq = sum(
                min(row.ce.nivel_maturidade_atual, mins[i])
                for i, row in enumerate(domain_rows_obrigatorios)
            )
            percentagem_dom = math.floor(soma_conq / soma_exig * 100) if soma_exig else 0
            # Cap 99%: math says 100% mas mínimos não estão todos implementado/aprovado
            if percentagem_dom == 100 and any(
                row.ce.estado not in (EstadoControloV2.IMPLEMENTADO, EstadoControloV2.APROVADO)
                for row in domain_rows_obrigatorios
            ):
                percentagem_dom = 99
        else:
            percentagem_dom = 0
        score = calcular_score_dominio([row.ce for row in domain_rows])
        scores_dominio[domain.code] = score

        dominios_schema.append(
            ScoreDominioSchema(
                dominio_id=domain.id,
                codigo=domain.code,
                nome=domain_locale.name if domain_locale else domain.code,
                score=score,
                total_controlos=total,
                controlos_conformidade=conformes,
                percentagem_conformidade=percentagem_dom,
            )
        )

        for row in domain_rows:
            if row.control.criticality != "critical":
                continue

            controlos_criticos.append(row.ce)

            if row.control.id not in rows_obrigatorios_ids:
                continue

            if row.ce.id not in rows_visiveis_ids:
                continue

            nivel_minimo = _get_nivel_minimo(
                thresholds_map, row.control.id, framework
            )
            if row.ce.nivel_maturidade_atual >= nivel_minimo:
                continue

            control_locale = control_locales.get(row.control.id)
            criticos_em_falta.append(
                ControloListaSchema(
                    id=row.control.id,
                    codigo=row.control.code,
                    titulo=(
                        control_locale.title if control_locale else row.control.code
                    ),
                    descricao_simples=(
                        control_locale.description if control_locale else ""
                    ),
                    critico=True,
                    dominio_id=row.domain.id,
                    dominio_codigo=row.domain.code,
                    dominio_nome=(
                        domain_locale.name if domain_locale else row.domain.code
                    ),
                    ordem=row.control.order,
                    controlo_empresa_id=row.ce.id,
                    estado=_estado_schema(row.ce.estado),
                    nivel_maturidade_atual=row.ce.nivel_maturidade_atual,
                    nivel_minimo=nivel_minimo,
                    em_conformidade=False,
                    obrigatorio_perfil=True,
                    progresso_conformidade=_progresso_conformidade(
                        row.ce.id,
                        row.control.id,
                        nivel_minimo,
                        True,
                        subrequirements_por_controlo,
                        checks_concluidos_por_controlo_empresa,
                    ),
                    implementador_id=row.ce.implementador_id,
                )
            )

    percentagem_global, _, _ = calcular_conformidade_global_v2(
        db,
        rows,
        empresa,
        framework,
        thresholds_map,
    )
    # Cap 99%: math says 100% mas mínimos não estão todos implementado/aprovado
    rows_obrigatorios_global = [row for row in rows if row.control.id in rows_obrigatorios_ids]
    if percentagem_global == 100 and any(
        row.ce.estado not in (EstadoControloV2.IMPLEMENTADO, EstadoControloV2.APROVADO)
        for row in rows_obrigatorios_global
    ):
        percentagem_global = 99
    score_global = calcular_score_global(scores_dominio, controlos_criticos)

    nivel_minimo_global = calcular_nivel_minimo_global(
        thresholds_map,
        framework,
    )

    return DashboardScoreSchema(
        empresa_id=empresa.id,
        empresa_nome=decifrar_pii(empresa.nome),
        empresa_created_at=empresa.created_at,
        score_global=score_global,
        percentagem_conformidade=percentagem_global,
        nivel_minimo_exigido=nivel_minimo_global,
        tipo_entidade=empresa.tipo_entidade.value,
        nivel_qnrcs=(
            empresa.nivel_qnrcs.value
            if hasattr(empresa.nivel_qnrcs, "value") and empresa.nivel_qnrcs
            else empresa.nivel_qnrcs
        ),
        dominios=dominios_schema,
        resumo_controlos=resumo_controlos,
        controlos_criticos_em_falta=criticos_em_falta,
        data_calculo=datetime.now(timezone.utc),
        framework_id=framework.id,
        framework_nome=(
            framework_locale.name if framework_locale else framework.registry_id
        ),
        maturity_scale_max=framework.maturity_scale_max,
        maturity_radar_max=framework.maturity_radar_max,
        display_mode=framework.display_mode,
        features=framework.features,
        maturity_level_names=(
            framework_locale.maturity_level_names if framework_locale else None
        ),
    )


__all__ = [
    "alterar_estado",
    "aprovar_controlo",
    "calcular_conformidade_global_v2",
    "calcular_dashboard",
    "calcular_nivel_minimo_global",
    "concluir_check",
    "delegar_controlo",
    "get_controlo_detalhe",
    "get_historico_relatorios",
    "inicializar_controlos_empresa",
    "inicializar_controlos_empresa_v2",
    "listar_controlos",
    "listar_dominios",
    "obter_ids_controlos_obrigatorios_perfil_v2",
    "reprovar_controlo",
    "reverter_check",
]
