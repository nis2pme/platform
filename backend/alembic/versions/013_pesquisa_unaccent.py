"""Pesquisa global: correspondência insensível a acentos.

Sem isto, quem escreve "seguranca" não encontra "Segurança" — o caso comum em
português. A extensão fica no schema public.

Tolerante a falha: criar extensões exige privilégio elevado e nem todo o Postgres
gerido o concede. Se falhar, a migração NÃO quebra o arranque — a pesquisa deteta
a ausência e cai para correspondência simples (sensível a acentos).

Revision ID: 013_pesquisa_unaccent
Revises: 012_bloqueio_conta
Create Date: 2026-07-20
"""
import logging
from typing import Sequence, Union

from alembic import op

revision: str = "013_pesquisa_unaccent"
down_revision: Union[str, None] = "012_bloqueio_conta"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    try:
        op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    except Exception as exc:  # noqa: BLE001 — degradação deliberada, ver docstring
        logger.warning(
            "unaccent não instalada (%s) — a pesquisa fica sensível a acentos.", exc
        )


def downgrade() -> None:
    # Aditiva por regra permanente — a extensão pode ser usada por outros.
    pass
