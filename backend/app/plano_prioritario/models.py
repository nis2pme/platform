"""
Modelos SQLModel para o Plano de Ações Prioritárias.

QuestionarioResposta: guarda as respostas ao questionário de 10 perguntas.
PlanoItem: cada linha do roadmap gerado (controlo ordenado por prioridade).
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel


class QuestionarioResposta(SQLModel, table=True):
    """
    Respostas ao questionário de diagnóstico de 10 perguntas.
    Uma resposta por empresa — ao refazer, sobrescreve.
    """

    __tablename__ = "questionario_respostas"  # type: ignore[assignment]

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", unique=True, index=True)

    # JSON: {"q1": "A", "q2": "D", ..., "q10": "NS"}
    # Valores: A=1, B=2, C=3, D=4, E=5, NS=null
    respostas: dict = Field(sa_column=Column(JSON, nullable=False))

    respondido_por_id: uuid.UUID = Field(foreign_key="utilizadores.id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PlanoItem(SQLModel, table=True):
    """
    Linha individual do roadmap de implementação gerado pelo algoritmo.
    Cada registo mapeia um controlo à sua posição na fila de prioridade.
    """

    __tablename__ = "plano_itens"  # type: ignore[assignment]

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    # Referencia ao controlo V2
    control_id: uuid.UUID = Field(foreign_key="controls.id", index=True)

    # Posição global na fila (1 = mais prioritário)
    posicao: int = Field(ge=1)

    # Tier macro do roadmap a que pertence (1=Básico, 2=Substancial, 3=Elevado)
    nivel_conformidade: int = Field(ge=1, le=4)

    # Gap calculado: nivel_conformidade - nivel_maturidade_atual
    gap: int = Field(default=0)

    # Se este controlo foi mapeado pelo questionário
    mapeado_questionario: bool = Field(default=False)

    # Domínio para desempate (ordem cronológica)
    dominio_codigo: str = Field(max_length=10)
    dominio_ordem: int = Field(default=0)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
