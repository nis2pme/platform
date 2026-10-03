"""Política de capacidades configurável por empresa.

Tabela ESPARSA: guarda apenas as células (módulo × classe × papel) em que a
empresa se afasta do defeito do código. Uma empresa que nunca mexeu na política
não tem linha nenhuma, e um módulo acrescentado numa versão futura herda o
defeito sem precisar de seed.

Aditiva (regra permanente de compatibilidade).

Revision ID: 015_politica_capacidades
Revises: 014_conetor_cursor
Create Date: 2026-08-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "015_politica_capacidades"
down_revision: Union[str, None] = "014_conetor_cursor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "politica_capacidades",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "empresa_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("empresas.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("modulo", sa.String(40), nullable=False),
        sa.Column("classe", sa.String(20), nullable=False),
        sa.Column("papel", sa.String(20), nullable=False),
        # "total" | "atribuido" | "nenhum" (nenhum = a capacidade é retirada).
        sa.Column("ambito", sa.String(20), nullable=False),
        sa.Column("alterado_por", sa.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "alterado_em",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Uma célula tem no máximo uma opinião por empresa.
        sa.UniqueConstraint(
            "empresa_id", "modulo", "classe", "papel", name="uq_politica_celula"
        ),
    )


def downgrade() -> None:
    op.drop_table("politica_capacidades")
