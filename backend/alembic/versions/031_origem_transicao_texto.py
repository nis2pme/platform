"""A origem das transições de estado passa mesmo a texto.

A 027 devia convertê-la, mas decidia pelo tipo refletido com
`isinstance(tipo, sa.String)` — e o enumerado do Postgres é uma subclasse de
`String`, por isso a conversão nunca corria. Nas instalações cujas tabelas
nasceram dos modelos antigos a coluna ficou um enumerado com os NOMES
(`UTILIZADOR`), enquanto o código escreve os VALORES (`utilizador`): mudar o
estado de um controlo rebentava com um erro interno.

Aqui decide-se pelo catálogo do Postgres, que não se engana, e os nomes antigos
passam a valores (são os mesmos em minúsculas). Corre duas vezes sem mudar nada.

Revision ID: 031_origem_transicao_texto
Revises: 030_empresa_plano
Create Date: 2026-09-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "031_origem_transicao_texto"
down_revision: Union[str, None] = "030_empresa_plano"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    tipo = bind.execute(sa.text(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'controlo_estado_historico' AND column_name = 'origem'"
    )).scalar()
    if tipo is None:
        return
    if tipo == "USER-DEFINED":
        op.execute(
            "ALTER TABLE controlo_estado_historico "
            "ALTER COLUMN origem TYPE VARCHAR(20) USING lower(origem::text)"
        )
        op.execute("DROP TYPE IF EXISTS origemtransicao")
    else:
        # Já é texto, mas pode ter herdado os nomes de uma conversão anterior.
        op.execute(
            "UPDATE controlo_estado_historico SET origem = lower(origem) "
            "WHERE origem <> lower(origem)"
        )


def downgrade() -> None:
    # Voltar a um enumerado com os nomes era voltar ao defeito.
    pass
