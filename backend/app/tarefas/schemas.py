"""
Schemas Pydantic do módulo de Tarefas recorrentes.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

from app.shared.validacao import SemNulos
from app.tarefas.models import Periodicidade, ResultadoTeste, TipoTarefa


# ── Entrada ──────────────────────────────────────────────────────────────────

class TarefaCriarIn(BaseModel):
    # A partir do catálogo (preenche o resto) ou personalizada (titulo obrigatório).
    chave_catalogo: str | None = None
    titulo: str = Field("", max_length=255)
    descricao: str = ""
    categoria: str = Field("outro", max_length=80)
    tipo: TipoTarefa = TipoTarefa.RECORRENTE
    controlos: list[str] = []
    # None = não escolhida: usa a do catálogo (se houver) ou anual. Um valor
    # explícito do utilizador prevalece SEMPRE sobre o default do catálogo.
    periodicidade: Periodicidade | None = None
    periodicidade_dias: int | None = Field(None, ge=1, le=3650)
    responsavel_id: str = ""
    primeiro_prazo: date | None = None   # default = hoje + 1 período (no service)


class TarefaAtualizarIn(SemNulos):
    nao_anulaveis = frozenset(
        {"titulo", "descricao", "categoria", "controlos", "periodicidade", "proximo_prazo", "ativa"}
    )

    titulo: str | None = Field(None, max_length=255)
    descricao: str | None = None
    categoria: str | None = Field(None, max_length=80)
    controlos: list[str] | None = None
    periodicidade: Periodicidade | None = None
    periodicidade_dias: int | None = Field(None, ge=1, le=3650)
    responsavel_id: str | None = None
    proximo_prazo: date | None = None
    ativa: bool | None = None


class ConclusaoIn(BaseModel):
    concluida_em: date | None = None     # default = hoje (no service)
    notas: str = ""
    resultado: ResultadoTeste | None = None    # só testes/exercícios
    licoes_aprendidas: str | None = None       # só testes/exercícios


# ── Saída ────────────────────────────────────────────────────────────────────

class ConclusaoSchema(BaseModel):
    id: uuid.UUID
    concluida_em: date
    notas: str
    resultado: ResultadoTeste | None = None
    licoes_aprendidas: str | None = None
    autor_id: uuid.UUID | None = None
    autor_nome: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TarefaSchema(BaseModel):
    id: uuid.UUID
    chave_catalogo: str | None
    titulo: str
    descricao: str
    categoria: str
    tipo: TipoTarefa
    controlos: list[str] = []
    periodicidade: Periodicidade
    periodicidade_dias: int | None
    responsavel_id: uuid.UUID | None
    responsavel_nome: str | None = None
    proximo_prazo: date
    ultima_conclusao_at: date | None
    ativa: bool
    # Derivados (calculados no service, não persistidos):
    dias_restantes: int | None = None   # até ao próximo prazo (negativo = em atraso)
    em_atraso: bool = False
    total_conclusoes: int = 0

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def controlos_de_texto(cls, data):
        """
        A tabela guarda os controlos numa só coluna de texto separada por vírgulas;
        a API expõe-nos como lista. A conversão tem de acontecer ANTES da validação:
        feita depois, o `model_validate` já rejeitou a string e nada do que venha a
        seguir chega a correr.
        """
        if not isinstance(data, dict):
            data = {k: getattr(data, k) for k in cls.model_fields if hasattr(data, k)}
        bruto = data.get("controlos")
        if isinstance(bruto, str):
            data["controlos"] = [c for c in (p.strip() for p in bruto.split(",")) if c]
        return data


class TarefaDetalheSchema(TarefaSchema):
    conclusoes: list[ConclusaoSchema] = []


class ListaTarefasSchema(BaseModel):
    total: int
    tarefas: list[TarefaSchema]


class PainelTarefasSchema(BaseModel):
    total: int              # tarefas ativas
    em_dia: int             # com prazo no futuro
    a_vencer: int           # prazo dentro da janela de aviso
    em_atraso: int          # prazo ultrapassado


class ItemCatalogoSchema(BaseModel):
    """Uma obrigação pré-definida que o utilizador pode adicionar num clique."""
    chave: str
    titulo: str
    descricao: str
    categoria: str
    tipo: TipoTarefa
    controlos: list[str] = []
    periodicidade: Periodicidade
