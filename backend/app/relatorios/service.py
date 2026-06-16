"""
Serviço do módulo de relatórios.
Gera relatórios estruturados de conformidade NIS2 / DL 125/2025.

Relatórios disponíveis:
  - Conformidade detalhada (todos os controlos por domínio)
  - Gap analysis (controlos não conformes ordenados por prioridade)
  - Histórico de maturidade (evolução temporal)
  - Resumo executivo (visão CEO em linguagem não técnica)
  - Exportação de dados RGPD (portabilidade Art. 20)

Nota: A geração DOCX (python-docx) será implementada numa fase futura.
      Por agora os relatórios são devolvidos como JSON estruturado,
      que o frontend pode renderizar e imprimir via CSS.
"""
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException, Request, status
from sqlmodel import Session, select

from app.auth.models import Utilizador
from app.controlos.models import HistoricoMaturidade
from app.controlos.schemas import ScoreDominioSchema
from app.controlos.service import (
    _load_ultimo_nivel_controlo,
    calcular_conformidade_global_v2,
    calcular_dashboard,
    calcular_nivel_minimo_global,
    obter_ids_controlos_obrigatorios_perfil_v2,
)
from app.empresas.models import Empresa
from app.frameworks.models import (
    Control,
    ControlLocale,
    ControloEmpresaV2,
    Domain,
    DomainLocale,
    Framework,
)
from app.frameworks.runtime import (
    ControlHierarchyRow,
    load_company_control_rows,
    load_preferred_locales,
    load_thresholds_map,
)
from app.relatorios.schemas import (
    ExportacaoDadosSchema,
    ExportacaoUtilizadorSchema,
    GapControloSchema,
    HistoricoDashboardSchema,
    HistoricoEntradaSchema,
    HistoricoExportacaoItemSchema,
    HistoricoExportacoesSchema,
    HistoricoSemanalPontoSchema,
    RelatorioConformidadeSchema,
    RelatorioControloSchema,
    RelatorioDominioSchema,
    RelatorioExecutivoSchema,
    RelatorioGapSchema,
)
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.pii import decifrar_pii
from app.shared.scoring import calcular_score_global
from app.shared.utils import resolver_locale

logger = logging.getLogger(__name__)
SEMANAS_EVOLUCAO_DASHBOARD = 24


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------


def _estado_geral_texto(percentagem: float, conforme: bool) -> str:
    """Gera texto de estado em linguagem não técnica para o resumo executivo."""
    if conforme and percentagem >= 80:
        return (
            "A sua organização atingiu o nível de conformidade exigido pela lei "
            "de cibersegurança. Continue a manter e melhorar as medidas implementadas."
        )
    if percentagem >= 60:
        return (
            "A sua organização está no bom caminho para a conformidade, mas ainda "
            "existem medidas importantes por implementar. Priorize os controlos críticos."
        )
    if percentagem >= 30:
        return (
            "A sua organização está a iniciar o processo de conformidade com a lei "
            "de cibersegurança. É necessário agir com urgência nas medidas prioritárias."
        )
    return (
        "A sua organização ainda não iniciou a implementação das medidas exigidas "
        "pela lei de cibersegurança. Recomenda-se iniciar imediatamente as medidas críticas."
    )


def _proximos_passos(
    controlos_criticos_nc: int,
    sem_implementador: int,
    por_aprovar: int,
) -> list[str]:
    """Gera lista de próximos passos prioritários em linguagem não técnica."""
    passos = []
    if controlos_criticos_nc > 0:
        passos.append(
            f"Implementar os {controlos_criticos_nc} controlos críticos em falta — "
            "estes são obrigatórios e bloqueiam a conformidade geral."
        )
    if sem_implementador > 0:
        passos.append(
            f"Atribuir um responsável aos {sem_implementador} controlos ainda sem responsável."
        )
    if por_aprovar > 0:
        passos.append(
            f"Solicitar aprovação dos {por_aprovar} controlos já implementados "
            "mas ainda pendentes de validação."
        )
    if not passos:
        passos.append(
            "Manter as medidas implementadas e rever periodicamente o estado de conformidade."
        )
    return passos


