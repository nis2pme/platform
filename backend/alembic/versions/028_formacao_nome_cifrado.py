"""O nome do participante de formação passa a ser cifrado em repouso.

`formacao_participantes.nome` guardava em claro o nome de pessoas — quase
sempre colaboradores, às vezes externos — enquanto todos os outros campos com
nomes de pessoas vão cifrados com a chave de PII. Esta migração cifra as linhas
existentes; o código passa a escrever cifrado e a decifrar ao ler.

Idempotente: um valor que já é um criptograma (começa por `gAAAA`) fica como
está — correr duas vezes não cifra duas vezes. Sem downgrade: reverter seria
voltar a escrever dados pessoais em claro de propósito.

Revision ID: 028_formacao_nome_cifrado
Revises: 027_convergencia_esquema
Create Date: 2026-09-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "028_formacao_nome_cifrado"
down_revision: Union[str, None] = "027_convergencia_esquema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _parece_cifrado(valor: str) -> bool:
    return valor.startswith("gAAAA")


def upgrade() -> None:
    from app.shared.pii import cifrar_pii

    ligacao = op.get_bind()
    linhas = ligacao.execute(
        sa.text("SELECT id, nome FROM formacao_participantes WHERE nome IS NOT NULL AND nome <> ''")
    ).fetchall()
    for id_, nome in linhas:
        if _parece_cifrado(nome):
            continue
        ligacao.execute(
            sa.text("UPDATE formacao_participantes SET nome = :nome WHERE id = :id"),
            {"nome": cifrar_pii(nome), "id": id_},
        )
    # O criptograma de um nome de 255 caracteres não cabe em 255: alarga-se a
    # coluna para o mesmo tamanho dos outros nomes cifrados.
    op.alter_column(
        "formacao_participantes",
        "nome",
        existing_type=sa.String(length=255),
        type_=sa.String(length=500),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Sem downgrade: voltar a gravar nomes em claro seria reintroduzir o defeito.
    pass
