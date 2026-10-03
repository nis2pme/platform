"""
Dependências FastAPI partilhadas: get_session, get_current_user, require_role.
Todas as rotas autenticadas devem usar estas dependências.
"""
import uuid
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlmodel import Session, select

from app.config import get_settings
from app.database import get_session

if TYPE_CHECKING:  # só para as anotações — em execução importam-se dentro das funções
    from app.auth.models import Utilizador
    from app.empresas.models import Empresa

settings = get_settings()

# ---------------------------------------------------------------------------
# Dependência de base de dados
# ---------------------------------------------------------------------------

# Alias tipado para injeção limpa nos routers.
#
# `scope="function"`: o commit do `get_session` corre ANTES de a resposta sair.
# Com o âmbito por omissão ("request") corria depois — o cliente recebia 200
# antes de a escrita estar na base, o pedido seguinte podia ainda não a ver, e
# um commit que falhasse perdia-se depois de já ter dito "feito". Todos os
# `Depends(get_session)` têm de ter o mesmo âmbito, para o pedido partilhar a
# mesma sessão.
SessionDep = Annotated[Session, Depends(get_session, scope="function")]

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
    return payload_de_access_token(credentials.credentials)


def payload_de_access_token(token: str) -> dict:
    """
    Valida um access token (assinatura, prazo e tipo) e devolve o payload.

    Separada da dependência para a guarda dos uploads (`shared/multipart.py`)
    decidir com exatamente as mesmas regras, antes de o corpo ser lido.
    """
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


def access_token_so_expirado(token: str) -> bool:
    """Um access token assinado por nós que só falha por ter expirado.

    Chamada depois de `payload_de_access_token` o recusar: com a assinatura e o
    tipo certos, o que falhou foi o prazo. É quem tem sessão e vai renová-la."""
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": False},
        )
    except JWTError:
        return False
    return payload.get("type") == "access"


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

    # O token diz a que empresa pertencia quem o recebeu; se a conta entretanto
    # mudou de empresa (ou o token foi forjado com outra), a sessão antiga não
    # vale. É a verificação que a docstring sempre prometeu.
    empresa_no_token = payload.get("empresa_id")
    if empresa_no_token is not None and str(empresa_no_token) != str(utilizador.empresa_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido.",
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

    # A suspensão da empresa vale para TODAS as rotas, não só para o login. Sem
    # esta verificação, quem já tinha sessão continuava a trabalhar — e o
    # refresh renovava-a — depois de a empresa ter sido suspensa. Uma leitura
    # por chave primária a mais em cada pedido é o preço de a suspensão ser
    # uma suspensão.
    from app.empresas.models import Empresa

    empresa = db.get(Empresa, utilizador.empresa_id)
    # Uma empresa apagada (soft delete) conta como suspensa: sem isto, quem já
    # tinha sessão continuava a usar a plataforma — e os módulos pagos.
    if empresa is not None and (empresa.suspenso or empresa.deleted_at is not None):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"codigo": "empresa_suspensa"},
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
        request: Request,
        utilizador: "Utilizador" = Depends(get_current_user),  # type: ignore[name-defined]
    ) -> "Utilizador":  # type: ignore[name-defined]
        if utilizador.role not in roles:
            # Import local: `audit` não pode ser importado no topo deste módulo
            # sem fechar um ciclo (audit → pii → dependencies).
            from app.shared.audit import registar_negacao

            registar_negacao(
                utilizador, modulo="papel", acao="|".join(roles), request=request
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Sem permissão para realizar esta ação.",
            )
        return utilizador

    # Marca lida por `app/shared/gates.py` — ver a nota em `require_capability`.
    # Este gate também autoriza, embora fora da matriz: são os poucos sítios em
    # que a decisão é sobre o papel bruto (2FA e o arranque da instalação).
    verificador._gate = ("papel", roles)
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


