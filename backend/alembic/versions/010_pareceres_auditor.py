"""Parecer do auditor (round-trip): pareceres_importados + auditores_confiaveis

Importar um parecer .nis2pme cria relatórios de auditoria EXTERNOS e tarefas
de plano de ação — esta migração acrescenta o que falta a bases existentes:

  - tabelas novas: pareceres_importados (registo + anti-replay + selo) e
    auditores_confiaveis (chaves públicas fixadas por TOFU);
  - relatorios_auditoria: auditor_id passa a aceitar NULL (o auditor externo
    não é utilizador da app) + colunas externo / estado_externo / parecer_id;
  - tarefas: coluna origem_parecer_id (achado do parecer → tarefa).

Idempotente: instalações novas já recebem tudo via create_all (001 com os
modelos registados no env.py); aqui só se cria/altera o que ainda falta.

Revision ID: 010_pareceres_auditor
Revises: 009_dossies_gerados
Create Date: 2026-07-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "010_pareceres_auditor"
down_revision: Union[str, None] = "009_dossies_gerados"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABELAS = ("pareceres_importados", "auditores_confiaveis")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existentes = set(inspector.get_table_names())

    # Regista os modelos nos metadados SQLModel e cria só o que falta.
    import app.dossie.models  # noqa: F401
    from sqlmodel import SQLModel

    tabelas = [
        SQLModel.metadata.tables[t]
        for t in _TABELAS
        if t not in existentes and t in SQLModel.metadata.tables
    ]
    if tabelas:
        SQLModel.metadata.create_all(bind, tables=tabelas, checkfirst=True)

    # --- relatorios_auditoria: campos do relatório externo -------------------
    colunas = {c["name"]: c for c in inspector.get_columns("relatorios_auditoria")}

    if colunas.get("auditor_id", {}).get("nullable") is False:
        op.alter_column(
            "relatorios_auditoria",
            "auditor_id",
            existing_type=postgresql.UUID(as_uuid=True),
            nullable=True,
        )

    if "externo" not in colunas:
        # server_default para preencher as linhas existentes; remove-se logo a
        # seguir para o schema convergir com o das instalações frescas.
        op.add_column(
            "relatorios_auditoria",
            sa.Column("externo", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.alter_column("relatorios_auditoria", "externo", server_default=None)

    if "estado_externo" not in colunas:
        op.add_column(
            "relatorios_auditoria",
            sa.Column("estado_externo", sa.String(length=20), nullable=True),
        )

    if "parecer_id" not in colunas:
        op.add_column(
            "relatorios_auditoria",
            sa.Column("parecer_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_relatorios_auditoria_parecer_id",
            "relatorios_auditoria",
            "pareceres_importados",
            ["parecer_id"],
            ["id"],
        )
        op.create_index(
            "ix_relatorios_auditoria_parecer_id", "relatorios_auditoria", ["parecer_id"]
        )

    # --- tarefas: origem no parecer ------------------------------------------
    # A coluna e a chave estrangeira são tratadas em SEPARADO de propósito. Numa
    # instalação nova a coluna já vem da 001 (nasce do modelo) mas a restrição
    # chega com o nome que o SQLAlchemy inventa; numa atualização a coluna vem da
    # 006, que adia a chave por a tabela de destino só existir aqui. Guardar as
    # duas com a mesma condição deixava sempre um dos caminhos por servir — e era
    # isso que fazia o `downgrade` tentar remover uma restrição inexistente.
    colunas_tarefas = {c["name"] for c in inspector.get_columns("tarefas")}
    if "origem_parecer_id" not in colunas_tarefas:
        op.add_column(
            "tarefas",
            sa.Column("origem_parecer_id", postgresql.UUID(as_uuid=True), nullable=True),
        )

    ja_referencia = any(
        "origem_parecer_id" in (fk.get("constrained_columns") or [])
        for fk in inspector.get_foreign_keys("tarefas")
    )
    if not ja_referencia:
        op.create_foreign_key(
            "fk_tarefas_origem_parecer_id",
            "tarefas",
            "pareceres_importados",
            ["origem_parecer_id"],
            ["id"],
        )

    indices_tarefas = {i["name"] for i in inspector.get_indexes("tarefas")}
    if "ix_tarefas_origem_parecer_id" not in indices_tarefas:
        op.create_index("ix_tarefas_origem_parecer_id", "tarefas", ["origem_parecer_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    colunas_tarefas = {c["name"] for c in inspector.get_columns("tarefas")}
    if "origem_parecer_id" in colunas_tarefas:
        # Cada peça é removida sob a sua própria condição: o índice e a restrição
        # podem não existir, ou existir com o nome que o SQLAlchemy gerou numa
        # instalação nova. Remover pelo nome fixo, sem confirmar, era o que
        # rebentava a reversão a meio e a deixava sem efeito nenhum.
        indices_tarefas = {i["name"] for i in inspector.get_indexes("tarefas")}
        if "ix_tarefas_origem_parecer_id" in indices_tarefas:
            op.drop_index("ix_tarefas_origem_parecer_id", table_name="tarefas")
        for fk in inspector.get_foreign_keys("tarefas"):
            if "origem_parecer_id" in (fk.get("constrained_columns") or []) and fk.get("name"):
                op.drop_constraint(fk["name"], "tarefas", type_="foreignkey")
        op.drop_column("tarefas", "origem_parecer_id")

    colunas = {c["name"] for c in inspector.get_columns("relatorios_auditoria")}
    if "parecer_id" in colunas:
        op.drop_index(
            "ix_relatorios_auditoria_parecer_id", table_name="relatorios_auditoria"
        )
        op.drop_constraint(
            "fk_relatorios_auditoria_parecer_id", "relatorios_auditoria", type_="foreignkey"
        )
        op.drop_column("relatorios_auditoria", "parecer_id")
    if "estado_externo" in colunas:
        op.drop_column("relatorios_auditoria", "estado_externo")
    if "externo" in colunas:
        op.drop_column("relatorios_auditoria", "externo")

    existentes = set(inspector.get_table_names())
    for t in _TABELAS:
        if t in existentes:
            op.drop_table(t)
