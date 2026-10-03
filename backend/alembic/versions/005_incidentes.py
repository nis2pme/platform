"""Módulo de Incidentes: tabelas incidentes + incidente_eventos

Cria as tabelas do módulo de gestão de incidentes (core). Instalações novas já
recebem as tabelas via create_all (001); esta migração cobre bases existentes.

Idempotente: cria só as tabelas que ainda não existam (checkfirst).

Revision ID: 005_incidentes
Revises: 004_evidencia_conteudo_hash
Create Date: 2026-07-14
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

revision: str = "005_incidentes"
down_revision: Union[str, None] = "004_evidencia_conteudo_hash"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABELAS = ("incidentes", "incidente_eventos")


def upgrade() -> None:
    bind = op.get_bind()
    existentes = set(inspect(bind).get_table_names())

    # Regista os modelos nos metadados SQLModel e cria só o que falta.
    import app.incidentes.models  # noqa: F401
    from sqlmodel import SQLModel

    tabelas = [
        SQLModel.metadata.tables[t]
        for t in _TABELAS
        if t not in existentes and t in SQLModel.metadata.tables
    ]
    if tabelas:
        SQLModel.metadata.create_all(bind, tables=tabelas, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    existentes = set(inspect(bind).get_table_names())
    import app.incidentes.models  # noqa: F401
    from sqlmodel import SQLModel

    # Ordem inversa (eventos referenciam incidentes).
    for t in reversed(_TABELAS):
        if t in existentes and t in SQLModel.metadata.tables:
            SQLModel.metadata.tables[t].drop(bind, checkfirst=True)
