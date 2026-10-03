"""Um controlo aparece uma vez só na fila do plano de cada empresa.

O plano é gerado à primeira leitura de quem ainda não o tem. Dois pedidos em
paralelo — dois separadores abertos, o painel e a página do plano a carregar ao
mesmo tempo — veem os dois que o plano não existe e geram-no os dois. O
resultado é a fila com os controlos repetidos, sem erro nenhum a assinalá-lo.

A restrição fecha a corrida: a segunda escrita passa a falhar em vez de
duplicar.

Antes de a criar é preciso limpar o que já esteja duplicado. Fica a linha de
menor `posicao` de cada par (empresa, controlo) — é a que a empresa via na
lista; as outras são cópias da mesma decisão. Nada aqui é informação original:
a fila inteira é recalculada a partir das respostas ao questionário e do estado
dos controlos sempre que o plano é regenerado.

Estritamente idempotente. Em bases frescas o create_all da 001 já cria a tabela
com a restrição, porque ela vem do modelo. Aditiva — sem downgrade, por regra
permanente.

Revision ID: 019_plano_item_unico
Revises: 018_notificacoes_v2
Create Date: 2026-08-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "019_plano_item_unico"
down_revision: Union[str, None] = "018_notificacoes_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RESTRICAO = "uq_plano_empresa_control"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "plano_itens" not in inspector.get_table_names():
        # Base sem a tabela: o create_all da 001 cria-a já com a restrição.
        return

    existentes = {c["name"] for c in inspector.get_unique_constraints("plano_itens")}
    if _RESTRICAO in existentes:
        return

    # Só em Postgres: é o único motor onde existem instalações com histórico.
    # Uma base SQLite aqui é sempre uma base de teste acabada de criar.
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                """
                DELETE FROM plano_itens a
                      USING plano_itens b
                      WHERE a.empresa_id = b.empresa_id
                        AND a.control_id = b.control_id
                        AND (a.posicao > b.posicao
                             OR (a.posicao = b.posicao AND a.id > b.id))
                """
            )
        )

    op.create_unique_constraint(
        _RESTRICAO, "plano_itens", ["empresa_id", "control_id"]
    )


def downgrade() -> None:
    # Aditiva por regra permanente — sem downgrade.
    pass
