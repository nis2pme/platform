"""
Configuração da base de dados: engine SQLAlchemy + sessão SQLModel.
Importar `get_session` como dependência nos routers FastAPI.
"""
import logging
from collections.abc import Generator

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from sqlmodel import Session, create_engine

from app.config import get_settings

logger = logging.getLogger(__name__)


def _criar_engine():
    """Cria o engine SQLAlchemy a partir da DATABASE_URL nas settings."""
    settings = get_settings()
    # SQL_ECHO controlado por variável separada de DEBUG (CWE-532).
    # Em prod deve ser False mesmo que DEBUG fique True acidentalmente.
    return create_engine(
        settings.DATABASE_URL,
        echo=settings.SQL_ECHO,
        pool_pre_ping=True,  # testa conexão antes de usar do pool
        pool_size=10,
        max_overflow=20,
    )


engine = _criar_engine()


def get_session() -> Generator[Session, None, None]:
    """
    Dependência FastAPI que fornece uma sessão de base de dados.
    Garante commit automático em sucesso e rollback em exceção.

    Uso nos routers:
        db: Session = Depends(get_session)
    """
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except (HTTPException, RequestValidationError):
            # HTTPException e RequestValidationError são controlo de fluxo
            # normal do FastAPI (4xx), não erro interno de base de dados.
            # Faz rollback silencioso para evitar ruído no terminal.
            session.rollback()
            raise
        except Exception:
            logger.exception("Transação de BD falhou — rollback executado")
            session.rollback()
            raise
