"""
Router do módulo de relatórios.
Geração de relatórios de conformidade NIS2 / DL 125/2025.

Prefixo base: /api (incluído em main.py)
Prefixo do router: /relatorios
"""
from fastapi import APIRouter, Depends, Request

from app.auth.models import RoleUtilizador
from app.shared.dependencies import CurrentUserDep, SessionDep, get_empresa_ativa, require_role
from app.relatorios import schemas, service


router = APIRouter(prefix="/relatorios", tags=["Relatórios"])

# ---------------------------------------------------------------------------
# Dependências de role
# ---------------------------------------------------------------------------

# CEO, admin, subadmin e auditor acedem a todos os relatórios de leitura
RelatorioReadDep = Depends(
    require_role(RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN, RoleUtilizador.AUDITOR, RoleUtilizador.CEO)
)

# Histórico semanal do dashboard — disponível a todos os perfis autenticados
HistoricoReadDep = Depends(
    require_role(
        RoleUtilizador.ADMIN,
        RoleUtilizador.SUBADMIN,
        RoleUtilizador.AUDITOR,
        RoleUtilizador.CEO,
        RoleUtilizador.IMPLEMENTADOR,
    )
)

# Apenas admin, subadmin e auditor acedem ao gap e conformidade detalhada
GapReadDep = Depends(require_role(RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN, RoleUtilizador.AUDITOR))

# Apenas admin e subadmin podem exportar dados RGPD
AdminDep = Depends(require_role(RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN))


# ---------------------------------------------------------------------------
# Relatórios
# ---------------------------------------------------------------------------


@router.get(
    "/conformidade",
    response_model=schemas.RelatorioConformidadeSchema,
    summary="Relatório detalhado de conformidade",
    dependencies=[GapReadDep],
)
def relatorio_conformidade(
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
):
    """
    Relatório completo de conformidade NIS2 / DL 125/2025.
    Mostra todos os domínios, controlos, níveis atuais e gaps.
    Disponível para admin e auditor.
    """
    empresa = get_empresa_ativa(db, utilizador_atual)
    return service.gerar_relatorio_conformidade(
        db, empresa, utilizador_atual, request=request
    )


@router.get(
    "/gap",
    response_model=schemas.RelatorioGapSchema,
    summary="Análise de lacunas (gap analysis)",
    dependencies=[GapReadDep],
)
def relatorio_gap(
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
):
    """
    Lista de controlos não conformes ordenados por prioridade.
    Ferramenta de trabalho para planeamento da implementação.
    Disponível para admin e auditor.
    """
    empresa = get_empresa_ativa(db, utilizador_atual)
    return service.gerar_relatorio_gap(
        db, empresa, utilizador_atual, request=request
    )


@router.get(
    "/historico",
    response_model=schemas.HistoricoDashboardSchema,
    summary="Histórico semanal global para o dashboard",
    dependencies=[HistoricoReadDep],
)
def relatorio_historico(
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    periodo_meses: int = 12,
):
    """
    Série semanal global de percentagem de conformidade.
    Dados mínimos para o gráfico de progresso no dashboard.
    Período máximo: 24 meses. Default: 12 meses.
    """
    periodo = min(max(1, periodo_meses), 24)  # limita entre 1 e 24 meses
    empresa = get_empresa_ativa(db, utilizador_atual)
    return service.gerar_relatorio_historico(
        db, empresa, utilizador_atual, periodo_meses=periodo
    )


@router.get(
    "/executivo",
    response_model=schemas.RelatorioExecutivoSchema,
    summary="Resumo executivo (visão CEO)",
    dependencies=[RelatorioReadDep],
)
def relatorio_executivo(
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
):
    """
    Resumo executivo em linguagem não técnica.
    Foco em conformidade legal e riscos de alto nível.
    Disponível para admin, auditor e CEO.
    """
    empresa = get_empresa_ativa(db, utilizador_atual)
    return service.gerar_resumo_executivo(
        db, empresa, utilizador_atual, request=request
    )


@router.get(
    "/historico-exportacoes",
    response_model=schemas.HistoricoExportacoesSchema,
    summary="Histórico de exportações de relatórios",
    dependencies=[RelatorioReadDep],
)
def historico_exportacoes(
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    limite: int = 10,
    offset: int = 0,
):
    """
    Lista exportações de relatórios registadas no AuditLog (paginada).
    Disponível para admin, auditor e CEO.
    Máximo por página: 100.
    """
    empresa = get_empresa_ativa(db, utilizador_atual)
    return service.listar_historico_exportacoes(
        db, empresa, utilizador_atual,
        limite=min(max(1, limite), 100),
        offset=max(0, offset),
    )


@router.get(
    "/exportar-dados",
    response_model=schemas.ExportacaoDadosSchema,
    summary="Exportar dados da empresa (RGPD Art. 20)",
    dependencies=[AdminDep],
)
def exportar_dados(
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
):
    """
    Exporta todos os dados da empresa em formato estruturado.
    Cumpre o direito à portabilidade de dados (RGPD Art. 20).
    Apenas admin pode solicitar esta exportação.
    Ficheiros de evidências devem ser descarregados individualmente.
    """
    empresa = get_empresa_ativa(db, utilizador_atual)
    return service.exportar_dados_empresa(
        db, empresa, utilizador_atual, request=request
    )