def _normalizar_utc(data: datetime) -> datetime:
    """Converte datetimes naive/aware para UTC consciente."""
    if data.tzinfo is None:
        return data.replace(tzinfo=timezone.utc)
    return data.astimezone(timezone.utc)


def _inicio_da_semana(data: datetime) -> datetime:
    """Normaliza uma data para o início da semana em UTC."""
    data = _normalizar_utc(data)

    inicio = data - timedelta(days=data.isoweekday() - 1)
    return inicio.replace(hour=0, minute=0, second=0, microsecond=0)


def _agregar_historico_dashboard(
    snapshots: list[tuple[datetime, float]],
    empresa_created_at: datetime | None,
) -> list[HistoricoSemanalPontoSchema]:
    """Agrega snapshots globais numa série semanal pronta para o dashboard."""
    if not snapshots:
        return []

    por_semana: dict[datetime, tuple[datetime, int]] = {}
    for data_snapshot, percentagem in snapshots:
        data_original = _normalizar_utc(data_snapshot)
        semana = _inicio_da_semana(data_original)
        atual = por_semana.get(semana)
        valor = round(percentagem or 0)
        if atual is None or data_original > atual[0]:
            por_semana[semana] = (data_original, valor)

    semanas_ordenadas = [
        {
            "data_original": data_original,
            "data_semana": data_semana,
            "valor": valor,
        }
        for data_semana, (data_original, valor) in por_semana.items()
    ]
    semanas_ordenadas.sort(key=lambda item: item["data_semana"])

    if not semanas_ordenadas:
        return []

    fim = _inicio_da_semana(datetime.now(timezone.utc))
    inicio = fim - timedelta(days=(SEMANAS_EVOLUCAO_DASHBOARD - 1) * 7)

    if empresa_created_at is not None:
        semana_criacao = _inicio_da_semana(empresa_created_at)
        if semana_criacao > inicio:
            inicio = semana_criacao

    serie: list[HistoricoSemanalPontoSchema] = []
    cursor = inicio
    ultimo_valor: int | None = None
    indice_snapshot = 0

    while (
        indice_snapshot < len(semanas_ordenadas)
        and semanas_ordenadas[indice_snapshot]["data_original"] < inicio
    ):
        ultimo_valor = semanas_ordenadas[indice_snapshot]["valor"]
        indice_snapshot += 1

    while cursor <= fim:
        semana_existente = por_semana.get(cursor)
        if semana_existente is not None:
            ultimo_valor = semana_existente[1]
        else:
            fim_semana = cursor + timedelta(
                days=6,
                hours=23,
                minutes=59,
                seconds=59,
                microseconds=999999,
            )
            while (
                indice_snapshot < len(semanas_ordenadas)
                and semanas_ordenadas[indice_snapshot]["data_original"] <= fim_semana
            ):
                ultimo_valor = semanas_ordenadas[indice_snapshot]["valor"]
                indice_snapshot += 1

        serie.append(
            HistoricoSemanalPontoSchema(
                semana_inicio=cursor,
                percentagem_conformidade=ultimo_valor or 0,
            )
        )
        cursor = cursor + timedelta(days=7)

    return serie


@dataclass
class V2DomainAccumulator:
    domain_id: uuid.UUID
    codigo: str
    nome: str
    score: int
    total_controlos: int = 0
    controlos_conformes: int = 0
    niveis_minimos: list[int] = field(default_factory=list)
    controlos: list[RelatorioControloSchema] = field(default_factory=list)


