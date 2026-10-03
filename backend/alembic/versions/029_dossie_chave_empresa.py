"""Chave de assinatura dos dossiês por empresa.

Até aqui a chave Ed25519 que assina os dossiês `.nis2pme` era uma por
instalação (`/app/data/dossie-assinatura.json`). Numa instalação local isso é
o mesmo que "uma por empresa"; em SaaS todos os clientes assinavam com a
mesma chave e apresentavam o mesmo *fingerprint* ao auditor. Passa a haver
uma chave por empresa, guardada nestas colunas (privada cifrada em repouso).

Adoção da chave existente: quando há exatamente UMA empresa e o ficheiro da
instância existe (o caso de uma instalação local), essa empresa herda a chave
da instância — os auditores que já a fixaram continuam a reconhecê-la. Com
várias empresas (SaaS) as chaves nascem no primeiro uso de cada uma, e o
auditor verá uma chave nova por cliente, o que é o objetivo.

Sem downgrade: apagar as colunas seria deitar fora chaves já apresentadas a
terceiros.

Revision ID: 029_dossie_chave_empresa
Revises: 028_formacao_nome_cifrado
Create Date: 2026-09-12
"""
import json
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "029_dossie_chave_empresa"
down_revision: Union[str, None] = "028_formacao_nome_cifrado"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHAVE_INSTANCIA = Path("/app/data/dossie-assinatura.json")


def _tem_coluna(inspector, tabela: str, coluna: str) -> bool:
    return coluna in {c["name"] for c in inspector.get_columns(tabela)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _tem_coluna(inspector, "empresas", "dossie_chave_priv"):
        op.add_column("empresas", sa.Column("dossie_chave_priv", sa.Text(), nullable=True))
    if not _tem_coluna(inspector, "empresas", "dossie_chave_pub"):
        op.add_column("empresas", sa.Column("dossie_chave_pub", sa.String(length=64), nullable=True))
    if not _tem_coluna(inspector, "empresas", "dossie_chave_criada_em"):
        # Sem fuso, como os outros instantes de `empresas` que o modelo declara —
        # o gate de equivalência de esquema apanhou a divergência.
        op.add_column("empresas", sa.Column("dossie_chave_criada_em", sa.DateTime(), nullable=True))

    if not _CHAVE_INSTANCIA.exists():
        return
    ligacao = op.get_bind()
    empresas = ligacao.execute(
        sa.text("SELECT id FROM empresas WHERE deleted_at IS NULL AND dossie_chave_pub IS NULL")
    ).fetchall()
    if len(empresas) != 1:
        return
    try:
        dados = json.loads(_CHAVE_INSTANCIA.read_text(encoding="utf-8"))
        priv, pub = dados["priv"], dados["pub"]
    except (OSError, ValueError, KeyError):
        return
    from app.shared.pii import cifrar_pii

    ligacao.execute(
        sa.text(
            "UPDATE empresas SET dossie_chave_priv = :priv, dossie_chave_pub = :pub, "
            "dossie_chave_criada_em = :em WHERE id = :id"
        ),
        {
            "priv": cifrar_pii(priv),
            "pub": pub,
            "em": dados.get("criado_em"),
            "id": empresas[0][0],
        },
    )


def downgrade() -> None:
    # Sem downgrade: as chaves já foram apresentadas a auditores.
    pass
