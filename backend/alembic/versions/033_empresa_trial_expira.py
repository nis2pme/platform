"""Fim do período de avaliação por empresa (SaaS).

A data em que um trial termina vivia em dois sítios que não se falavam: a
borda de registo (que suspende a conta nesse dia) e os direitos no gateway (que
contavam o prazo a partir do instante em que eram escritos). Um plano reposto
mais tarde dava aos módulos um prazo diferente do da suspensão, e o core não
sabia nenhum dos dois — não tinha como avisar quem usa a aplicação.

A data passa a ser a da borda: vem no registo, fica aqui e segue para o gateway.
O prolongamento feito pelo operador escreve os três sítios. Vazia em on-prem e
nos planos pagos.

Revision ID: 033_empresa_trial_expira
Revises: 032_pii_em_claro
Create Date: 2026-09-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "033_empresa_trial_expira"
down_revision: Union[str, None] = "032_pii_em_claro"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tem_coluna(inspector, tabela: str, coluna: str) -> bool:
    return coluna in {c["name"] for c in inspector.get_columns(tabela)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _tem_coluna(inspector, "empresas", "trial_expira_em"):
        # Sem fuso (UTC), como os outros instantes de `empresas`.
        op.add_column("empresas", sa.Column("trial_expira_em", sa.DateTime(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _tem_coluna(inspector, "empresas", "trial_expira_em"):
        op.drop_column("empresas", "trial_expira_em")
