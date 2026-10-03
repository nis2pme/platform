"""Plano comercial e estado do provisionamento por empresa.

No registo SaaS o core pede ao gateway (escritor único dos entitlements) o
plano do tenant, em best-effort: um falhanço não pode quebrar o registo, mas
até aqui também não deixava rasto — o tenant ficava sem IA em silêncio e a
"reconciliação futura" prometida no código não existia. Passa a haver duas
colunas: o plano pedido e o instante em que o gateway o confirmou. Plano
preenchido sem confirmação = por reconciliar; o tick volta a pedir.

Em on-prem as duas colunas ficam vazias (não há gateway a provisionar).

Revision ID: 030_empresa_plano
Revises: 029_dossie_chave_empresa
Create Date: 2026-09-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "030_empresa_plano"
down_revision: Union[str, None] = "029_dossie_chave_empresa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tem_coluna(inspector, tabela: str, coluna: str) -> bool:
    return coluna in {c["name"] for c in inspector.get_columns(tabela)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _tem_coluna(inspector, "empresas", "plano"):
        op.add_column("empresas", sa.Column("plano", sa.String(length=32), nullable=True))
    if not _tem_coluna(inspector, "empresas", "plano_provisionado_em"):
        # Sem fuso, como os outros instantes de `empresas` que o modelo declara.
        op.add_column(
            "empresas", sa.Column("plano_provisionado_em", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _tem_coluna(inspector, "empresas", "plano_provisionado_em"):
        op.drop_column("empresas", "plano_provisionado_em")
    if _tem_coluna(inspector, "empresas", "plano"):
        op.drop_column("empresas", "plano")