def _carregar_contexto_v2_relatorios(
    db: Session,
    empresa: Empresa,
    framework: Framework,
    locale: str,
    *,
    include_implementadores: bool = False,
) -> tuple[
    list[ControlHierarchyRow],
    dict[uuid.UUID, int],
    dict[uuid.UUID, DomainLocale],
    dict[uuid.UUID, ControlLocale],
    dict[uuid.UUID, Utilizador],
]:
    rows = load_company_control_rows(db, empresa.id, framework.id)
    thresholds_map = load_thresholds_map(db, framework, empresa)
    domain_locales = load_preferred_locales(
        db,
        DomainLocale,
        "domain_id",
        {row.domain.id for row in rows},
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

    implementadores: dict[uuid.UUID, Utilizador] = {}
    if include_implementadores:
        implementador_ids = {
            row.ce.implementador_id
            for row in rows
            if row.ce.implementador_id is not None
        }
        if implementador_ids:
            implementadores = {
                impl.id: impl
                for impl in db.exec(
                    select(Utilizador).where(
                        Utilizador.id.in_(list(implementador_ids))
                    )
                ).all()
            }

    return (
        rows,
        thresholds_map,
        domain_locales,
        control_locales,
        implementadores,
    )


def _get_or_create_v2_domain_accumulator(
    accumulators: dict[uuid.UUID, V2DomainAccumulator],
    domain_order: list[uuid.UUID],
    row: ControlHierarchyRow,
    framework: Framework,
    domain_locales: dict[uuid.UUID, DomainLocale],
) -> V2DomainAccumulator:
    accumulator = accumulators.get(row.domain.id)
    if accumulator is not None:
        return accumulator

    loc_dom = domain_locales.get(row.domain.id)
    accumulator = V2DomainAccumulator(
        domain_id=row.domain.id,
        codigo=row.domain.code,
        nome=loc_dom.name if loc_dom else row.domain.code,
        score=framework.maturity_scale_max,
    )
    accumulators[row.domain.id] = accumulator
    domain_order.append(row.domain.id)
    return accumulator


def _build_v2_scores_dominio(
    rows: list[ControlHierarchyRow],
    thresholds_map: dict[uuid.UUID, int],
    framework: Framework,
    domain_locales: dict[uuid.UUID, DomainLocale],
    obrigatorios_ids: set[uuid.UUID] | None = None,
    ultimo_nivel_map: dict[uuid.UUID, int] | None = None,
) -> tuple[list[ScoreDominioSchema], int]:
    accumulators: dict[uuid.UUID, V2DomainAccumulator] = {}
    domain_order: list[uuid.UUID] = []
    score_por_dominio: dict[str, int] = {}
    controlos_criticos: list[ControloEmpresaV2] = []

    for row in rows:
        accumulator = _get_or_create_v2_domain_accumulator(
            accumulators,
            domain_order,
            row,
            framework,
            domain_locales,
        )
        nivel_min = thresholds_map.get(
            row.control.id,
            framework.maturity_scale_min,
        )
        # Limita o mínimo exigido ao nível máximo alcançável do controlo
        if ultimo_nivel_map:
            ultimo = ultimo_nivel_map.get(row.control.id)
            if ultimo is not None:
                nivel_min = min(nivel_min, ultimo)
        obrigatorio = (
            obrigatorios_ids is None
            or row.control.id in obrigatorios_ids
        )
        if obrigatorio:
            accumulator.total_controlos += 1
        accumulator.score = min(
            accumulator.score,
            row.ce.nivel_maturidade_atual,
        )
        if obrigatorio and row.ce.nivel_maturidade_atual >= nivel_min:
            accumulator.controlos_conformes += 1
        if row.control.criticality == "critical":
            controlos_criticos.append(row.ce)

    scores_dominio: list[ScoreDominioSchema] = []
    for domain_id in domain_order:
        accumulator = accumulators[domain_id]
        percentagem = 0.0
        if accumulator.total_controlos:
            percentagem = round(
                accumulator.controlos_conformes
                / accumulator.total_controlos
                * 100,
                1,
            )

        scores_dominio.append(
            ScoreDominioSchema(
                dominio_id=accumulator.domain_id,
                codigo=accumulator.codigo,
                nome=accumulator.nome,
                score=accumulator.score,
                total_controlos=accumulator.total_controlos,
                controlos_conformidade=accumulator.controlos_conformes,
                percentagem_conformidade=percentagem,
            )
        )
        score_por_dominio[accumulator.codigo] = accumulator.score

    score_global = calcular_score_global(score_por_dominio, controlos_criticos)
    return scores_dominio, score_global


# ---------------------------------------------------------------------------
# Relatório de conformidade detalhado
# ---------------------------------------------------------------------------


def gerar_relatorio_conformidade(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    request: Request | None = None,
) -> RelatorioConformidadeSchema:
    """Gera o relatório detalhado de conformidade — V2."""
    return _gerar_relatorio_conformidade_v2(db, empresa, utilizador, request)


# ---------------------------------------------------------------------------


def _gerar_relatorio_conformidade_v2(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    request: Request | None = None,
) -> RelatorioConformidadeSchema:
    framework = db.get(Framework, empresa.framework_id)
    if not framework:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Framework da empresa não encontrado.")

    locale = resolver_locale(empresa, framework, request=request)
    (
        rows,
        thresholds_map,
        domain_locales,
        control_locales,
        implementadores,
    ) = _carregar_contexto_v2_relatorios(
        db,
        empresa,
        framework,
        locale,
        include_implementadores=True,
    )
    controlos_obrigatorios_ids = obter_ids_controlos_obrigatorios_perfil_v2(
        db,
        rows,
        empresa,
        framework,
        thresholds_map,
    )
    ultimo_nivel_map = _load_ultimo_nivel_controlo(db, {row.control.id for row in rows})
    _, score_global = _build_v2_scores_dominio(
        rows,
        thresholds_map,
        framework,
        domain_locales,
        controlos_obrigatorios_ids,
        ultimo_nivel_map,
    )

    dominios_schema: list[RelatorioDominioSchema] = []
    criticos_nc = 0

    accumulators: dict[uuid.UUID, V2DomainAccumulator] = {}
    domain_order: list[uuid.UUID] = []

    for row in rows:
        accumulator = _get_or_create_v2_domain_accumulator(
            accumulators,
            domain_order,
            row,
            framework,
            domain_locales,
        )
        nivel_min = thresholds_map.get(
            row.control.id,
            framework.maturity_scale_min,
        )
        critico = row.control.criticality == "critical"
        obrigatorio = row.control.id in controlos_obrigatorios_ids
        conforme = row.ce.nivel_maturidade_atual >= nivel_min
        gap = max(0, nivel_min - row.ce.nivel_maturidade_atual)

        accumulator.score = min(
            accumulator.score,
            row.ce.nivel_maturidade_atual,
        )
        if obrigatorio:
            accumulator.total_controlos += 1
            accumulator.niveis_minimos.append(nivel_min)

            if conforme:
                accumulator.controlos_conformes += 1
            elif critico:
                criticos_nc += 1

        implementador = implementadores.get(row.ce.implementador_id)
        control_locale = control_locales.get(row.control.id)
        accumulator.controlos.append(
            RelatorioControloSchema(
                controlo_id=row.control.id,
                codigo=row.control.code,
                titulo=control_locale.title if control_locale else row.control.code,
                dominio_codigo=row.domain.code,
                dominio_nome=accumulator.nome,
                critico=critico,
                estado=(
                    row.ce.estado.value
                    if hasattr(row.ce.estado, "value")
                    else row.ce.estado
                ),
                nivel_maturidade_atual=row.ce.nivel_maturidade_atual,
                nivel_minimo_exigido=nivel_min,
                conforme=conforme,
                gap=gap,
                obrigatorio_perfil=obrigatorio,
                implementador_nome=(
                    decifrar_pii(implementador.nome) if implementador else None
                ),
                data_aprovacao=row.ce.data_aprovacao,
            )
        )

    for domain_id in domain_order:
        accumulator = accumulators[domain_id]
        perc_dom = 0.0
        if accumulator.total_controlos:
            perc_dom = (
                accumulator.controlos_conformes
                / accumulator.total_controlos
                * 100
            )

        dominios_schema.append(
            RelatorioDominioSchema(
                dominio_id=accumulator.domain_id,
                codigo=accumulator.codigo,
                nome=accumulator.nome,
                score=accumulator.score,
                nivel_minimo_exigido=min(
                    accumulator.niveis_minimos,
                    default=framework.maturity_scale_min,
                ),
                controlos=accumulator.controlos,
                total_controlos=accumulator.total_controlos,
                controlos_conformes=accumulator.controlos_conformes,
                percentagem_conformidade=round(perc_dom, 1),
            )
        )
    perc_global, controlos_conformes_globais, _ = (
        calcular_conformidade_global_v2(
            db,
            rows,
            empresa,
            framework,
            thresholds_map,
        )
    )
    total_controlos_globais = sum(acc.total_controlos for acc in accumulators.values())

    nivel_min_global = calcular_nivel_minimo_global(
        thresholds_map,
        framework,
    )

    relatorio = RelatorioConformidadeSchema(
        id=uuid.uuid4(),
        gerado_em=datetime.now(timezone.utc),
        gerado_por_nome=decifrar_pii(utilizador.nome),
        gerado_por_role=utilizador.role,
        versao_framework=f"{framework.registry_id}",
        empresa_id=empresa.id,
        empresa_nome=decifrar_pii(empresa.nome),
        empresa_tipo_entidade=empresa.tipo_entidade.value,
        empresa_setor=empresa.setor,
        score_global=score_global,
        percentagem_conformidade_global=round(perc_global, 1),
        nivel_minimo_global=nivel_min_global,
        dominios=dominios_schema,
        total_controlos=total_controlos_globais,
        controlos_conformes=controlos_conformes_globais,
        controlos_criticos_nao_conformes=criticos_nc,
    )

    registar_acao(
        db, acao=Acao.RELATORIO_EXPORTADO, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa.id, utilizador_id=utilizador.id,
        dados_novos={"tipo": "conformidade", "id": str(relatorio.id)}, request=request,
    )
    db.commit()
    return relatorio


# ---------------------------------------------------------------------------
# Gap analysis
# ---------------------------------------------------------------------------


def gerar_relatorio_gap(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    request: Request | None = None,
) -> RelatorioGapSchema:
    """Gera análise de lacunas — V2."""
    return _gerar_relatorio_gap_v2(db, empresa, utilizador, request)


# ---------------------------------------------------------------------------


def _gerar_relatorio_gap_v2(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    request: Request | None = None,
) -> RelatorioGapSchema:
    """Variante V2 do gap analysis — usa tabelas V2."""
    framework = db.get(Framework, empresa.framework_id)
    if not framework:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Framework da empresa não encontrado.")

    locale = resolver_locale(empresa, framework, request=request)
    (
        rows,
        thresholds_map,
        domain_locales,
        control_locales,
        implementadores,
    ) = _carregar_contexto_v2_relatorios(
        db,
        empresa,
        framework,
        locale,
        include_implementadores=True,
    )
    controlos_obrigatorios_ids = obter_ids_controlos_obrigatorios_perfil_v2(
        db,
        rows,
        empresa,
        framework,
        thresholds_map,
    )
    ultimo_nivel_map = _load_ultimo_nivel_controlo(db, {row.control.id for row in rows})
    _, score_global = _build_v2_scores_dominio(
        rows,
        thresholds_map,
        framework,
        domain_locales,
        controlos_obrigatorios_ids,
        ultimo_nivel_map,
    )

    gaps: list[GapControloSchema] = []

    for row in rows:
        if row.control.id not in controlos_obrigatorios_ids:
            continue

        nivel_min = thresholds_map.get(
            row.control.id,
            framework.maturity_scale_min,
        )
        if row.ce.nivel_maturidade_atual >= nivel_min:
            continue  # conforme

        gap_val = nivel_min - row.ce.nivel_maturidade_atual
        critico = row.control.criticality == "critical"
        prioridade = "alta" if critico else ("media" if gap_val >= 2 else "baixa")

        domain_locale = domain_locales.get(row.domain.id)
        control_locale = control_locales.get(row.control.id)
        implementador = implementadores.get(row.ce.implementador_id)

        gaps.append(
            GapControloSchema(
                controlo_id=row.control.id,
                codigo=row.control.code,
                titulo=(
                    control_locale.title
                    if control_locale
                    else row.control.code
                ),
                dominio_nome=(
                    domain_locale.name
                    if domain_locale
                    else row.domain.code
                ),
                critico=critico,
                nivel_atual=row.ce.nivel_maturidade_atual,
                nivel_minimo=nivel_min,
                gap=gap_val,
                estado=(
                    row.ce.estado.value
                    if hasattr(row.ce.estado, "value")
                    else row.ce.estado
                ),
                prioridade=prioridade,
                implementador_nome=(
                    decifrar_pii(implementador.nome) if implementador else None
                ),
            )
        )

    gaps.sort(key=lambda g: (0 if g.critico else 1, -g.gap))

    perc_global, _, _ = calcular_conformidade_global_v2(
        db,
        rows,
        empresa,
        framework,
        thresholds_map,
    )

    relatorio = RelatorioGapSchema(
        id=uuid.uuid4(),
        gerado_em=datetime.now(timezone.utc),
        empresa_id=empresa.id,
        empresa_nome=decifrar_pii(empresa.nome),
        empresa_tipo_entidade=empresa.tipo_entidade.value,
        score_global=score_global,
        percentagem_conformidade_global=round(perc_global, 1),
        gaps=gaps,
        total_gaps=len(gaps),
        gaps_criticos=sum(1 for g in gaps if g.critico),
    )

    registar_acao(
        db, acao=Acao.RELATORIO_EXPORTADO, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa.id, utilizador_id=utilizador.id,
        dados_novos={"tipo": "gap", "id": str(relatorio.id), "total_gaps": len(gaps)},
        request=request,
    )
    db.commit()
    return relatorio


# ---------------------------------------------------------------------------
# Histórico de maturidade
# ---------------------------------------------------------------------------


def gerar_relatorio_historico(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    periodo_meses: int = 12,
) -> HistoricoDashboardSchema:
    """Devolve apenas a série semanal global necessária ao dashboard."""
    desde = datetime.now(timezone.utc) - timedelta(days=periodo_meses * 30)

    snapshots = db.exec(
        select(
            HistoricoMaturidade.data_snapshot,
            HistoricoMaturidade.percentagem_conformidade,
        ).where(
            HistoricoMaturidade.empresa_id == empresa.id,
            HistoricoMaturidade.dominio_id.is_(None),
            HistoricoMaturidade.data_snapshot >= desde,
        ).order_by(HistoricoMaturidade.data_snapshot)
    ).all()

    return HistoricoDashboardSchema(
        pontos=_agregar_historico_dashboard(snapshots, empresa.created_at),
    )


# ---------------------------------------------------------------------------
# Resumo executivo
# ---------------------------------------------------------------------------


def gerar_resumo_executivo(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    request: Request | None = None,
) -> RelatorioExecutivoSchema:
    """
    Gera resumo executivo em linguagem não técnica.
    Disponível para: admin, auditor, ceo.
    """
    dashboard = calcular_dashboard(db, empresa)

    nivel_min_global = dashboard.nivel_minimo_exigido
    conforme = dashboard.score_global >= nivel_min_global

    framework = db.get(Framework, empresa.framework_id)
    if not framework:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Framework da empresa não encontrado.")

    locale = resolver_locale(empresa, framework, request=request)
    rows, thresholds_map, domain_locales, _, _ = _carregar_contexto_v2_relatorios(
        db,
        empresa,
        framework,
        locale,
    )
    controlos_obrigatorios_ids = obter_ids_controlos_obrigatorios_perfil_v2(
        db,
        rows,
        empresa,
        framework,
        thresholds_map,
    )
    ultimo_nivel_map = _load_ultimo_nivel_controlo(db, {row.control.id for row in rows})
    scores_dominio, score_global = _build_v2_scores_dominio(
        rows,
        thresholds_map,
        framework,
        domain_locales,
        controlos_obrigatorios_ids,
        ultimo_nivel_map,
    )
    criticos_nc = 0
    sem_impl = 0
    por_aprovar = 0
    for row in rows:
        nivel_min = thresholds_map.get(
            row.control.id,
            framework.maturity_scale_min,
        )
        if (
            row.control.id in controlos_obrigatorios_ids
            and
            row.ce.nivel_maturidade_atual < nivel_min
            and row.control.criticality == "critical"
        ):
            criticos_nc += 1
        if row.ce.implementador_id is None:
            sem_impl += 1
        estado_val = (
            row.ce.estado.value
            if hasattr(row.ce.estado, "value")
            else row.ce.estado
        )
        if estado_val == "implementado":
            por_aprovar += 1
    perc_global, _, _ = calcular_conformidade_global_v2(
        db,
        rows,
        empresa,
        framework,
        thresholds_map,
    )
    conforme = score_global >= nivel_min_global

    relatorio = RelatorioExecutivoSchema(
        id=uuid.uuid4(),
        gerado_em=datetime.now(timezone.utc),
        empresa_id=empresa.id,
        empresa_nome=decifrar_pii(empresa.nome),
        empresa_tipo_entidade=empresa.tipo_entidade.value,
        percentagem_conformidade_global=round(perc_global, 1),
        score_global=score_global,
        nivel_minimo_global=nivel_min_global,
        conforme=conforme,
        scores_dominio=scores_dominio,
        numero_controlos_criticos_nao_conformes=criticos_nc,
        numero_controlos_sem_implementador=sem_impl,
        numero_controlos_por_aprovar=por_aprovar,
        estado_geral=_estado_geral_texto(perc_global, conforme),
        proximos_passos=_proximos_passos(criticos_nc, sem_impl, por_aprovar),
    )

    registar_acao(
        db,
        acao=Acao.RELATORIO_EXPORTADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa.id,
        utilizador_id=utilizador.id,
        dados_novos={"tipo": "executivo", "id": str(relatorio.id)},
        request=request,
    )
    db.commit()

    return relatorio


# ---------------------------------------------------------------------------
# Exportação de dados RGPD (portabilidade — Art. 20)
# ---------------------------------------------------------------------------


def exportar_dados_empresa(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    request: Request | None = None,
) -> ExportacaoDadosSchema:
    """
    Exporta todos os dados da empresa para portabilidade RGPD (Art. 20).
    Apenas admin pode solicitar.
    Não inclui ficheiros de evidências (disponíveis via download individual).
    """
    # Utilizadores da empresa
    utilizadores = db.exec(
        select(Utilizador).where(
            Utilizador.empresa_id == empresa.id,
            Utilizador.deleted_at.is_(None),  # type: ignore[attr-defined]
        )
    ).all()

    utils_schema = [
        ExportacaoUtilizadorSchema(
            id=u.id,
            email=u.email,
            nome=decifrar_pii(u.nome),
            role=u.role.value,
            ativo=u.ativo,
            consentimento_termos_at=u.consentimento_termos_at,
            consentimento_termos_versao=u.consentimento_termos_versao,
            created_at=u.created_at,
        )
        for u in utilizadores
    ]

    # Estado dos controlos (controlo, estado, maturidade, data aprovação)
    framework = db.get(Framework, empresa.framework_id)
    if not framework:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Framework da empresa não encontrado.")

    locale = resolver_locale(empresa, framework, request=request)
    rows, _, _, control_locales, _ = _carregar_contexto_v2_relatorios(
        db,
        empresa,
        framework,
        locale,
    )
    estado_controlos = []
    for row in rows:
        loc = control_locales.get(row.control.id)
        estado_controlos.append({
            "controlo_codigo": row.control.code,
            "controlo_titulo": (
                loc.title if loc else row.control.code
            ),
            "estado": (
                row.ce.estado.value
                if hasattr(row.ce.estado, "value")
                else row.ce.estado
            ),
            "nivel_maturidade_atual": row.ce.nivel_maturidade_atual,
            "data_aprovacao": (
                row.ce.data_aprovacao.isoformat()
                if row.ce.data_aprovacao
                else None
            ),
        })
    # Histórico de maturidade
    snapshots = db.exec(
        select(HistoricoMaturidade).where(
            HistoricoMaturidade.empresa_id == empresa.id
        ).order_by(HistoricoMaturidade.data_snapshot)
    ).all()

    historico = [
        {
            "data_snapshot": s.data_snapshot.isoformat(),
            "dominio_id": str(s.dominio_id) if s.dominio_id else None,
            "nivel_maturidade": s.nivel_maturidade,
            "percentagem_conformidade": s.percentagem_conformidade,
        }
        for s in snapshots
    ]

    exportacao = ExportacaoDadosSchema(
        exportado_em=datetime.now(timezone.utc),
        empresa_id=empresa.id,
        empresa_nome=decifrar_pii(empresa.nome),
        empresa_nif=decifrar_pii(empresa.nif),
        empresa_email=decifrar_pii(empresa.email),
        empresa_setor=empresa.setor,
        empresa_tipo_entidade=empresa.tipo_entidade.value,
        utilizadores=utils_schema,
        estado_controlos=estado_controlos,
        historico_maturidade=historico,
    )

    registar_acao(
        db,
        acao=Acao.EMPRESA_DADOS_EXPORTADOS,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa.id,
        utilizador_id=utilizador.id,
        dados_novos={"tipo": "exportacao_rgpd"},
        request=request,
    )
    db.commit()

    return exportacao


# ---------------------------------------------------------------------------
# Histórico de exportações (consulta AuditLog)
# ---------------------------------------------------------------------------


def listar_historico_exportacoes(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    limite: int = 10,
    offset: int = 0,
) -> HistoricoExportacoesSchema:
    """
    Lista exportações de relatórios da empresa a partir do AuditLog (paginada).
    A tabela AuditLog é append-only — leitura segura sem risco de modificação.
    """
    import json as _json

    from sqlalchemy import func as _func
    from app.shared.audit import AuditLog

    base_filter = [
        AuditLog.empresa_id == empresa.id,
        AuditLog.acao == Acao.RELATORIO_EXPORTADO,
    ]

    total_registos: int = db.exec(
        select(_func.count()).select_from(AuditLog).where(*base_filter)
    ).one()

    stmt = (
        select(AuditLog, Utilizador)
        .join(Utilizador, AuditLog.utilizador_id == Utilizador.id, isouter=True)  # type: ignore[arg-type]
        .where(*base_filter)
        .order_by(AuditLog.created_at.desc())
        .offset(offset)
        .limit(limite)
    )
    rows = db.exec(stmt).all()

    items: list[HistoricoExportacaoItemSchema] = []
    for log, util in rows:
        tipo = "—"
        if log.dados_novos:
            try:
                tipo = _json.loads(log.dados_novos).get("tipo", "—")
            except Exception:
                logger.warning("JSON inválido no AuditLog %s — campo dados_novos não parséavel", log.id)
        items.append(
            HistoricoExportacaoItemSchema(
                id=log.id,
                tipo_relatorio=tipo,
                utilizador_nome=decifrar_pii(util.nome) if util else None,
                data=log.created_at,
            )
        )

    return HistoricoExportacoesSchema(total_registos=total_registos, limite=limite, offset=offset, exportacoes=items)
