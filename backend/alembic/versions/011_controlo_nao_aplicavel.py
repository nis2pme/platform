"""Estado "não aplicável" nos controlos (scoping de exclusão).

Um controlo fora do âmbito da organização pode ser marcado como não
aplicável — com justificação obrigatória, autor e data. Sai das contas de
conformidade mas continua visível (e contestável por um auditor).

  - enum `estadocontrolo`: novo valor NAO_APLICAVEL;
  - controlos_empresa_v2: colunas na_justificacao (texto cifrado em repouso),
    na_definido_por (FK utilizadores) e na_definido_em.

Idempotente: instalações novas recebem o enum/colunas via create_all (001);
aqui só se acrescenta o que falta a bases existentes.

Revision ID: 011_controlo_nao_aplicavel
Revises: 010_pareceres_auditor
Create Date: 2026-07-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "011_controlo_nao_aplicavel"
down_revision: Union[str, None] = "010_pareceres_auditor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # Valor novo no tipo nativo (o SQLAlchemy guarda os NOMES do enum).
    # IF NOT EXISTS torna a operação idempotente (PG 12+).
    op.execute("ALTER TYPE estadocontrolo ADD VALUE IF NOT EXISTS 'NAO_APLICAVEL'")

    colunas = {c["name"] for c in inspector.get_columns("controlos_empresa_v2")}
    if "na_justificacao" not in colunas:
        op.add_column(
            "controlos_empresa_v2", sa.Column("na_justificacao", sa.Text(), nullable=True)
        )
    if "na_definido_por" not in colunas:
        op.add_column(
            "controlos_empresa_v2",
            sa.Column(
                "na_definido_por",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("utilizadores.id"),
                nullable=True,
            ),
        )
    if "na_definido_em" not in colunas:
        op.add_column(
            "controlos_empresa_v2",
            sa.Column("na_definido_em", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    # Aditiva por regra permanente — sem downgrade (o valor de enum não se
    # remove em Postgres sem recriar o tipo).
    pass
