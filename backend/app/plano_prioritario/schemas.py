"""
Schemas Pydantic do módulo de Plano de Ações Prioritárias.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


# ─── Questionário ──────────────────────────────────────────────────────────────

class QuestionarioRespostasIn(BaseModel):
    """Payload para submeter/atualizar respostas ao questionário."""
    respostas: dict[str, str] = Field(
        ...,
        description="Mapa q1..q10 → A|B|C|D|E|NS",
        json_schema_extra={"example": {"q1": "B", "q2": "D", "q3": "NS"}},
    )


class QuestionarioRespostasOut(BaseModel):
    """Resposta devolvida ao frontend."""
    id: uuid.UUID
    empresa_id: uuid.UUID
    respostas: dict[str, str]
    respondido_por_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ─── Plano de Ações Prioritárias ──────────────────────────────────────────────

class PlanoItemOut(BaseModel):
    """Item mínimo do roadmap usado no dashboard."""

    posicao: int
    control_id: uuid.UUID
    codigo: str
    titulo: str
    descricao: str
    dominio_codigo: str
    dominio_nome: str
    mapeado_questionario: bool
    estado: str

    model_config = {"from_attributes": True}


class PlanoOut(BaseModel):
    """Resposta mínima do plano para o dashboard."""

    questionario_preenchido: bool
    itens: list[PlanoItemOut]
