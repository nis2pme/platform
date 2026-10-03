"""Volta a cifrar os dados pessoais que versões antigas regravaram em claro.

Até à correção de julho, alguns validadores decifravam os campos pessoais
diretamente na entidade lida da base, e o commit do pedido gravava-os de volta
em texto simples: o nome de cada utilizador que entrava, os dados da empresa e
os relatórios de auditoria interna. A versão atual espera esses campos
cifrados; um valor em claro não decifra, a leitura devolve vazio e o login com
2FA dessas contas acabava em erro 500.

A correção de julho impediu que voltasse a acontecer, mas nada reparou as linhas
já gravadas. Esta migração cifra-as.

Idempotente: um valor que já é um criptograma (começa por `gAAAA`) fica como
está. Um valor que parece criptograma mas não decifra (chave errada, dado
corrompido) também não se toca: cifrá-lo outra vez escondia o problema. Sem
downgrade: reverter seria voltar a escrever dados pessoais em claro.

Revision ID: 032_pii_em_claro
Revises: 031_origem_transicao_texto
Create Date: 2026-09-26
"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "032_pii_em_claro"
down_revision: Union[str, None] = "031_origem_transicao_texto"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

# Os campos que os validadores antigos regravavam em claro.
_CAMPOS = (
    ("utilizadores", "nome"),
    ("empresas", "nome"),
    ("empresas", "nif"),
    ("empresas", "email"),
    ("empresas", "website"),
    ("relatorios_auditoria", "auditor_nome"),
    ("relatorios_auditoria", "texto"),
)


def _parece_cifrado(valor: str) -> bool:
    return valor.startswith("gAAAA")


def _existe(ligacao, tabela: str, coluna: str) -> bool:
    return bool(ligacao.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabela, "c": coluna},
    ).scalar())


def upgrade() -> None:
    from app.shared.pii import cifrar_pii

    ligacao = op.get_bind()
    for tabela, coluna in _CAMPOS:
        if not _existe(ligacao, tabela, coluna):
            continue
        linhas = ligacao.execute(sa.text(
            f"SELECT id, {coluna} FROM {tabela} WHERE {coluna} IS NOT NULL AND {coluna} <> ''"
        )).fetchall()
        cifrados = 0
        for id_, valor in linhas:
            if _parece_cifrado(valor):
                continue
            ligacao.execute(
                sa.text(f"UPDATE {tabela} SET {coluna} = :valor WHERE id = :id"),
                {"valor": cifrar_pii(valor), "id": id_},
            )
            cifrados += 1
        if cifrados:
            logger.info("%s.%s: %d valor(es) em claro voltaram a ser cifrados.", tabela, coluna, cifrados)


def downgrade() -> None:
    # Sem downgrade: voltar a gravar dados pessoais em claro seria reintroduzir o defeito.
    pass
