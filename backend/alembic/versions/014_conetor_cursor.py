"""Conetores: cursor de consumo de eventos por empresa.

O sidecar premium materializa eventos (drift/contradição/aviso) com ids
sequenciais; o core consome-os num tick e produz notificações, evidência
automática e auditoria. Esta tabela lembra, por empresa, o último evento
consumido e a última verificação vista — sem ela o tick repetiria ou
perderia eventos entre reinícios.

Aditiva (regra permanente de compatibilidade).

Revision ID: 014_conetor_cursor
Revises: 013_pesquisa_unaccent
Create Date: 2026-07-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "014_conetor_cursor"
down_revision: Union[str, None] = "013_pesquisa_unaccent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conetor_cursor",
        sa.Column(
            "empresa_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("empresas.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "ultimo_evento_id", sa.BigInteger(), nullable=False, server_default="0"
        ),
        # RFC3339 vindo do sidecar; comparado por igualdade (nunca interpretado).
        sa.Column("ultima_verificacao_vista", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("conetor_cursor")
