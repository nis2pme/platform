"""Módulo de Tarefas recorrentes: tabelas tarefas + tarefa_conclusoes

Cria as tabelas do módulo de tarefas recorrentes (core). Instalações novas já
recebem as tabelas via create_all (001); esta migração cobre bases existentes.

Idempotente: cria só as tabelas que ainda não existam (checkfirst).

Revision ID: 006_tarefas
Revises: 005_incidentes
Create Date: 2026-07-14
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect
from sqlalchemy.schema import CreateIndex, CreateTable

revision: str = "006_tarefas"
down_revision: Union[str, None] = "005_incidentes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABELAS = ("tarefas", "tarefa_conclusoes")


def upgrade() -> None:
    bind = op.get_bind()
    existentes = set(inspect(bind).get_table_names())

    # Regista os modelos nos metadados SQLModel e cria só o que falta.
    import app.tarefas.models  # noqa: F401
    from sqlmodel import SQLModel

    tabelas = [
        SQLModel.metadata.tables[t]
        for t in _TABELAS
        if t not in existentes and t in SQLModel.metadata.tables
    ]
    for tabela in tabelas:
        # As tabelas nascem do modelo de HOJE, e o modelo envelhece com o código:
        # a `tarefas` ganhou entretanto uma chave estrangeira para
        # `pareceres_importados`, que só é criada quatro migrações à frente.
        #
        # Numa instalação nova isto nunca se nota — a 001 cria tudo de uma vez e
        # esta migração não chega a correr. Numa ATUALIZAÇÃO nota-se e é fatal: a
        # base foi criada pela 001 da versão antiga, que não conhecia nenhuma das
        # duas, e o `CREATE TABLE` rebentava a apontar para uma tabela que ainda
        # não existe. Com DDL transacional isso reverte a atualização inteira e
        # deixa o cliente na versão anterior, com o contentor em ciclo de arranque.
        #
        # Cria-se sem as chaves estrangeiras cujo destino ainda não existe; quem
        # as acrescenta é a migração que cria esse destino.
        adiaveis = [
            fk for fk in tabela.foreign_key_constraints
            if fk.referred_table.name not in existentes
            and fk.referred_table.name != tabela.name
        ]
        # `existentes` cresce à medida que se cria: as duas tabelas nascem aqui e
        # a segunda referencia a primeira. Sem isto, a fotografia tirada antes do
        # ciclo dizia que `tarefas` ainda não existia quando chegava a vez de
        # `tarefa_conclusoes`, e a chave entre as duas era adiada para sempre —
        # ninguém a repõe, porque a tabela de destino é criada aqui e não à frente.
        existentes.add(tabela.name)
        if adiaveis:
            # Os tipos ENUM têm de nascer antes da tabela que os usa. O
            # `create_all` trata disso sozinho; o `CreateTable` em cru não —
            # e a tabela falharia com "type ... does not exist".
            for coluna in tabela.columns:
                criar_tipo = getattr(coluna.type, "create", None)
                if criar_tipo is not None:
                    criar_tipo(bind, checkfirst=True)
            manter = [fk for fk in tabela.foreign_key_constraints if fk not in adiaveis]
            op.execute(CreateTable(tabela, include_foreign_key_constraints=manter))
            for indice in tabela.indexes:
                op.execute(CreateIndex(indice))
        else:
            SQLModel.metadata.create_all(bind, tables=[tabela], checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    existentes = set(inspect(bind).get_table_names())
    import app.tarefas.models  # noqa: F401
    from sqlmodel import SQLModel

    # Ordem inversa (conclusões referenciam tarefas).
    for t in reversed(_TABELAS):
        if t in existentes and t in SQLModel.metadata.tables:
            SQLModel.metadata.tables[t].drop(bind, checkfirst=True)
