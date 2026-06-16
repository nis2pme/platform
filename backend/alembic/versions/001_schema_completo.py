"""Schema completo — migração única para instalações novas.

Esta é a migração de nível 0 do docker/on-prem.
Cria todas as tabelas e tipos a partir dos modelos SQLModel actuais.
Não tem histórico de migrações anteriores — apenas para instalações frescas.

Revision ID: 001_schema_completo
Revises: -
Create Date: 2026-05-23
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect

revision: str = "001_schema_completo"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Cria todo o schema a partir dos modelos SQLModel actuais.
    Idempotente: ignora tabelas que já existam (checkfirst=True).
    """
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = set(inspector.get_table_names())

    # Importar todos os módulos de modelos para registar os metadados SQLModel
    import app.auth.models  # noqa: F401
    import app.empresas.models  # noqa: F401
    import app.controlos.models  # noqa: F401
    import app.evidencias.models  # noqa: F401
    import app.frameworks.models  # noqa: F401
    import app.notificacoes.models  # noqa: F401
    import app.plano_prioritario.models  # noqa: F401

    from sqlmodel import SQLModel
    SQLModel.metadata.create_all(bind, checkfirst=True)


def downgrade() -> None:
    """Elimina todas as tabelas geridas pelo SQLModel (CUIDADO — apaga todos os dados)."""
    bind = op.get_bind()

    import app.auth.models  # noqa: F401
    import app.empresas.models  # noqa: F401
    import app.controlos.models  # noqa: F401
    import app.evidencias.models  # noqa: F401
    import app.frameworks.models  # noqa: F401
    import app.notificacoes.models  # noqa: F401
    import app.plano_prioritario.models  # noqa: F401

    from sqlmodel import SQLModel
    SQLModel.metadata.drop_all(bind)
