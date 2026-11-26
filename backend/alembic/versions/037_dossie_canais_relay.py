"""Canal de resposta dos dossiês enviados pelo relay.

Ao enviar um dossiê pelo relay, a instância passa a abrir lá um canal de
resposta: manda o hash de um token aleatório que só ela guarda (cifrado), e com
ele procura e levanta o parecer que o auditor deixar sobre esse dossiê. A
tabela é nova e fica à parte de `dossies_gerados`, que é um registo imutável.

Uma instalação nova já recebe a tabela dos modelos; aqui só se cria se faltar.
Nada se converte: os dossiês enviados antes não têm canal e o parecer deles
continua a chegar por ficheiro.

Revision ID: 037_dossie_canais_relay
Revises: 036_incidente_eventos_i18n
Create Date: 2026-11-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "037_dossie_canais_relay"
down_revision: Union[str, None] = "036_incidente_eventos_i18n"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABELA = "dossie_canais_relay"
_INDICES = ("empresa_id", "estado")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABELA in inspector.get_table_names():
        return
    op.create_table(
        _TABELA,
        sa.Column("dossie_gerado_id", sa.Uuid(), nullable=False),
        sa.Column("empresa_id", sa.Uuid(), nullable=False),
        sa.Column("relay_url", sa.String(length=500), nullable=False),
        sa.Column("relay_convite_id", sa.String(length=128), nullable=False),
        sa.Column("token_resposta", sa.Text(), nullable=False),
        sa.Column("estado", sa.String(length=20), nullable=False),
        sa.Column("canal_expira_em", sa.DateTime(), nullable=False),
        sa.Column("parecer_sha256", sa.String(length=64), nullable=True),
        sa.Column("parecer_expira_em", sa.DateTime(), nullable=True),
        sa.Column("detetado_em", sa.DateTime(), nullable=True),
        sa.Column("importado_sha256", sa.String(length=64), nullable=True),
        sa.Column("confirmar_sha256", sa.String(length=64), nullable=True),
        sa.Column("lembrete_enviado", sa.Boolean(), nullable=False),
        sa.Column("ultima_procura", sa.DateTime(), nullable=True),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("dossie_gerado_id"),
        sa.ForeignKeyConstraint(["dossie_gerado_id"], ["dossies_gerados.id"]),
        sa.ForeignKeyConstraint(["empresa_id"], ["empresas.id"]),
    )
    for coluna in _INDICES:
        op.create_index(f"ix_{_TABELA}_{coluna}", _TABELA, [coluna])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABELA not in inspector.get_table_names():
        return
    for coluna in _INDICES:
        op.drop_index(f"ix_{_TABELA}_{coluna}", table_name=_TABELA)
    op.drop_table(_TABELA)
