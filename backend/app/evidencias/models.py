"""
Modelos SQLModel do módulo de evidências.
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Optional

import sqlalchemy as sa
from sqlalchemy import Column
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.auth.models import Utilizador


class TipoEvidencia(str, Enum):
    TEXTO = "texto"
    FICHEIRO = "ficheiro"
    AMBOS = "ambos"   # tem texto E ficheiro em simultâneo


class Evidencia(SQLModel, table=True):
    """
    Prova de implementação de um controlo.
    Pode ser texto livre ou ficheiro (PDF, imagem, documento, etc.).
    Soft delete — evidências não são eliminadas fisicamente.
    """

    __tablename__ = "evidencias"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)

    # Ligação ao controlo implementado (V2)
    controlo_empresa_v2_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.UUID(as_uuid=True), nullable=True, index=True),
    )
    # Desnormalizado para queries rápidas sem join
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    titulo: Optional[str] = Field(default=None, max_length=255)  # título opcional da evidência
    tipo: TipoEvidencia = Field()

    # Conteúdo textual (tipo=texto ou ambos)
    conteudo_texto: Optional[str] = Field(default=None)

    # Ficheiro (tipo=ficheiro)
    ficheiro_path: Optional[str] = Field(default=None, max_length=500)
    ficheiro_nome: Optional[str] = Field(default=None, max_length=500)  # cifrado em repouso
    ficheiro_tipo: Optional[str] = Field(default=None, max_length=100)  # MIME type
    ficheiro_tamanho: Optional[int] = Field(default=None)               # bytes

    # Cifra Fernet — True se o conteúdo do ficheiro foi cifrado no upload
    ficheiro_cifrado: bool = Field(default=False)

    # Cifra Fernet — True se conteudo_texto foi cifrado em repouso
    conteudo_texto_cifrado: bool = Field(default=False)

    # Quem fez upload
    uploaded_by_id: uuid.UUID = Field(foreign_key="utilizadores.id", index=True)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deleted_at: Optional[datetime] = Field(default=None)  # soft delete

    # Relationships (sem FK gerida pelo ORM — join feito manualmente no service)
    # controlo_empresa gerido via UUID raw — ver evidencias/service.py
