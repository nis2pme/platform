"""Prova de trabalho só sob carga (ao estilo do ALTCHA, mas nossa e pequena).

Quando a fila de argon2 dos desconhecidos aperta, ou um endereço/grupo acumula
falhas, o login passa a exigir a solução de um desafio antes de gastar um argon2.
Sem carga, é invisível: quem chega quando há folga não vê nada disto.

Como funciona (sem estado no servidor, a não ser o uso único):
  - o servidor escolhe um número secreto entre 0 e `dificuldade`, calcula
    `sha256(sal + numero)` e assina (HMAC) o conjunto {sal, dificuldade, hash,
    validade, grupo de origem};
  - o browser, num Web Worker, tenta 0, 1, 2, … até `sha256(sal + n)` bater com o
    hash, e devolve esse `n`;
  - o servidor confere a assinatura, a validade, o grupo, que `sha256(sal + n)`
    dá o hash, e que o sal ainda não foi usado.

O custo médio para quem resolve é ~metade de `dificuldade` hashes SHA-256; a
dificuldade sobe com a carga. A chave de assinatura deriva-se do JWT_SECRET_KEY
(HKDF, rótulo próprio) — não é um segredo novo.

Limite honesto: o SHA-256 acelera-se com código nativo e com GPU, por isso a prova
encarece um ataque em volume mas não o impede a quem tenha esses meios. O que
garante a entrada de quem já cá esteve é o cookie de dispositivo (camada 2); a
prova de trabalho é o atrito que se liga só quando é preciso.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
from datetime import datetime, timezone

from app.config import get_settings

_INFO = b"nis2pme-desafio-pow-v1"
_VERSAO = "v1"

# Uso único: sais já gastos, com o instante em que expiram (para os limpar).
_usados: dict[str, float] = {}
_trinco = threading.Lock()


def _b64(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).decode().rstrip("=")


def _chave() -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    segredo = get_settings().JWT_SECRET_KEY.encode()
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO)
    return hkdf.derive(segredo)


def _assinar(sal: str, dificuldade: int, desafio: str, expira: int, grupo: str) -> str:
    corpo = f"{_VERSAO}.{sal}.{dificuldade}.{desafio}.{expira}.{grupo}"
    return _b64(hmac.new(_chave(), corpo.encode(), hashlib.sha256).digest())


def _hash(sal: str, numero: int) -> str:
    return hashlib.sha256(f"{sal}{numero}".encode()).hexdigest()


def emitir(grupo: str, dificuldade: int | None = None) -> dict:
    """Cria um desafio preso ao `grupo` de origem. Devolve o que o browser precisa
    de resolver — o número secreto NÃO viaja, só o hash dele."""
    s = get_settings()
    dificuldade = min(dificuldade or s.DESAFIO_DIFICULDADE_BASE, s.DESAFIO_DIFICULDADE_MAX)
    dificuldade = max(1, dificuldade)
    sal = secrets.token_urlsafe(12)
    numero = secrets.randbelow(dificuldade + 1)
    desafio = _hash(sal, numero)
    expira = int(datetime.now(timezone.utc).timestamp()) + s.DESAFIO_VALIDADE_S
    return {
        "sal": sal,
        "dificuldade": dificuldade,
        "desafio": desafio,
        "expira": expira,
        "assinatura": _assinar(sal, dificuldade, desafio, expira, grupo),
    }


def _limpar(agora: float) -> None:
    for sal, ate in list(_usados.items()):
        if ate < agora:
            _usados.pop(sal, None)


def verificar(dados: dict | None, grupo: str) -> bool:
    """True se a solução resolve um desafio autêntico, válido, deste grupo, e
    ainda não usado. Marca o sal como usado. Qualquer campo em falta → False."""
    if not isinstance(dados, dict):
        return False
    try:
        sal = str(dados["sal"])
        dificuldade = int(dados["dificuldade"])
        desafio = str(dados["desafio"])
        expira = int(dados["expira"])
        assinatura = str(dados["assinatura"])
        numero = int(dados["numero"])
    except (KeyError, TypeError, ValueError):
        return False

    esperado = _assinar(sal, dificuldade, desafio, expira, grupo)
    if not hmac.compare_digest(assinatura, esperado):
        return False
    agora = datetime.now(timezone.utc).timestamp()
    if agora >= expira:
        return False
    if not (0 <= numero <= dificuldade):
        return False
    if not hmac.compare_digest(_hash(sal, numero), desafio):
        return False

    # Uso único: o sal só serve uma vez, dentro da sua validade.
    with _trinco:
        _limpar(agora)
        if sal in _usados:
            return False
        _usados[sal] = float(expira)
    return True


def reset() -> None:
    """Limpa o estado de uso único (usado nos testes)."""
    with _trinco:
        _usados.clear()
