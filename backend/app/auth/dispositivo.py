"""Cookie de dispositivo — a técnica OWASP «Slow Down Online Guessing Attacks
with Device Cookies».

Depois de um login completo (com 2FA), o servidor põe um cookie assinado, preso
ao utilizador. Quem volta com um cookie válido para ESSE utilizador:
  - não conta nos limites por endereço nem por grupo (é reconhecido, não é volume);
  - tem uma vaga de argon2 reservada (entra mesmo com a fila dos desconhecidos cheia);
  - tem o SEU próprio contador de falhas — ao fim de N falhas o cookie deixa de
    valer, em vez do bloqueio da conta que um atacante consegue provocar.

O cookie é sem estado no servidor (a validade está no próprio valor, assinada), a
não ser o contador de falhas, que vive na tabela de bloqueios já existente. A
chave de assinatura deriva-se do JWT_SECRET_KEY (HKDF, rótulo próprio): não é um
segredo novo, por isso não muda a lista fechada de segredos da instalação.

O cookie NÃO substitui a password nem o 2FA: só dispensa a fricção anti-abuso a
quem já se autenticou por completo neste dispositivo. Roubá-lo sem a password não
serve para entrar — só valeria como um dos dispositivos reconhecidos dessa conta.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from app.config import get_settings

# Rótulo de derivação: separa esta chave de qualquer outro uso do JWT_SECRET_KEY.
_INFO = b"nis2pme-cookie-dispositivo-v1"
_VERSAO = "v1"

# Nome do cookie: variante endurecida em HTTPS, base em HTTP (fase de setup).
COOKIE_DISPOSITIVO = "device_id"
COOKIE_DISPOSITIVO_SECURE = "__Secure-device_id"
# Caminho de auth: o cookie só viaja para as rotas de autenticação.
COOKIE_DISPOSITIVO_PATH = "/api/auth"


def _b64(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).decode().rstrip("=")


def _chave() -> bytes:
    """Chave de assinatura derivada do JWT_SECRET_KEY por HKDF-SHA256.

    Fica em cache no processo; se o segredo mudar (rotação, reinício), a derivação
    corre de novo no arranque seguinte e os cookies antigos deixam de validar — o
    que só custa a quem os tinha um novo login.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    segredo = get_settings().JWT_SECRET_KEY.encode()
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO)
    return hkdf.derive(segredo)


def _mac(corpo: str) -> str:
    return _b64(hmac.new(_chave(), corpo.encode(), hashlib.sha256).digest())


def emitir(user_id: uuid.UUID) -> str:
    """Cria o valor do cookie para `user_id`, válido por DISPOSITIVO_COOKIE_DIAS."""
    nonce = secrets.token_urlsafe(12)
    expira = int(
        (datetime.now(timezone.utc)
         + timedelta(days=get_settings().DISPOSITIVO_COOKIE_DIAS)).timestamp()
    )
    corpo = f"{_VERSAO}.{user_id.hex}.{nonce}.{expira}"
    return f"{corpo}.{_mac(corpo)}"


def ler(cookie: str | None) -> tuple[str, str] | None:
    """Devolve (user_id_hex, nonce) se o cookie é autêntico e não expirou, senão
    None. Não confere a que utilizador pertence — quem quer isso usa `validar`."""
    if not cookie:
        return None
    partes = cookie.split(".")
    if len(partes) != 5:
        return None
    versao, uid_hex, nonce, expira_txt, mac = partes
    if versao != _VERSAO:
        return None
    corpo = f"{versao}.{uid_hex}.{nonce}.{expira_txt}"
    if not hmac.compare_digest(mac, _mac(corpo)):
        return None
    try:
        expira = int(expira_txt)
        uuid.UUID(hex=uid_hex)
    except (ValueError, TypeError):
        return None
    if datetime.now(timezone.utc).timestamp() >= expira:
        return None
    return uid_hex, nonce


