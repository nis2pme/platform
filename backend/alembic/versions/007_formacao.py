"""Módulo de Formação: tabelas formacao_acoes + formacao_participantes

Cria as tabelas do módulo de formação (core). Instalações novas já recebem as
tabelas via create_all (001); esta migração cobre bases existentes.

Idempotente: cria só as tabelas que ainda não existam (checkfirst).

Revision ID: 007_formacao
Revises: 006_tarefas
Create Date: 2026-07-14
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

revision: str = "007_formacao"
down_revision: Union[str, None] = "006_tarefas"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABELAS = ("formacao_acoes", "formacao_participantes")


def upgrade() -> None:
    bind = op.get_bind()
    existentes = set(inspect(bind).get_table_names())

    # Regista os modelos nos metadados SQLModel e cria só o que falta.
    import app.formacao.models  # noqa: F401
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
    import app.formacao.models  # noqa: F401
    from sqlmodel import SQLModel

    # Ordem inversa (participantes referenciam ações).
    for t in reversed(_TABELAS):
        if t in existentes and t in SQLModel.metadata.tables:
            SQLModel.metadata.tables[t].drop(bind, checkfirst=True)
