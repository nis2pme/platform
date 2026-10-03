"""Como é que cada desvio de política foi decidido.

A tabela esparsa diz o QUE a empresa mudou; esta coluna diz por que caminho. A
diferença interessa a quem audita: uma organização que carregou num interruptor
nomeado escolheu de entre as opções que a plataforma lhe ofereceu, e uma que
editou a matriz célula a célula tomou uma decisão própria sobre a sua segregação
de funções. As duas são legítimas, mas não são a mesma afirmação, e o documento
de funções e responsabilidades tem de as distinguir.

Aditiva, com defeito. As linhas que já existem vieram todas de interruptores —
era o único caminho — e é isso que o defeito diz.

Revision ID: 017_politica_origem
Revises: 016_audit_logs_indices
Create Date: 2026-08-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "017_politica_origem"
down_revision: Union[str, None] = "016_audit_logs_indices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "politica_capacidades",
        sa.Column(
            "origem",
            sa.String(20),
            nullable=False,
            server_default="interruptor",
        ),
    )


def downgrade() -> None:
    op.drop_column("politica_capacidades", "origem")
