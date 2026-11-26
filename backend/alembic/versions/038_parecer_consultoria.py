"""Consultoria ao lado da auditoria, e pareceres que se seguem uns aos outros.

- `pareceres_importados.natureza`: "auditoria" ou "consultoria". Os que já
  existem foram todos pareceres de auditor.
- `pareceres_importados.conteudo`: o parecer normalizado, cifrado, para se
  poder ler na app. Os já importados ficam sem ele (só o nível ficava).
- `tarefas.origem_achado_id` e `tarefas.severidade`: o mesmo achado num
  parecer seguinte atualiza a sua tarefa em vez de criar outra.
- `auditores_confiaveis.proximo_convite` e `proximo_convite_expira_em`: o
  convite que veio no último ficheiro, para o envio seguinte do dossiê.

Uma instalação nova já recebe as colunas dos modelos; aqui só se acrescentam
as que faltam.

Revision ID: 038_parecer_consultoria
Revises: 037_dossie_canais_relay
Create Date: 2026-11-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "038_parecer_consultoria"
down_revision: Union[str, None] = "037_dossie_canais_relay"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _colunas(tabela: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(tabela)}


def _indices(tabela: str) -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(tabela)}


def upgrade() -> None:
    pareceres = _colunas("pareceres_importados")
    if "natureza" not in pareceres:
        op.add_column(
            "pareceres_importados",
            sa.Column("natureza", sa.String(length=20), nullable=False, server_default="auditoria"),
        )
        op.alter_column("pareceres_importados", "natureza", server_default=None)
    if "conteudo" not in pareceres:
        op.add_column("pareceres_importados", sa.Column("conteudo", sa.Text(), nullable=True))

    tarefas = _colunas("tarefas")
    if "origem_achado_id" not in tarefas:
        op.add_column("tarefas", sa.Column("origem_achado_id", sa.String(length=36), nullable=True))
    if "ix_tarefas_origem_achado_id" not in _indices("tarefas"):
        op.create_index("ix_tarefas_origem_achado_id", "tarefas", ["origem_achado_id"])
    if "severidade" not in tarefas:
        op.add_column("tarefas", sa.Column("severidade", sa.String(length=20), nullable=True))

    auditores = _colunas("auditores_confiaveis")
    if "proximo_convite" not in auditores:
        op.add_column("auditores_confiaveis", sa.Column("proximo_convite", sa.Text(), nullable=True))
    if "proximo_convite_expira_em" not in auditores:
        op.add_column(
            "auditores_confiaveis", sa.Column("proximo_convite_expira_em", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    # Perde-se o conteúdo dos pareceres e o convite guardado; as tarefas ficam,
    # sem a ligação ao achado (um parecer seguinte voltaria a criá-las).
    auditores = _colunas("auditores_confiaveis")
    for coluna in ("proximo_convite_expira_em", "proximo_convite"):
        if coluna in auditores:
            op.drop_column("auditores_confiaveis", coluna)
    if "ix_tarefas_origem_achado_id" in _indices("tarefas"):
        op.drop_index("ix_tarefas_origem_achado_id", table_name="tarefas")
    tarefas = _colunas("tarefas")
    for coluna in ("severidade", "origem_achado_id"):
        if coluna in tarefas:
            op.drop_column("tarefas", coluna)
    pareceres = _colunas("pareceres_importados")
    for coluna in ("conteudo", "natureza"):
        if coluna in pareceres:
            op.drop_column("pareceres_importados", coluna)
