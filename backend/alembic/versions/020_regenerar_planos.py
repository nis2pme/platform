"""A fila do plano volta a ser construída pelo algoritmo desta versão.

A ordem dos controlos é decidida uma vez, quando o plano é gerado, e fica
gravada em cada linha. Uma correção à ordenação não chega, por isso, a quem já
tem plano: continua a ver a fila que foi calculada pela versão anterior.

O que se faz aqui é esvaziar a fila. A aplicação reconstrói a de cada empresa na
primeira leitura — é o comportamento que já existe para quem nunca teve plano —
e reconstrói-a com o algoritmo que estiver a correr nesse momento.

A construção fica deliberadamente de fora desta migração. Uma migração é
imutável e o algoritmo não é: chamá-lo aqui significaria, daqui a umas versões,
executar regras diferentes daquelas para que isto foi escrito. Reescrever a
ordenação em SQL seria pior — uma segunda cópia das regras, congelada, a
divergir da primeira alteração em diante. Apagar é a única metade que não
envelhece.

Nada aqui é informação original. Cada linha guarda posição, nível exigido, gap e
a marca de ter sido apontado pelo diagnóstico — tudo recalculado a partir dos
controlos e das respostas ao questionário, que vivem noutra tabela e não se
tocam. O próprio gerador já apaga a fila anterior sempre que regenera.

Estritamente idempotente. Sem downgrade, por regra permanente.

Revision ID: 020_regenerar_planos
Revises: 019_plano_item_unico
Create Date: 2026-08-04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "020_regenerar_planos"
down_revision: Union[str, None] = "019_plano_item_unico"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "plano_itens" not in inspector.get_table_names():
        # Base sem a tabela: não há fila anterior para esvaziar.
        return

    bind.execute(sa.text("DELETE FROM plano_itens"))


def downgrade() -> None:
    # A fila reconstrói-se sozinha — não há estado anterior a repor.
    pass
