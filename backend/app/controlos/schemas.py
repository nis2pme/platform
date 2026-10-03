"""
Schemas Pydantic para o módulo de controlos.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.controlos.models import DecisaoAuditor
from app.shared.enums import EstadoControlo


# ---------------------------------------------------------------------------
# Domínio
# ---------------------------------------------------------------------------

class DominioSchema(BaseModel):
    id: uuid.UUID
    codigo: str
    nome: str
    descricao: str
    ordem: int
    score: int = 0  # calculado no momento da resposta

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Controlo (listagem)
# ---------------------------------------------------------------------------

class ControloListaSchema(BaseModel):
    """Versão reduzida para listagem — sem guias e exemplos."""

    id: uuid.UUID
    codigo: str
    titulo: str
    descricao_simples: str
    critico: bool
    dominio_id: uuid.UUID
    dominio_codigo: str
    dominio_nome: str = ""
    # Nível intermédio da hierarquia do referencial (domínio > subdomínio >
    # controlo). O detalhe já o expunha; a listagem precisa dele para conseguir
    # apresentar os controlos agrupados sem pedir o detalhe de cada um.
    subdomain_id: uuid.UUID | None = None
    subdomain_codigo: str | None = None
    subdomain_nome: str | None = None
    ordem: int

    # Estado corrente da empresa (injected em service)
    controlo_empresa_id: uuid.UUID | None = None
    estado: EstadoControlo | None = None
    nivel_maturidade_atual: int = 0
    nivel_minimo: int = 2          # nível mínimo exigido para esta empresa
    # Nível em que o controlo começa (o mais baixo dos seus sub-requisitos).
    # Não depende da empresa: é o que o filtro por nível usa, para que "Substancial"
    # mostre os controlos que entram nesse nível e não os que a empresa tem de
    # levar ao nível 2.
    nivel_inicial: int | None = None
    em_conformidade: bool = False
    score_conformidade: float = 0.0     # 0.0–1.0 fórmula contínua ponderada
    obrigatorio_perfil: bool = True
    progresso_conformidade: float = 0.0  # 0-100 sobre checks exigidos ate ao nivel minimo
    implementador_id: uuid.UUID | None = None
    implementador_nome: str | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Check (nível de maturidade)
# ---------------------------------------------------------------------------

class ControloNivelCheckSchema(BaseModel):
    id: uuid.UUID
    nivel: int
    ordem: int
    descricao: str
    obrigatorio: bool
    concluido: bool = False         # estado para esta empresa
    concluido_at: datetime | None = None
    concluido_por_nome: str | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Relatório de auditoria
# ---------------------------------------------------------------------------

class RelatorioAuditoriaSchema(BaseModel):
    id: uuid.UUID
    # Vazio nos relatórios externos (o auditor não é utilizador da app).
    auditor_id: uuid.UUID | None = None
    auditor_nome: str
    decisao: DecisaoAuditor
    texto: str
    created_at: datetime
    # Relatório importado de um parecer assinado de auditor externo.
    externo: bool = False
    estado_externo: str | None = None

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def decifrar_campos_pii(cls, data):
        """Decifra os campos cifrados em repouso antes da validação.

        Copia os atributos para um dict em vez de mutar a entidade ORM recebida:
        mutar o objeto persistente faria o commit automático do get_session regravar
        os campos DECIFRADOS (em claro) na base — corrompia a cifra em repouso.
        """
        from app.shared.pii import decifrar_pii
        if not isinstance(data, dict):
            data = {k: getattr(data, k) for k in cls.model_fields if hasattr(data, k)}
        for campo in ("auditor_nome", "texto"):
            if data.get(campo) is not None:
                data[campo] = decifrar_pii(data[campo])
        return data


# ---------------------------------------------------------------------------
# Controlo (detalhe completo)
# ---------------------------------------------------------------------------

class ControloDetalheSchema(BaseModel):
    """Detalhe completo de um controlo, com guias, exemplos e checks."""

    id: uuid.UUID
    codigo: str
    titulo: str
    descricao_simples: str
    critico: bool
    dominio_id: uuid.UUID
    dominio_codigo: str
    dominio_nome: str
    ordem: int
    subdomain_id: uuid.UUID | None = None
    subdomain_codigo: str | None = None
    subdomain_nome: str | None = None

    # Conteúdo de apoio por nível de maturidade.
    # Retrocompatível com chaves antigas e novas:
    # {"1": {"descricao": str, "indicadores": list[str], "evidencias": list[str],
    #         "dica_pratica": str, "esforco_estimado": str,
    #         "guia_implementacao": list[str], "exemplos_evidencias": list[str]}, ...}
    niveis_conteudo: dict | None = None
    mapeamento: dict | None = None

    nivel_minimo_base: int | None = None
    nivel_minimo_importante: int | None = None
    nivel_minimo_essencial: int | None = None

    # Estado corrente da empresa
    controlo_empresa_id: uuid.UUID | None = None
    estado: EstadoControlo | None = None
    nivel_maturidade_atual: int = 0
    nivel_minimo: int = 2
    em_conformidade: bool = False
    score_conformidade: float = 0.0     # 0.0–1.0 fórmula contínua ponderada
    implementador_id: uuid.UUID | None = None
    implementador_nome: str | None = None
    aprovado_por_id: uuid.UUID | None = None
    aprovado_por_nome: str | None = None
    data_aprovacao: datetime | None = None

    # Estado "não aplicável" (scoping): justificação + autoria, visíveis a todos
    # (incluindo o auditor — a exclusão é contestável).
    na_justificacao: str | None = None
    na_definido_em: datetime | None = None
    na_definido_por_nome: str | None = None

    # Relatório de auditoria mais recente (informação rápida no detalhe)
    ultimo_relatorio_auditoria: RelatorioAuditoriaSchema | None = None

    # Checks organizados por nível
    checks_por_nivel: dict[int, list[ControloNivelCheckSchema]] = {}

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Alteração de estado
# ---------------------------------------------------------------------------

class AlterarEstadoSchema(BaseModel):
    """Pedido de alteração de estado de um controlo."""

    estado: EstadoControlo

    @field_validator("estado")
    @classmethod
    def estado_valido(cls, v: EstadoControlo) -> EstadoControlo:
        # Estados que podem ser definidos directamente via este endpoint
        # (aprovado/nao_aprovado têm endpoints dedicados)
        permitidos = {
            EstadoControlo.EM_PROGRESSO,
            EstadoControlo.IMPLEMENTADO,
            EstadoControlo.NAO_INICIADO,
        }
        if v not in permitidos:
            raise ValueError(
                "Use os endpoints /aprovar ou /reprovar para aprovação."
            )
        return v


class MarcarNaoAplicavelSchema(BaseModel):
    """Pedido de exclusão de âmbito — a justificação é obrigatória."""

    justificacao: str = Field(min_length=10, max_length=5000)


# ---------------------------------------------------------------------------
# Delegação
# ---------------------------------------------------------------------------

class DelegarControloSchema(BaseModel):
    """Atribuir controlo a um implementador (admin only)."""

    implementador_id: uuid.UUID | None = None  # None = remover delegação


class DelegarControlosLoteSchema(BaseModel):
    """Atribuir/remover delegações de múltiplos controlos num só pedido."""

    implementador_id: uuid.UUID
    adicionar_ids: list[uuid.UUID] = Field(default_factory=list)
    remover_ids: list[uuid.UUID] = Field(default_factory=list)


class ResultadoDelegacaoLoteSchema(BaseModel):
    """Resultado de uma operação batch de delegação."""

    alterados: int


# ---------------------------------------------------------------------------
# Aprovação / reprovação
# ---------------------------------------------------------------------------

class AprovarControloSchema(BaseModel):
    """Aprovar controlo com relatório obrigatório."""
    texto_relatorio: str


class ReprovarControloSchema(BaseModel):
    """Reprovar controlo com relatório obrigatório."""
    texto_relatorio: str
    nota: str | None = None  # mantido por retrocompatibilidade


# ---------------------------------------------------------------------------
# Dashboard / scores
# ---------------------------------------------------------------------------

class ScoreDominioSchema(BaseModel):
    dominio_id: uuid.UUID
    codigo: str
    nome: str
    score: int
    total_controlos: int
    controlos_conformidade: int
    percentagem_conformidade: float


class ResumoControlosSchema(BaseModel):
    total: int
    nao_iniciados: int
    em_progresso: int
    implementados: int
    aprovados: int
    # Exclusões de âmbito SEMPRE visíveis no dashboard (anti-abuso: marcar
    # controlos como não aplicáveis nunca passa despercebido).
    nao_aplicaveis: int = 0


class DashboardScoreSchema(BaseModel):
    empresa_id: uuid.UUID
    empresa_nome: str
    empresa_created_at: datetime
    score_global: int
    percentagem_conformidade: float
    nivel_minimo_exigido: int
    tipo_entidade: str
    nivel_qnrcs: str | None = None
    dominios: list[ScoreDominioSchema]
    resumo_controlos: ResumoControlosSchema
    controlos_criticos_em_falta: list[ControloListaSchema]
    data_calculo: datetime
    # Framework metadata (V2) — None quando empresa usa camada V1 legada
    framework_id: uuid.UUID | None = None
    framework_nome: str | None = None
    maturity_scale_max: int = 5
    maturity_radar_max: int | None = None
    display_mode: str = "maturity"
    features: dict | None = None
    maturity_level_names: dict | None = None
