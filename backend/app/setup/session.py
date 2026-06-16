"""
Sessão de setup efémera (on-prem).

Garante que apenas **um** cliente faz o setup inicial de cada vez: o primeiro a
iniciar recebe um cookie httpOnly; os restantes vêem "ocupado". O Lock serializa
o claim e a verificação, resolvendo também a corrida ao primeiro admin (TOCTOU).

Estado em memória (1 worker uvicorn). Reiniciar o container limpa a sessão e
reabre o setup — aceitável, dado tratar-se de uma janela de instalação curta.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.shared.utils import pedido_e_seguro

# Cookie — mesmo padrão do refresh (app.auth.cookies): prefixo/Secure derivados do
# esquema real do pedido; Path restrito aos endpoints de setup (CWE-614).
_COOKIE_PATH = "/api/setup"
_COOKIE = "setup_session"
_COOKIE_SECURE = "__Secure-setup_session"

_TTL = timedelta(minutes=30)

_lock = threading.Lock()


@dataclass
class _Sessao:
    token: str
    expira_em: datetime


_sessao: _Sessao | None = None


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def reclamar(cookie_token: str | None) -> tuple[str | None, bool]:
    """Reclama (ou renova) a sessão de setup.

    Devolve (token, ocupado):
      - livre/expirada, ou o cookie pertence à sessão atual → (token, False)
      - sessão ativa de outro cliente → (None, True)
    """
    global _sessao
    with _lock:
        if _sessao is not None and _sessao.expira_em > _agora():
            if cookie_token and secrets.compare_digest(cookie_token, _sessao.token):
                return _sessao.token, False
            return None, True
        token = secrets.token_urlsafe(32)
        _sessao = _Sessao(token=token, expira_em=_agora() + _TTL)
        return token, False


def validar(cookie_token: str | None) -> bool:
    """True se o cookie corresponde à sessão ativa e não expirada."""
    with _lock:
        if _sessao is None or _sessao.expira_em <= _agora():
            return False
        return bool(cookie_token) and secrets.compare_digest(cookie_token, _sessao.token)


def limpar() -> None:
    """Esquece a sessão (setup concluído)."""
    global _sessao
    with _lock:
        _sessao = None


# --- Cookie helpers (espelham app.auth.cookies) ---------------------------------

def ler_cookie(request: Any) -> str | None:
    return request.cookies.get(_COOKIE_SECURE) or request.cookies.get(_COOKIE)


def definir_cookie(response: Any, request: Any, token: str) -> None:
    seguro = pedido_e_seguro(request)
    nome = _COOKIE_SECURE if seguro else _COOKIE
    response.set_cookie(
        nome,
        token,
        httponly=True,
        secure=seguro,
        samesite="lax",
        max_age=int(_TTL.total_seconds()),
        path=_COOKIE_PATH,
    )


def limpar_cookie(response: Any) -> None:
    response.delete_cookie(_COOKIE, path=_COOKIE_PATH)
    response.delete_cookie(
        _COOKIE_SECURE, path=_COOKIE_PATH, secure=True, httponly=True, samesite="lax"
    )
