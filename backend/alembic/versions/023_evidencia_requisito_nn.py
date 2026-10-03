"""Evidência ↔ requisito passa a N:N, com versões.

`evidencias.controlo_empresa_v2_id` era uma chave estrangeira única: uma
evidência pertencia a **um** controlo. Doía já — a política de segurança da
informação evidencia vários controlos do próprio QNRCS, e o utilizador tinha de a
carregar uma vez por controlo — e doía muito mais no multi-framework (ISO 27001,
RGPD, DORA), onde cada norma nova duplicaria todas as evidências do cliente, e o
dossiê e o produto do auditor duplicariam atrás.

**Porque é que isto entra antes do e2e.** A evidência é o objeto mais exercitado
do guião: validar os fluxos de evidência numa VPS e mudar-lhes o modelo a seguir
obrigaria a repetir essa parte toda. E depois custa o mesmo, mas com milhares de
evidências em produção, um formato de dossiê publicado a respeitar e o
verificador de referência a manter compatível.

## A tabela de ligação

Append-only, como o resto do modelo. A chave primária é **sintética de
propósito**: com `PRIMARY KEY (evidencia_id, requisito_id)` seria impossível
registar que uma evidência foi desligada de um controlo e mais tarde religada —
o segundo `ligar` colidiria com a linha antiga. Desligar preenche `desligado_em`,
religar cria linha nova, e o índice único **parcial** garante que só existe uma
ligação ativa por par.

## O que esta migração NÃO faz

Não apaga `evidencias.controlo_empresa_v2_id`. A coluna deixa de ser lida — a
verdade passa a ser a tabela de ligação — mas remover uma coluna a que o código
anterior possa aceder, na mesma migração que cria a alternativa, deixa uma
instalação a meio do caminho sem retorno. A remoção é uma migração própria,
depois de o staging confirmar. Enquanto lá estiver, fica preenchida na criação
como marca da ligação de origem, e nada a lê para decidir.

Revision ID: 023_evidencia_requisito_nn
Revises: 022_auditoria_cadeia_hash
Create Date: 2026-09-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "023_evidencia_requisito_nn"
down_revision: Union[str, None] = "022_auditoria_cadeia_hash"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tem_coluna(inspector, tabela: str, coluna: str) -> bool:
    return any(c["name"] == coluna for c in inspector.get_columns(tabela))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialeto = bind.dialect.name

    # --- versões da evidência -----------------------------------------------
    if not _tem_coluna(inspector, "evidencias", "substitui_id"):
        op.add_column("evidencias", sa.Column("substitui_id", sa.UUID(as_uuid=True), nullable=True))
        op.create_index("ix_evidencias_substitui_id", "evidencias", ["substitui_id"])
    if not _tem_coluna(inspector, "evidencias", "substituida_em"):
        op.add_column("evidencias", sa.Column("substituida_em", sa.DateTime(), nullable=True))
    if not _tem_coluna(inspector, "evidencias", "valido_ate"):
        op.add_column("evidencias", sa.Column("valido_ate", sa.DateTime(), nullable=True))

    # --- tabela de ligação --------------------------------------------------
    if "evidencia_requisito" not in inspector.get_table_names():
        op.create_table(
            "evidencia_requisito",
            sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("evidencia_id", sa.UUID(as_uuid=True), nullable=False),
            sa.Column("requisito_id", sa.UUID(as_uuid=True), nullable=False),
            sa.Column("empresa_id", sa.UUID(as_uuid=True), nullable=False),
            sa.Column("nota_ambito", sa.String(length=500), nullable=True),
            sa.Column(
                "ambito_por_confirmar", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("ligado_por_id", sa.UUID(as_uuid=True), nullable=True),
            sa.Column("ligado_em", sa.DateTime(), nullable=False),
            sa.Column("desligado_em", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["evidencia_id"], ["evidencias.id"]),
            sa.ForeignKeyConstraint(["empresa_id"], ["empresas.id"]),
        )
        op.create_index("ix_evidencia_requisito_evidencia_id", "evidencia_requisito", ["evidencia_id"])
        op.create_index("ix_evidencia_requisito_requisito_id", "evidencia_requisito", ["requisito_id"])
        op.create_index("ix_evidencia_requisito_empresa_id", "evidencia_requisito", ["empresa_id"])

        # Só uma ligação ATIVA por par. O índice parcial é o que torna possível
        # desligar e religar sem colidir com a linha histórica; em SQLite (testes)
        # os índices parciais também existem, com a mesma sintaxe.
        op.create_index(
            "uq_evidencia_requisito_ativa",
            "evidencia_requisito",
            ["evidencia_id", "requisito_id"],
            unique=True,
            postgresql_where=sa.text("desligado_em IS NULL"),
            sqlite_where=sa.text("desligado_em IS NULL"),
        )

    # --- retoma dos dados existentes ---------------------------------------
    # Uma linha de ligação por evidência viva que tenha controlo. As evidências
    # apagadas (soft delete) ficam de fora: religá-las traria de volta ao ecrã
    # provas que alguém retirou.
    #
    # `ligado_em` herda o `created_at` da evidência, não o instante da migração:
    # a história tem de dizer quando a prova passou a sustentar o controlo, e não
    # quando é que se mudou o modelo de dados.
    agora = "now()" if dialeto == "postgresql" else "CURRENT_TIMESTAMP"
    gerar_id = "gen_random_uuid()" if dialeto == "postgresql" else "lower(hex(randomblob(16)))"
    op.execute(
        f"""
        INSERT INTO evidencia_requisito
            (id, evidencia_id, requisito_id, empresa_id, ligado_por_id, ligado_em)
        SELECT {gerar_id}, e.id, e.controlo_empresa_v2_id, e.empresa_id,
               e.uploaded_by_id, COALESCE(e.created_at, {agora})
        FROM evidencias e
        WHERE e.controlo_empresa_v2_id IS NOT NULL
          AND e.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM evidencia_requisito er
              WHERE er.evidencia_id = e.id
                AND er.requisito_id = e.controlo_empresa_v2_id
                AND er.desligado_em IS NULL
          )
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "evidencia_requisito" in inspector.get_table_names():
        op.drop_table("evidencia_requisito")
    for coluna, indice in (
        ("valido_ate", None),
        ("substituida_em", None),
        ("substitui_id", "ix_evidencias_substitui_id"),
    ):
        if _tem_coluna(inspector, "evidencias", coluna):
            if indice:
                op.drop_index(indice, table_name="evidencias")
            op.drop_column("evidencias", coluna)
