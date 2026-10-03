"""Evidências: coluna conteudo_hash (deduplicação de evidências repetidas)

Acrescenta `evidencias.conteudo_hash` (SHA-256 hex do conteúdo em claro) + índice.
Permite detetar quando a mesma evidência é anexada outra vez sem alterações e evitar
processamento e armazenamento desnecessários. Instalações novas já recebem a coluna
via create_all (001); esta migração cobre bases existentes.

Idempotente: só adiciona a coluna/índice se ainda não existirem.

Revision ID: 004_evidencia_conteudo_hash
Revises: 003_qnrcs_prsd2_basico
Create Date: 2026-07-13
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "004_evidencia_conteudo_hash"
down_revision = "003_qnrcs_prsd2_basico"
branch_labels = None
depends_on = None

_TABELA = "evidencias"
_COLUNA = "conteudo_hash"
_INDICE = "ix_evidencias_conteudo_hash"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    colunas = {c["name"] for c in inspector.get_columns(_TABELA)}
    if _COLUNA not in colunas:
        op.add_column(_TABELA, sa.Column(_COLUNA, sa.String(length=64), nullable=True))
    indices = {i["name"] for i in inspector.get_indexes(_TABELA)}
    if _INDICE not in indices:
        op.create_index(_INDICE, _TABELA, [_COLUNA])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    indices = {i["name"] for i in inspector.get_indexes(_TABELA)}
    if _INDICE in indices:
        op.drop_index(_INDICE, table_name=_TABELA)
    colunas = {c["name"] for c in inspector.get_columns(_TABELA)}
    if _COLUNA in colunas:
        op.drop_column(_TABELA, _COLUNA)
