"""A auditoria passa a saber dizer "negado".

Até aqui o resultado de uma ação só podia ser `SUCESSO` ou `FALHA`, e a tabela
registava o que foi FEITO — nunca o que foi TENTADO e recusado. Uma pessoa a
sondar sistematicamente rotas fora do seu papel não deixava linha nenhuma, que é
precisamente o sinal que permite detetar reconhecimento a partir de dentro.

Acrescenta-se o valor `NEGADO` ao tipo enumerado. O tipo guarda os NOMES dos
membros do enum de Python (é assim que o SQLAlchemy os escreve por omissão), por
isso o rótulo vai em maiúsculas, como os que já lá estão.

Idempotente: `ADD VALUE IF NOT EXISTS` não se queixa se já existir. Sem
downgrade — remover um rótulo de um enum obrigaria a reescrever o tipo, e as
linhas que já o usassem não teriam para onde ir.

Revision ID: 021_auditoria_acesso_negado
Revises: 020_regenerar_planos
Create Date: 2026-08-14
"""
from typing import Sequence, Union

from alembic import op

revision: str = "021_auditoria_acesso_negado"
down_revision: Union[str, None] = "020_regenerar_planos"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # O ALTER TYPE ... ADD VALUE não pode partilhar transação com quem venha a
    # USAR o valor novo. O autocommit fecha a transação da migração antes de o
    # acrescentar, e assim o rótulo fica visível às ligações seguintes.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE resultadoacao ADD VALUE IF NOT EXISTS 'NEGADO'")


def downgrade() -> None:
    # Um rótulo de enum não se remove sem reescrever o tipo inteiro, e as linhas
    # já gravadas com ele ficariam sem valor válido.
    pass
