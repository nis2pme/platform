"""História de conformidade por controlo.

O que existia era o `historico_maturidade`, que guarda o score **agregado** e
responde a "como evoluiu a percentagem". A pergunta que uma auditoria faz é
outra: *"este controlo estava implementado a 2027-03-01, e com que prova?"*.

O log de auditoria também não responde. É forense de **acesso** — quem viu, quem
mexeu, de que IP —, tem retenção curta por desenho (`AUDIT_RETENCAO_DIAS`, 365
dias) e é arquivado e purgado. Reconstruir o estado de um controlo numa data
obrigaria a varrer o log inteiro e a reproduzir transições a partir de JSON
guardado como texto, com metade já fora da base. E nem toda a transição vem de
uma ação de utilizador: há ticks, importações e cascatas.

**Append-only, uma linha por transição REAL — nunca fotografias periódicas.** É
essa decisão que mantém o custo irrelevante: 107 controlos e, por excesso, cinco
mudanças por controlo por ano dão ~535 linhas/ano, ou seja menos de 2 MB por
empresa numa década. O que geraria volume a sério seriam snapshots periódicos,
que é precisamente o que este desenho não faz.

Sem backfill: as transições passadas não estão registadas em lado nenhum de onde
se possam recuperar com fidelidade, e inventá-las a partir do estado atual daria
uma história falsa — pior do que não haver história.

Revision ID: 024_controlo_estado_historico
Revises: 023_evidencia_requisito_nn
Create Date: 2026-09-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "024_controlo_estado_historico"
down_revision: Union[str, None] = "023_evidencia_requisito_nn"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "controlo_estado_historico" in inspector.get_table_names():
        return

    op.create_table(
        "controlo_estado_historico",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("empresa_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("controlo_empresa_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("estado_anterior", sa.String(length=30), nullable=True),
        sa.Column("estado_novo", sa.String(length=30), nullable=False),
        sa.Column("nivel_qnrcs_em_vigor", sa.String(length=30), nullable=True),
        sa.Column("evidencias", sa.Text(), nullable=True),
        # O enum viaja como texto: um tipo enumerado na base obrigaria a uma
        # migração de tipo cada vez que aparecesse uma origem nova (foi o que a
        # `021` teve de fazer no `resultadoacao`), e aqui não há ganho que o
        # justifique — a validação está no modelo.
        sa.Column("origem", sa.String(length=20), nullable=False, server_default="utilizador"),
        sa.Column("utilizador_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("ocorrido_em", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["empresa_id"], ["empresas.id"]),
    )
    op.create_index(
        "ix_controlo_estado_historico_empresa_id", "controlo_estado_historico", ["empresa_id"]
    )
    op.create_index(
        "ix_controlo_estado_historico_ocorrido_em", "controlo_estado_historico", ["ocorrido_em"]
    )
    # O índice que serve a pergunta "estado deste controlo àquela data": procura
    # pelo controlo e ordena por instante, numa passagem só.
    op.create_index(
        "ix_controlo_estado_historico_controlo_em",
        "controlo_estado_historico",
        ["controlo_empresa_id", "ocorrido_em"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "controlo_estado_historico" in inspector.get_table_names():
        op.drop_table("controlo_estado_historico")
