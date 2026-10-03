"""Dossiês de auditoria: tabela dossies_gerados (registo de exportações)

Cada dossiê exportado deixa um registo local (id, sha256, âmbito, quem/quando
e a identidade de resposta cifrada) — sem conteúdo. Instalações novas já
recebem a tabela via create_all (001); esta migração cobre bases existentes.

Idempotente: cria só a tabela se ainda não existir (checkfirst).

Revision ID: 009_dossies_gerados
Revises: 008_email_envios
Create Date: 2026-07-17
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

revision: str = "009_dossies_gerados"
down_revision: Union[str, None] = "008_email_envios"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABELAS = ("dossies_gerados",)


def upgrade() -> None:
    bind = op.get_bind()
    existentes = set(inspect(bind).get_table_names())

    # Regista os modelos nos metadados SQLModel e cria só o que falta.
    import app.dossie.models  # noqa: F401
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
    for t in _TABELAS:
        if t in existentes:
            op.drop_table(t)
