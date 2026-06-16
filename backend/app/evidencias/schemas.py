"""
Schemas Pydantic para o módulo de evidências.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.evidencias.models import TipoEvidencia


# ---------------------------------------------------------------------------
# Evidência (resposta)
# ---------------------------------------------------------------------------

class EvidenciaSchema(BaseModel):
    id: uuid.UUID
    controlo_empresa_id: uuid.UUID
    empresa_id: uuid.UUID
    tipo: TipoEvidencia

    titulo: str | None = None        # título descritivo (opcional)
    conteudo_texto: str | None = None
    conteudo_resumo: str | None = None

    # Ficheiro — não expõe o path interno do servidor
    ficheiro_nome: str | None = None
    ficheiro_tipo: str | None = None
    ficheiro_tamanho: int | None = None

    uploaded_by_id: uuid.UUID
    uploaded_by_nome: str | None = None  # injectado em service
    created_at: datetime
    deleted_at: datetime | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------

class ListaEvidenciasSchema(BaseModel):
    total: int
    evidencias: list[EvidenciaSchema]


# ---------------------------------------------------------------------------
# Listagem global (enriquecida com dados do controlo/domínio)
# ---------------------------------------------------------------------------

class EvidenciaComControloSchema(EvidenciaSchema):
    """Evidencia com contexto do controlo e domínio — para listagem global."""
    controlo_codigo: str | None = None
    controlo_titulo: str | None = None
    controlo_estado: str | None = None   # estado de ControloEmpresa
    dominio_codigo: str | None = None
    dominio_nome: str | None = None


class ListaTodasEvidenciasSchema(BaseModel):
    total: int
    evidencias: list[EvidenciaComControloSchema]
