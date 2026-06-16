"""
Modelos SQLModel do módulo de notificações.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


class Notificacao(SQLModel, table=True):
    """
    Notificação in-app para um utilizador específico.
    Desaparece da lista quando o utilizador visita o controlo associado.
    """

    __tablename__ = "notificacoes"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)
    utilizador_id: uuid.UUID = Field(
        foreign_key="utilizadores.id", index=True
    )  # destinatário

    tipo: str = Field(max_length=100)    # ex: "controlo_decisao_auditoria"
    titulo: str = Field(max_length=255)
    mensagem: str = Field(sa_column=Column(Text))

    # Link para o controlo — usado para marcar como lida quando visitado
    controlo_empresa_id: Optional[uuid.UUID] = Field(
        default=None,
        foreign_key="controlos_empresa_v2.id",
        nullable=True,
        index=True,
    )

    lida: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
