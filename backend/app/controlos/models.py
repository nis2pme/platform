"""
Modelos SQLModel partilhados do módulo de controlos.

  - DecisaoAuditor (enum)
  - RelatorioAuditoria (relatórios imutáveis do auditor)
  - HistoricoMaturidade (snapshots de evolução)
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


# ---------------------------------------------------------------------------
# HistoricoMaturidade — snapshots de evolução (para gráficos de tendência)
# ---------------------------------------------------------------------------

class DecisaoAuditor(str, Enum):
    APROVADO = "aprovado"
    NAO_APROVADO = "nao_aprovado"


class RelatorioAuditoria(SQLModel, table=True):
    """
    Relatório imutável criado pelo auditor na decisão de aprovação/rejeição.
    Cada transição para APROVADO ou NAO_APROVADO cria uma nova entrada.
    Suporta histórico completo por controlo.
    """

    __tablename__ = "relatorios_auditoria"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    controlo_empresa_v2_id: uuid.UUID | None = Field(
        default=None, foreign_key="controlos_empresa_v2.id", index=True, nullable=True
    )
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)
    auditor_id: uuid.UUID = Field(foreign_key="utilizadores.id", index=True)
    auditor_nome: str = Field(max_length=500, default="")    # cache para evitar join — cifrado em repouso
    decisao: DecisaoAuditor = Field()
    texto: str = Field(sa_column=Column(Text))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)


class HistoricoMaturidade(SQLModel, table=True):
    """
    Snapshot do nível de maturidade em determinado momento.
    Guardado automaticamente quando o nível de um controlo/domínio muda.
    dominio_id=None significa snapshot do score global.
    """

    __tablename__ = "historico_maturidade"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)
    dominio_id: Optional[uuid.UUID] = Field(
        default=None,
        foreign_key="domains.id",
        nullable=True,
        index=True,
    )

    nivel_maturidade: float = Field()   # pode ser decimal (média ponderada)
    percentagem_conformidade: float = Field()  # 0–100

    data_snapshot: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
