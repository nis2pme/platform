"""Gestão do cookie de refresh token (httpOnly).

O atributo `Secure` e o prefixo do nome são derivados do esquema real do pedido
(via X-Forwarded-Proto do Nginx), não de uma string de configuração — para que o
cookie fique Secure exactamente quando a ligação é HTTPS, sem reinício e
independentemente de o endereço ser um IP ou um domínio (CWE-614 / CWE-1004).

Prefixo: usa-se `__Secure-` (e não `__Host-`) porque preserva o `Path` restrito
aos endpoints de auth; `__Host-` exigiria `Path=/`, alargando a exposição do cookie.
Durante a transição HTTP→HTTPS (após o setup) ambas as variantes são geridas para
não deixar cookies órfãos.
"""
from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.shared.utils import pedido_e_seguro

# Caminho restrito aos endpoints de auth (CWE-614). Unificado entre login e setup.
REFRESH_COOKIE_PATH = "/api/auth"
# Nome base (ligação HTTP, ex.: fase de setup) e variante endurecida (HTTPS).
REFRESH_COOKIE = "refresh_token"
REFRESH_COOKIE_SECURE = "__Secure-refresh_token"


def _max_age() -> int:
    return get_settings().JWT_REFRESH_TOKEN_EXPIRE_DAYS * 86400


def definir_cookie_refresh(response: Any, request: Any, token: str) -> None:
    """Define o cookie de refresh; Secure e prefixo conforme o pedido seja HTTPS."""
    seguro = pedido_e_seguro(request)
    nome = REFRESH_COOKIE_SECURE if seguro else REFRESH_COOKIE
    response.set_cookie(
        nome,
        token,
        httponly=True,
        secure=seguro,
        samesite="lax",
        max_age=_max_age(),
        path=REFRESH_COOKIE_PATH,
    )
    if seguro:
        # Remove a variante não-segura deixada por uma sessão HTTP anterior
        # (transição HTTP→HTTPS após o setup), evitando cookies órfãos.
        response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)


def limpar_cookie_refresh(response: Any) -> None:
    """Remove ambas as variantes do cookie (logout)."""
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)
    response.delete_cookie(
        REFRESH_COOKIE_SECURE,
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )


def obter_token_refresh(request: Any) -> str | None:
    """Lê o refresh token do cookie, preferindo a variante segura (__Secure-)."""
    return request.cookies.get(REFRESH_COOKIE_SECURE) or request.cookies.get(REFRESH_COOKIE)
