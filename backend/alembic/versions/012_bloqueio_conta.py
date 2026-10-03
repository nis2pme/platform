"""Bloqueio de conta no login (anti-brute-force por conta).

Defesa complementar ao rate-limit por IP, em duas camadas:

Por conta — após um número de falhas dentro de uma janela deslizante a conta
fica bloqueada temporariamente. Três colunas em `utilizadores`:

  - tentativas_falhadas (inteiro, contador de falhas na janela atual);
  - tentativas_janela_inicio (timestamp; início da janela deslizante);
  - bloqueado_ate (timestamp; conta bloqueada enquanto no futuro).

Por IP (anti password-spray) — nova tabela `login_bloqueios_ip` acumula falhas
do mesmo IP entre todas as contas; ao atingir o limiar o IP é bloqueado. O IP é
guardado só como hash SHA-256 (não reversível).

Idempotente: instalações novas recebem as colunas via create_all (001); aqui só
se acrescenta o que falta a bases existentes.

Revision ID: 012_bloqueio_conta
Revises: 011_controlo_nao_aplicavel
Create Date: 2026-07-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "012_bloqueio_conta"
down_revision: Union[str, None] = "011_controlo_nao_aplicavel"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    colunas = {c["name"] for c in inspector.get_columns("utilizadores")}
    if "tentativas_falhadas" not in colunas:
        op.add_column(
            "utilizadores",
            sa.Column(
                "tentativas_falhadas",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    if "tentativas_janela_inicio" not in colunas:
        op.add_column(
            "utilizadores",
            sa.Column(
                "tentativas_janela_inicio", sa.DateTime(timezone=True), nullable=True
            ),
        )
    if "bloqueado_ate" not in colunas:
        op.add_column(
            "utilizadores",
            sa.Column("bloqueado_ate", sa.DateTime(timezone=True), nullable=True),
        )

    # Camada por IP (anti password-spray).
    if "login_bloqueios_ip" not in inspector.get_table_names():
        op.create_table(
            "login_bloqueios_ip",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("ip_hash", sa.String(length=64), nullable=False),
            sa.Column("contador", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("janela_inicio", sa.DateTime(timezone=True), nullable=False),
            sa.Column("bloqueado_ate", sa.DateTime(timezone=True), nullable=True),
            sa.Column("atualizado_em", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_login_bloqueios_ip_ip_hash",
            "login_bloqueios_ip",
            ["ip_hash"],
            unique=True,
        )


def downgrade() -> None:
    # Aditiva por regra permanente — sem downgrade.
    pass
