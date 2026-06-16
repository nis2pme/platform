"""
Schemas Pydantic para notificações.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class NotificacaoSchema(BaseModel):
    id: uuid.UUID
    tipo: str
    titulo: str
    mensagem: str
    controlo_empresa_id: uuid.UUID | None = None
    lida: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ListaNotificacoesSchema(BaseModel):
    total: int
    nao_lidas: int
    notificacoes: list[NotificacaoSchema] = Field(default_factory=list)
    controlos_com_notificacoes: list[uuid.UUID] = Field(default_factory=list)


class ResultadoNotificacoesMarcadasSchema(BaseModel):
    marcadas: int
