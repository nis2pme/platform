"""O registo de auditoria passa a ser encadeado por hash.

Até aqui a tabela era imutável **por convenção** — nada impedia um `UPDATE` de
reescrever uma linha sem deixar rasto. Cada linha passa a levar o SHA-256 do seu
conteúdo, calculado sobre o hash da linha anterior da mesma empresa: alterar a
linha N obriga a recalcular de N até ao fim.

**Porque é que isto não se faz mais tarde.** Uma linha já escrita não tem hash e
não há como lho dar: calculá-lo agora, a partir do conteúdo atual, provaria
apenas que o conteúdo é o que está lá hoje — que é exatamente a pergunta a que a
cadeia serve para responder. A cadeia só pode começar no dia em que é criada, e
todos os registos anteriores ficam para trás. Cada dia de adiamento é um dia de
registos sem proteção. É por isso que esta migração não faz backfill: não é uma
omissão, é a única coisa honesta a fazer.

As colunas são NULL-able por isso mesmo. A verificação salta as linhas sem hash
em vez de as acusar — uma cadeia que se declarasse partida por causa da história
anterior seria um alarme que ninguém voltaria a olhar.

`audit_hash_chain_head` guarda o *head* de cada empresa: é a linha que se bloqueia
para serializar as escritas (é o que impede duas linhas de apontarem para o mesmo
anterior) e é o valor que sai da máquina no dossiê e no backup. Sem sair, a
cadeia não vale contra quem manda na base.

Revision ID: 022_auditoria_cadeia_hash
Revises: 021_auditoria_acesso_negado
Create Date: 2026-09-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "022_auditoria_cadeia_hash"
down_revision: Union[str, None] = "021_auditoria_acesso_negado"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tem_coluna(inspector, tabela: str, coluna: str) -> bool:
    return any(c["name"] == coluna for c in inspector.get_columns(tabela))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Idempotente: uma instalação que já tenha corrido isto (ou que arranque de
    # um `create_all` com os modelos atuais) não pode falhar aqui.
    if not _tem_coluna(inspector, "audit_logs", "hash_anterior"):
        op.add_column("audit_logs", sa.Column("hash_anterior", sa.String(length=64), nullable=True))
    if not _tem_coluna(inspector, "audit_logs", "hash_registo"):
        op.add_column("audit_logs", sa.Column("hash_registo", sa.String(length=64), nullable=True))
        op.create_index("ix_audit_logs_hash_registo", "audit_logs", ["hash_registo"])
    # O digest do conteúdo é assinado no lugar do texto: a purga mascara os dados
    # e assinar o texto faria a cadeia partir toda de uma vez nesse momento.
    if not _tem_coluna(inspector, "audit_logs", "dados_hash"):
        op.add_column("audit_logs", sa.Column("dados_hash", sa.String(length=64), nullable=True))

    if "audit_hash_chain_head" not in inspector.get_table_names():
        op.create_table(
            "audit_hash_chain_head",
            sa.Column("empresa_id", sa.UUID(as_uuid=True), primary_key=True, nullable=False),
            # A cadeia da plataforma (ações sem empresa) vive sob o UUID nulo, que
            # o uuid4() nunca gera — por isso não há FK para `empresas`: a chave
            # não é sempre uma empresa real.
            sa.Column("head_hash", sa.String(length=64), nullable=False),
            sa.Column("sequencia", sa.Integer(), nullable=False, server_default="0"),
        )

    # Sem backfill, de propósito — ver o cabeçalho.


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "audit_hash_chain_head" in inspector.get_table_names():
        op.drop_table("audit_hash_chain_head")
    if _tem_coluna(inspector, "audit_logs", "dados_hash"):
        op.drop_column("audit_logs", "dados_hash")
    if _tem_coluna(inspector, "audit_logs", "hash_registo"):
        op.drop_index("ix_audit_logs_hash_registo", table_name="audit_logs")
        op.drop_column("audit_logs", "hash_registo")
    if _tem_coluna(inspector, "audit_logs", "hash_anterior"):
        op.drop_column("audit_logs", "hash_anterior")
