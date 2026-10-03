"""
Modelo do cursor de consumo dos eventos do conetor (tabela do CORE).

Os eventos vivem no sidecar (autoridade); o core só precisa de se lembrar de
onde ficou por empresa, para o tick nunca perder nem repetir um evento entre
reinícios. Sem dados de conteúdo — apenas posições.
"""
from __future__ import annotations

import uuid

from sqlmodel import Field, SQLModel


class ConetorCursor(SQLModel, table=True):
    __tablename__ = "conetor_cursor"

    empresa_id: uuid.UUID = Field(primary_key=True, foreign_key="empresas.id")
    # Último evento do sidecar já consumido (id sequencial de lá).
    ultimo_evento_id: int = Field(default=0)
    # Última verificação vista (RFC3339 do sidecar, comparada por igualdade) —
    # distingue execuções agendadas novas das já auditadas.
    ultima_verificacao_vista: str | None = Field(default=None, max_length=64)
