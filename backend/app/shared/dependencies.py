"""
Dependências FastAPI partilhadas: get_session, get_current_user, require_role.
Todas as rotas autenticadas devem usar estas dependências.
"""
import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlmodel import Session, select

from app.config import get_settings
from app.database import get_session

settings = get_settings()

# ---------------------------------------------------------------------------
# Dependência de base de dados
# ---------------------------------------------------------------------------

# Alias tipado para injeção limpa nos routers
SessionDep = Annotated[Session, Depends(get_session)]

# ---------------------------------------------------------------------------
# Extração do token JWT do header Authorization: Bearer <token>
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=True)


def _extrair_payload_jwt(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """
    Extrai e valida o JWT do header Authorization.
    Lança 401 se o token for inválido, expirado ou de tipo errado.
    NÃO aceita tokens de tipo "2fa_pending" ou "2fa_setup_required".
    """
    token = credentials.credentials
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido ou expirado.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Garante que não é um token temporário de 2FA
    tipo = payload.get("type", "")
    if tipo not in ("access",):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido para este endpoint.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload


# ---------------------------------------------------------------------------
# Dependência principal: utilizador atual autenticado
# ---------------------------------------------------------------------------

def get_current_user(
    db: SessionDep,
    payload: dict = Depends(_extrair_payload_jwt),
) -> "Utilizador":  # type: ignore[name-defined]
    """
    Resolve o utilizador autenticado a partir do JWT.

    Verifica:
    - Token válido e tipo "access"
    - Utilizador existe na DB
    - Utilizador está ativo
    - empresa_id no token corresponde ao da DB (proteção extra)

    Returns:
        Instância de Utilizador com dados frescos da DB.
    """
    # Import local para evitar circular imports
    from app.auth.models import Utilizador

    utilizador_id_str = payload.get("user_id")
    if not utilizador_id_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido.",
        )

    try:
        utilizador_id = uuid.UUID(utilizador_id_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido.",
        )

    utilizador = db.exec(
        select(Utilizador).where(Utilizador.id == utilizador_id)
    ).first()

    if not utilizador:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Utilizador não encontrado.",
        )

    if not utilizador.ativo:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Conta desativada. Contacte o administrador.",
        )

    # Verifica soft delete
    if utilizador.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Conta removida.",
        )

    return utilizador


# Alias tipado para uso nos routers
CurrentUserDep = Annotated["Utilizador", Depends(get_current_user)]  # type: ignore[name-defined]


# ---------------------------------------------------------------------------
# Dependência de RBAC: require_role
# ---------------------------------------------------------------------------

def require_role(*roles: str):
    """
    Factory de dependência que verifica se o utilizador tem um dos roles indicados.

    Uso nos routers:
        @router.get("/admin", dependencies=[Depends(require_role("admin"))])
        async def rota_admin(...):

    Ou como parâmetro tipado:
        async def rota(utilizador = Depends(require_role("admin", "auditor"))):
    """
    def verificador(
        utilizador: "Utilizador" = Depends(get_current_user),  # type: ignore[name-defined]
    ) -> "Utilizador":  # type: ignore[name-defined]
        if utilizador.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Sem permissão para realizar esta ação.",
            )
        return utilizador

    return verificador


def get_empresa_ativa(
    db: SessionDep,
    utilizador: CurrentUserDep,
) -> "Empresa":  # type: ignore[name-defined]
    """Resolve a empresa ativa do utilizador autenticado."""
    from app.empresas.models import Empresa

    empresa = db.get(Empresa, utilizador.empresa_id)
    if (
        not empresa
        or not empresa.ativo
        or getattr(empresa, "deleted_at", None) is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empresa não encontrada.",
        )
    return empresa


EmpresaAtivaDep = Annotated["Empresa", Depends(get_empresa_ativa)]  # type: ignore[name-defined]


