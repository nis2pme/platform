"""
Ambiente Alembic — importa todos os modelos SQLModel antes de correr migrações
para que o autogenerate funcione correctamente.
"""
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool
from sqlmodel import SQLModel

# Adiciona o directório backend/ ao path para importar a app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Carregar .env manualmente (antes de importar app.config para garantir que as
# variáveis estão disponíveis mesmo quando corrido directamente via alembic CLI)
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            os.environ.setdefault(_key.strip(), _val.strip())

# Importar TODOS os modelos — necessário para que SQLModel.metadata os inclua
from app.empresas.models import Empresa  # noqa: F401
from app.auth.models import (  # noqa: F401
    Utilizador,
    TokenRefresh,
    PasswordResetToken,
    CodigoBackup2FA,
)
from app.shared.audit import AuditLog  # noqa: F401

# Importar modelos de controlos e evidências
from app.controlos.models import (  # noqa: F401
    HistoricoMaturidade,
    RelatorioAuditoria,
)
from app.evidencias.models import Evidencia  # noqa: F401

# Novos modelos de frameworks (nova estrutura)
from app.frameworks.models import (  # noqa: F401
    Framework,
    FrameworkLocale,
    Domain,
    DomainLocale,
    Subdomain,
    SubdomainLocale,
    Control,
    ControlLocale,
    SubRequirement,
    SubRequirementLocale,
    MaturityLevel,
    MaturityLevelLocale,
    ComplianceProfile,
    ProfileLocale,
    ProfileThreshold,
    ControloEmpresaV2,
    ControloEmpresaCheckV2,
)

# Configuração Alembic
config = context.config

# Obter DATABASE_URL — env var tem prioridade sobre alembic.ini
_database_url = os.environ.get("DATABASE_URL")
if not _database_url:
    raise RuntimeError(
        "DATABASE_URL não definida. Verifica o ficheiro backend/.env"
    )
config.set_main_option("sqlalchemy.url", _database_url)

# Logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata com todos os modelos registados
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Corre migrações em modo offline (sem conexão à DB)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Corre migrações em modo online (com conexão ativa à DB)."""
    url = config.get_main_option("sqlalchemy.url")
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
