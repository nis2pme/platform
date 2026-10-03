"""Linha temporal do incidente com código e parâmetros.

O que o próprio sistema escreve na linha temporal (incidente registado, estado
alterado, notificação enviada) ficava gravado como a frase já composta, na
língua da empresa, com a nota do utilizador colada no mesmo campo. Passa a
gravar-se o que aconteceu — um código e os parâmetros, estes cifrados — e a
frase compõe-se na leitura, na língua de quem lê; o texto fica só com a nota.

Nada se converte: uma linha anterior fica com o código a NULL e continua a
mostrar o texto que tinha. Uma instalação nova já recebe as colunas dos
modelos; cada passo verifica primeiro se há alguma coisa a fazer, e correr duas
vezes não muda nada.

Revision ID: 036_incidente_eventos_i18n
Revises: 035_incidentes_rjc
Create Date: 2026-10-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "036_incidente_eventos_i18n"
down_revision: Union[str, None] = "035_incidentes_rjc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABELA = "incidente_eventos"

# (nome, tipo). Ambas ficam a NULL nas linhas que já existem.
_COLUNAS = (
    ("codigo", sa.String(length=40)),
    ("params", sa.Text()),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABELA not in inspector.get_table_names():
        return
    existentes = {c["name"] for c in inspector.get_columns(_TABELA)}
    for nome, tipo in _COLUNAS:
        if nome not in existentes:
            op.add_column(_TABELA, sa.Column(nome, tipo, nullable=True))


def downgrade() -> None:
    # As colunas ficam: nas linhas que o sistema escreveu já só têm lá o que
    # aconteceu, e o código anterior ignora as colunas que não conhece.
    pass
