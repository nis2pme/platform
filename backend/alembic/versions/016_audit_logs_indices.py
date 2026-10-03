"""audit_logs sob controlo de versões: ip_hash, user_agent sem teto, índices.

Até aqui esta tabela nascia por efeito colateral: o env.py do Alembic importa o
modelo, portanto o create_all da 001 criava-a e nenhuma migração descrevia o seu
schema. Esta migração assume-a e passa a ser o sítio por onde as alterações
futuras entram.

Estritamente idempotente. A tabela existe em bases instaladas há muito, e em
bases frescas o create_all da 001 já a cria com as colunas novas (elas estão no
modelo). Cada passo é condicionado ao que o inspetor encontrar.

Aditiva (regra permanente de compatibilidade).

Revision ID: 016_audit_logs_indices
Revises: 015_politica_capacidades
Create Date: 2026-08-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "016_audit_logs_indices"
down_revision: Union[str, None] = "015_politica_capacidades"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if "audit_logs" not in inspector.get_table_names():
        # Base sem a tabela: o create_all da 001 cria-a já com esta forma.
        return

    colunas = {c["name"] for c in inspector.get_columns("audit_logs")}
    indices = {i["name"] for i in inspector.get_indexes("audit_logs")}

    if "ip_hash" not in colunas:
        op.add_column(
            "audit_logs", sa.Column("ip_hash", sa.String(length=64), nullable=True)
        )
    if "ix_audit_logs_ip_hash" not in indices:
        op.create_index("ix_audit_logs_ip_hash", "audit_logs", ["ip_hash"])

    # O criptograma Fernet de um User-Agent de 500 caracteres ocupa 760 — não cabia
    # em varchar(700) e o INSERT rebentava, o que num caminho de autenticação
    # devolvia 500. O corte passou a ser feito no texto limpo, antes de cifrar; a
    # coluna deixa de impor um segundo teto para não voltar a haver dois.
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "audit_logs",
            "user_agent",
            existing_type=sa.String(length=700),
            type_=sa.Text(),
            existing_nullable=True,
        )
    # SQLite não impõe o comprimento declarado num VARCHAR — não há nada a alterar.

    # Padrão de acesso real da listagem: um tenant fixo, ordenado por recência.
    # Um btree percorre-se nos dois sentidos, logo serve o ORDER BY DESC tal como está.
    if "ix_audit_logs_empresa_created" not in indices:
        op.create_index(
            "ix_audit_logs_empresa_created",
            "audit_logs",
            ["empresa_id", "created_at"],
        )

    # Filtros por entidade expostos na API e sem índice nenhum até aqui.
    if "ix_audit_logs_entidade" not in indices:
        op.create_index(
            "ix_audit_logs_entidade",
            "audit_logs",
            ["entidade_tipo", "entidade_id"],
        )

    # Duplicava a chave primária: o mesmo btree, mantido duas vezes em cada INSERT.
    if "ix_audit_logs_id" in indices:
        op.drop_index("ix_audit_logs_id", table_name="audit_logs")


def downgrade() -> None:
    pass