def validar(cookie: str | None, user_id: uuid.UUID) -> str | None:
    """Devolve o `nonce` se o cookie é válido E é deste utilizador; senão None."""
    lido = ler(cookie)
    if lido is None:
        return None
    uid_hex, nonce = lido
    if not hmac.compare_digest(uid_hex, user_id.hex):
        return None
    return nonce


# ---------------------------------------------------------------------------
# Contador de falhas por cookie — reutiliza a tabela de bloqueios por IP, com
# uma chave própria (nunca colide com um hash de IP).
# ---------------------------------------------------------------------------

def _chave_nonce(nonce: str) -> str:
    from app.shared.hashes import hash_ip_bloqueio

    return hash_ip_bloqueio(f"disp:{nonce}")


def cookie_travado(db, nonce: str, agora: datetime) -> bool:
    """True se este cookie já acumulou falhas a mais e deixou de valer."""
    from sqlmodel import select

    from app.auth.models import BloqueioIP

    reg = db.exec(select(BloqueioIP).where(BloqueioIP.ip_hash == _chave_nonce(nonce))).first()
    if reg is None or reg.bloqueado_ate is None:
        return False
    ate = reg.bloqueado_ate
    if ate.tzinfo is None:
        ate = ate.replace(tzinfo=timezone.utc)
    return ate > agora


def registar_falha_cookie(db, nonce: str, agora: datetime) -> None:
    """Conta uma falha deste cookie numa janela deslizante; ao atingir o limiar,
    o cookie deixa de valer (volta a valer só um login completo, que emite outro).

    Commit próprio: o chamador lança HTTPException a seguir, e o rollback da
    transação principal desfaria o contador (como no anti-spray por IP)."""
    from sqlmodel import select

    from app.auth.models import BloqueioIP

    s = get_settings()
    chave = _chave_nonce(nonce)
    reg = db.exec(select(BloqueioIP).where(BloqueioIP.ip_hash == chave)).first()
    if reg is None:
        db.add(BloqueioIP(ip_hash=chave, contador=1, janela_inicio=agora, atualizado_em=agora))
        db.commit()
        return
    janela = reg.janela_inicio
    if janela is not None and janela.tzinfo is None:
        janela = janela.replace(tzinfo=timezone.utc)
    if janela is None or (agora - janela) > timedelta(minutes=s.LOGIN_IP_JANELA_MINUTOS):
        reg.contador = 1
        reg.janela_inicio = agora
        reg.bloqueado_ate = None
    else:
        reg.contador = (reg.contador or 0) + 1
    reg.atualizado_em = agora
    if reg.contador >= s.DISPOSITIVO_MAX_FALHAS:
        # Trava por toda a vida restante do cookie: na prática mata-o.
        reg.bloqueado_ate = agora + timedelta(days=s.DISPOSITIVO_COOKIE_DIAS)
    db.add(reg)
    db.commit()


# ---------------------------------------------------------------------------
# Pôr / ler / limpar o cookie na resposta (Secure e prefixo conforme o pedido).
# ---------------------------------------------------------------------------

def definir(response, request, user_id: uuid.UUID) -> None:
    """Emite e põe o cookie de dispositivo (HttpOnly, SameSite=Strict, caminho de
    auth; Secure quando a ligação é HTTPS)."""
    from app.shared.utils import pedido_e_seguro

    seguro = pedido_e_seguro(request)
    nome = COOKIE_DISPOSITIVO_SECURE if seguro else COOKIE_DISPOSITIVO
    response.set_cookie(
        nome,
        emitir(user_id),
        httponly=True,
        secure=seguro,
        samesite="strict",
        max_age=get_settings().DISPOSITIVO_COOKIE_DIAS * 86400,
        path=COOKIE_DISPOSITIVO_PATH,
    )
    if seguro:
        response.delete_cookie(COOKIE_DISPOSITIVO, path=COOKIE_DISPOSITIVO_PATH)


def obter(request) -> str | None:
    """Lê o cookie de dispositivo (prefere a variante segura)."""
    return (
        request.cookies.get(COOKIE_DISPOSITIVO_SECURE)
        or request.cookies.get(COOKIE_DISPOSITIVO)
    )
