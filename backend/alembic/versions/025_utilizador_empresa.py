"""Adesão utilizador↔empresa — só schema.

`utilizadores.empresa_id` é uma coluna: um utilizador pertence a exatamente uma
empresa. Quem presta serviço de IT a 20 PME precisa de 20 contas, 20
palavras-passe e 20 enrolamentos de 2FA — um travão ao canal que leva o produto à
PME, que é justamente o prestador de IT.

**Só a tabela, nesta migração.** `utilizadores.empresa_id` mantém-se como a
empresa ativa da sessão e continua a ser o que todos os filtros usam; a UI
continua a permitir uma empresa só. É de propósito: a parte cara é mudar a
autenticação, os claims do JWT, o RBAC e todos os filtros de consulta, e essa
faz-se quando o canal existir. O que não se pode adiar é a **forma dos dados** —
depois seria com clientes em produção e sessões vivas.

Não há papéis novos. Quem avalia de fora não tem conta aqui: usa a plataforma do
auditor e recebe um dossiê `.nis2pme`. O `papel` desta tabela é o mesmo
vocabulário que já existe, com a diferença de passar a ser **por empresa**.

Retoma: uma linha por utilizador ativo, com o papel que já tem, para a tabela
nascer a dizer a verdade em vez de vazia.

Revision ID: 025_utilizador_empresa
Revises: 024_controlo_estado_historico
Create Date: 2026-09-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "025_utilizador_empresa"
down_revision: Union[str, None] = "024_controlo_estado_historico"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialeto = bind.dialect.name

    if "utilizador_empresa" in inspector.get_table_names():
        return

    op.create_table(
        "utilizador_empresa",
        sa.Column("utilizador_id", sa.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("empresa_id", sa.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("papel", sa.String(length=30), nullable=False),
        sa.Column("valido_ate", sa.DateTime(), nullable=True),
        sa.Column("criado_por_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["utilizador_id"], ["utilizadores.id"]),
        sa.ForeignKeyConstraint(["empresa_id"], ["empresas.id"]),
    )
    op.create_index("ix_utilizador_empresa_empresa_id", "utilizador_empresa", ["empresa_id"])

    # Retoma: a adesão que já existe implicitamente na coluna passa a estar
    # escrita. Só utilizadores ativos — recriar a adesão de contas desativadas
    # daria acesso a quem foi desligado, se um dia a tabela passar a decidir.
    agora = "now()" if dialeto == "postgresql" else "CURRENT_TIMESTAMP"
    op.execute(
        f"""
        INSERT INTO utilizador_empresa (utilizador_id, empresa_id, papel, criado_em)
        SELECT u.id, u.empresa_id, u.role, COALESCE(u.created_at, {agora})
        FROM utilizadores u
        WHERE u.empresa_id IS NOT NULL
          AND u.ativo = true
          AND NOT EXISTS (
              SELECT 1 FROM utilizador_empresa ue
              WHERE ue.utilizador_id = u.id AND ue.empresa_id = u.empresa_id
          )
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "utilizador_empresa" in inspector.get_table_names():
        op.drop_table("utilizador_empresa")
