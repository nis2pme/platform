"""
Router do módulo de relatórios.
Geração de relatórios de conformidade NIS2 / DL 125/2025.

Prefixo base: /api (incluído em main.py)
Prefixo do router: /relatorios
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.premium import exportacao as exportacao_premium
from app.premium.exportacao_client import ExportacaoClient, get_exportacao_client
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, SessionDep, get_empresa_ativa
from app.relatorios import schemas, service


router = APIRouter(prefix="/relatorios", tags=["Relatórios"])

# ---------------------------------------------------------------------------
# Dependências de acesso
# ---------------------------------------------------------------------------

RelatorioReadDep = Depends(require_capability("relatorios", ClasseAcao.VER))

# Exportação estruturada (RGPD): sai mais dados da aplicação do que numa leitura,
# por isso é classe própria e não a mesma do "ver".
ExportarDep = Depends(require_capability("relatorios", ClasseAcao.EXPORTAR))



# ---------------------------------------------------------------------------
# Relatórios
# ---------------------------------------------------------------------------


@router.get(
    "/conformidade",
    response_model=schemas.RelatorioConformidadeSchema,
    summary="Relatório detalhado de conformidade",
    dependencies=[RelatorioReadDep],
)
def relatorio_conformidade(
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
):
    """
    Relatório completo de conformidade NIS2.
    Mostra todos os objetivos, controlos, níveis atuais e gaps.
    Disponível à administração, ao auditor e ao órgão de gestão — que é quem
    responde pela aprovação das medidas.
    """
    empresa = get_empresa_ativa(db, utilizador_atual)
    return service.gerar_relatorio_conformidade(
        db, empresa, utilizador_atual, request=request
    )


@router.get(
    "/matriz-capacidades",
    summary="Documento de funções e responsabilidades (GR.FR-3)",
    dependencies=[RelatorioReadDep],
)
def relatorio_matriz_capacidades(request: Request, utilizador: CurrentUserDep):
    """
    Documento-evidência da matriz de capacidades (quem pode o quê por módulo).
    Serve o registo documentado de funções e responsabilidades (QNRCS GR.FR-3).
    Payload localizado no formato dos documentos-evidência; o PDF é gerado no cliente.
    """
    from app.shared.capacidades import documento_matriz_capacidades
    from app.shared.i18n import locale_de_request

    return documento_matriz_capacidades(
        locale_de_request(request), utilizador.empresa_id
    )


@router.get(
    "/gap",
    response_model=schemas.RelatorioGapSchema,
    summary="Análise de lacunas (gap analysis)",
    dependencies=[RelatorioReadDep],
)
def relatorio_gap(
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
):
    """
    Lista de controlos não conformes ordenados por prioridade.
    Ferramenta de trabalho para planeamento da implementação.
    """
    empresa = get_empresa_ativa(db, utilizador_atual)
    return service.gerar_relatorio_gap(
        db, empresa, utilizador_atual, request=request
    )


@router.get(
    "/historico",
    response_model=schemas.HistoricoDashboardSchema,
    summary="Histórico semanal global para o dashboard",
    # A série é a conformidade da EMPRESA, e é isso que a leitura de relatórios
    # decide quem alcança — sem este gate, quem tem os relatórios fechados via
    # na mesma a evolução global pelo painel. Quem fica sem ela vê o painel
    # sem o gráfico (o carregamento degrada sozinho), e a organização pode
    # abri-la a quem implementa pela política de permissões.
    dependencies=[RelatorioReadDep],
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
    dependencies=[ExportarDep],
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


@router.get(
    "/exportar-dados/premium",
    summary="Exportar os dados dos módulos premium (zip)",
    dependencies=[ExportarDep],
)
def exportar_dados_premium(
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
    cli: ExportacaoClient | None = Depends(get_exportacao_client),
):
    """
    Os dados dos módulos premium da empresa (inventário, riscos, fornecedores,
    importações, conetores sem credenciais, verificações, análises de IA), num
    zip com JSON e CSV. Os dados são do cliente: sai em qualquer estado da
    licença, por isso não passa pelo portão premium — só pela capacidade de
    exportar, a mesma da exportação RGPD ao lado. A empresa é sempre a da
    sessão. Sai em stream (o zip faz-se à medida que os dados chegam).
    """
    empresa = get_empresa_ativa(db, utilizador_atual)
    if cli is None:
        # Instalação sem o componente premium: não há dados premium a exportar.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"codigo": "sem_premium"})
    # A primeira parte pede-se já: uma recusa do sidecar (ocupado, em baixo)
    # sai como erro HTTP, antes de a resposta começar e de ficar na trilha.
    partes = exportacao_premium.abrir(cli.partes_de(str(empresa.id)))
    registar_acao(
        db,
        acao=Acao.EMPRESA_DADOS_EXPORTADOS,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa.id,
        utilizador_id=utilizador_atual.id,
        dados_novos={"tipo": "exportacao_premium"},
        request=request,
    )
    db.commit()
    nome = f"dados_premium_{datetime.now(timezone.utc).date().isoformat()}.zip"
    return StreamingResponse(
        exportacao_premium.zip_em_stream(partes),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{nome}"', "Cache-Control": "no-store"},
    )
