"""Último passo TOTP aceite por utilizador.

O código de 6 dígitos vale 30 segundos, com um passo de tolerância para cada lado
(relógios de telemóvel desacertados) — perto de 90 segundos no total. Sem memória
de qual já foi usado, quem o visse por cima do ombro, ou o apanhasse num registo,
podia entrar com ele outra vez dentro dessa janela. Guarda-se o último passo
aceite e só se aceita um posterior.

Revision ID: 034_totp_ultimo_passo
Revises: 033_empresa_trial_expira
Create Date: 2026-09-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "034_totp_ultimo_passo"
down_revision: Union[str, None] = "033_empresa_trial_expira"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tem_coluna(inspector, tabela: str, coluna: str) -> bool:
    return coluna in {c["name"] for c in inspector.get_columns(tabela)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _tem_coluna(inspector, "utilizadores", "totp_ultimo_passo"):
        op.add_column(
            "utilizadores", sa.Column("totp_ultimo_passo", sa.BigInteger(), nullable=True)
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _tem_coluna(inspector, "utilizadores", "totp_ultimo_passo"):
        op.drop_column("utilizadores", "totp_ultimo_passo")
