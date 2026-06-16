"""
Schemas do módulo de relatórios.
Estruturas de dados para os diferentes tipos de relatório exportáveis.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel

from app.auth.models import RoleUtilizador
from app.controlos.schemas import ScoreDominioSchema


# ---------------------------------------------------------------------------
# Relatório de conformidade — por controlo dentro de domínio
# ---------------------------------------------------------------------------


class RelatorioControloSchema(BaseModel):
    """Snapshot de um controlo para inclusão num relatório."""

    controlo_id: uuid.UUID
    codigo: str
    titulo: str
    dominio_codigo: str
    dominio_nome: str
    critico: bool
    estado: str
    nivel_maturidade_atual: int
    nivel_minimo_exigido: int
    conforme: bool                    # nivel_atual >= nivel_minimo
    gap: int                          # nivel_minimo - nivel_atual (0 se conforme)
    obrigatorio_perfil: bool           # True se obrigatório para o perfil da empresa
    implementador_nome: str | None    # nome do implementador delegado
    data_aprovacao: datetime | None


class RelatorioDominioSchema(BaseModel):
    """Agregado de controlos por domínio, com score."""

    dominio_id: uuid.UUID
    codigo: str
    nome: str
    score: int
    nivel_minimo_exigido: int          # mínimo exigido para o tipo de entidade
    controlos: list[RelatorioControloSchema]
    total_controlos: int
    controlos_conformes: int
    percentagem_conformidade: float


class RelatorioConformidadeSchema(BaseModel):
    """
    Relatório completo de conformidade NIS2/DL 125/2025.
    Inclui todos os domínios e controlos, com score global.
    """

    # Metadados do relatório
    id: uuid.UUID
    gerado_em: datetime
    gerado_por_nome: str
    gerado_por_role: RoleUtilizador
    versao_framework: str             # ex: "UCF v1.0 — DL 125/2025"

    # Dados da empresa
    empresa_id: uuid.UUID
    empresa_nome: str
    empresa_tipo_entidade: str
    empresa_setor: str | None

    # Score global
    score_global: int
    percentagem_conformidade_global: float
    nivel_minimo_global: int          # mínimo exigido para a categoria

    # Domínios
    dominios: list[RelatorioDominioSchema]

    # Sumário
    total_controlos: int
    controlos_conformes: int
    controlos_criticos_nao_conformes: int


# ---------------------------------------------------------------------------
# Relatório de lacunas (gap analysis)
# ---------------------------------------------------------------------------


class GapControloSchema(BaseModel):
    """Controlo com gap de conformidade."""

    controlo_id: uuid.UUID
    codigo: str
    titulo: str
    dominio_nome: str
    critico: bool
    nivel_atual: int
    nivel_minimo: int
    gap: int
    estado: str
    prioridade: str                   # "alta" (critico) | "media" | "baixa"
    implementador_nome: str | None


class RelatorioGapSchema(BaseModel):
    """
    Análise de lacunas — lista de controlos não conformes ordenados por prioridade.
    Ferramenta de trabalho para o implementador/admin.
    """

    id: uuid.UUID
    gerado_em: datetime
    empresa_id: uuid.UUID
    empresa_nome: str
    empresa_tipo_entidade: str

    score_global: int
    percentagem_conformidade_global: float

    gaps: list[GapControloSchema]     # ordenado: criticos primeiro, depois por gap desc
    total_gaps: int
    gaps_criticos: int


# ---------------------------------------------------------------------------
# Relatório de histórico de maturidade
# ---------------------------------------------------------------------------


class HistoricoEntradaSchema(BaseModel):
    """Ponto de dados do histórico de maturidade."""

    data_snapshot: datetime
    dominio_id: uuid.UUID | None      # None = score global
    dominio_codigo: str | None
    dominio_nome: str | None
    nivel_maturidade: float
    percentagem_conformidade: float


class HistoricoSemanalPontoSchema(BaseModel):
    """Ponto semanal já agregado para o gráfico do dashboard."""

    semana_inicio: datetime
    percentagem_conformidade: int


class HistoricoDashboardSchema(BaseModel):
    """Resposta mínima do histórico usada no dashboard."""

    pontos: list[HistoricoSemanalPontoSchema]


# ---------------------------------------------------------------------------
# Resumo executivo (CEO view)
# ---------------------------------------------------------------------------


class RelatorioExecutivoSchema(BaseModel):
    """
    Resumo executivo para o CEO — sem detalhes técnicos.
    Foco em conformidade legal e riscos de alto nível.
    """

    id: uuid.UUID
    gerado_em: datetime
    empresa_id: uuid.UUID
    empresa_nome: str
    empresa_tipo_entidade: str

    # Conformidade geral
    percentagem_conformidade_global: float
    score_global: int
    nivel_minimo_global: int
    conforme: bool                    # score_global >= nivel_minimo_global

    # Scores dos 5 domínios CyFun
    scores_dominio: list[ScoreDominioSchema]

    # Alertas executivos
    numero_controlos_criticos_nao_conformes: int
    numero_controlos_sem_implementador: int
    numero_controlos_por_aprovar: int

    # Mensagem de estado (linguagem não técnica)
    estado_geral: str                 # ex: "A sua organização ainda não atingiu..."
    proximos_passos: list[str]        # lista de ações prioritárias em linguagem simples


# ---------------------------------------------------------------------------
# Exportação de dados RGPD (portabilidade — Art. 20 RGPD)
# ---------------------------------------------------------------------------


class ExportacaoUtilizadorSchema(BaseModel):
    """Dados de um utilizador para exportação RGPD."""

    id: uuid.UUID
    email: str
    nome: str
    role: str
    ativo: bool
    consentimento_termos_at: datetime | None
    consentimento_termos_versao: str | None
    created_at: datetime


class ExportacaoDadosSchema(BaseModel):
    """
    Exportação completa dos dados da empresa (RGPD Art. 20 — portabilidade).
    Inclui todos os dados da empresa, utilizadores e state dos controlos.
    Não inclui ficheiros de evidências (disponíveis via download individual).
    """

    exportado_em: datetime
    empresa_id: uuid.UUID
    empresa_nome: str
    empresa_nif: str | None
    empresa_email: str | None
    empresa_setor: str | None
    empresa_tipo_entidade: str

    utilizadores: list[ExportacaoUtilizadorSchema]
    estado_controlos: list[dict]      # snapshot de controlos_empresa simplificado
    historico_maturidade: list[dict]  # snapshots históricos


# ---------------------------------------------------------------------------
# Histórico de exportações (consulta AuditLog)
# ---------------------------------------------------------------------------


class HistoricoExportacaoItemSchema(BaseModel):
    """Uma entrada do histórico de exportações de relatórios."""

    id: uuid.UUID
    tipo_relatorio: str      # 'conformidade', 'gap', 'historico', 'executivo', etc.
    utilizador_nome: str | None
    data: datetime


class HistoricoExportacoesSchema(BaseModel):
    """Lista de exportações registadas no AuditLog (paginada)."""

    total_registos: int                              # total sem paginação
    limite: int
    offset: int
    exportacoes: list[HistoricoExportacaoItemSchema]
