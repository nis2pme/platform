"""Lápide do apagamento a pedido do titular.

O RGPD puxa ao contrário da retenção. O produto quer guardar — uma prova que
sustentou um controlo, ou que saiu num dossiê, não se recicla — mas se o
conteúdo tiver dados pessoais e houver pedido de apagamento, tem de ser possível
apagar.

A saída é apagar o **conteúdo** e deixar uma **lápide**: a impressão digital do
que lá estava, a data, quem o mandou e porquê. A história passa a dizer
*"evidência eliminada a `<data>`"* em vez de apontar para o nada — que é a
diferença entre «isto foi apagado porque a lei obriga» e um espaço em branco
numa auditoria.

## Porque são colunas e não só uma linha na trilha

A trilha de auditoria tem retenção própria (`AUDIT_RETENCAO_DIAS`) e é
arquivada. A lápide tem de durar o que durar a evidência: quem a lê está a
perguntar *por esta prova*, e a resposta não pode depender de o registo do ato
ainda estar dentro da janela.

O `conteudo_hash` já existe e serve de impressão digital — não se duplica aqui.

Revision ID: 026_evidencia_lapide_rgpd
Revises: 025_utilizador_empresa
Create Date: 2026-09-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "026_evidencia_lapide_rgpd"
down_revision: Union[str, None] = "025_utilizador_empresa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tem_coluna(inspector, tabela: str, coluna: str) -> bool:
    return any(c["name"] == coluna for c in inspector.get_columns(tabela))


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # Distingue o apagamento a pedido do titular de uma eliminação normal. Sem
    # esta marca, as duas ficariam com o mesmo aspeto (`deleted_at` preenchido) e
    # a retenção não saberia qual delas pode ignorar.
    if not _tem_coluna(inspector, "evidencias", "eliminacao_rgpd"):
        op.add_column(
            "evidencias",
            sa.Column(
                "eliminacao_rgpd", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
        )
    # Porquê. Texto livre, cifrado em repouso como o resto da PII: o motivo de um
    # pedido de apagamento identifica frequentemente quem o pediu.
    if not _tem_coluna(inspector, "evidencias", "eliminacao_motivo"):
        op.add_column(
            "evidencias", sa.Column("eliminacao_motivo", sa.Text(), nullable=True)
        )
    # Quem. Sem FK: a conta pode ser anonimizada mais tarde, e a lápide tem de
    # sobreviver a isso — o identificador fica, o nome resolve-se se existir.
    if not _tem_coluna(inspector, "evidencias", "eliminacao_por_id"):
        op.add_column(
            "evidencias",
            sa.Column("eliminacao_por_id", sa.UUID(as_uuid=True), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for coluna in ("eliminacao_por_id", "eliminacao_motivo", "eliminacao_rgpd"):
        if _tem_coluna(inspector, "evidencias", coluna):
            op.drop_column("evidencias", coluna)
