"""
Selagem de envelope (custódia de dados) do open-core.

Antes de qualquer payload de cliente sair do core para o sidecar premium, é selado
em envelope com a chave PÚBLICA X25519 (sealed box) do gateway. Só o worker do
gateway, que detém a chave privada, consegue abrir — o sidecar (partilhado no SaaS)
e o ingress recebem apenas ciphertext. Isto é custódia de dados, não lógica premium:
proteger os dados do cliente ao saírem, não decidir nada sobre a análise.

Fail-closed: sem chave pública o core RECUSA selar, exceto se
PREMIUM_ENVELOPE_DEV_PLAINTEXT=true (apenas desenvolvimento local).

O envelope nunca passa de PREMIUM_ENVELOPE_MAX_BYTES: quem monta o payload pede
antes a `maximo_em_claro()` quanto lhe cabe.
"""
from __future__ import annotations

import base64
import json

from app.config import get_settings

settings = get_settings()

# O sealed box acrescenta a pública efémera (32 bytes) e o MAC (16).
SELO_BYTES = 48


class CifraPorConfigurarError(RuntimeError):
    """Falta a chave pública do gateway: a instalação não pode pedir análises."""


class EnvelopeGrandeDemaisError(RuntimeError):
    """O envelope passaria do teto: quem montou o payload não o respeitou."""


def _cabeca() -> bytes:
    kid = json.dumps(settings.PREMIUM_ENVELOPE_KID or "").encode("ascii")
    return b'{"_nev":1,"kid":' + kid + b',"ct":"'


_CAUDA = b'"}'


def maximo_em_claro() -> int:
    """Quantos bytes de payload em claro cabem num envelope de
    PREMIUM_ENVELOPE_MAX_BYTES. O envelope é `{"_nev":1,"kid":"…","ct":"…"}`,
    com o texto cifrado em base64 (4 bytes por cada 3)."""
    teto = settings.PREMIUM_ENVELOPE_MAX_BYTES
    if not settings.PREMIUM_ENVELOPE_PUBKEY:
        # Sem chave só se chega a selar em desenvolvimento, e aí vai o próprio payload.
        return teto
    base64_max = teto - len(_cabeca()) - len(_CAUDA)
    return max(0, (base64_max // 4) * 3 - SELO_BYTES)


def cifrar_envelope(plaintext: bytes) -> bytes:
    """
    Sela `plaintext` com a chave pública do gateway e devolve um envelope etiquetado
    `{"_nev":1,"kid","ct"}` (o `kid` permite rotação de chaves sem mismatch).

    Sem chave pública configurada: levanta CifraPorConfigurarError (fail-closed), exceto quando
    PREMIUM_ENVELOPE_DEV_PLAINTEXT=true — nunca degrada em silêncio.
    """
    if len(plaintext) > maximo_em_claro():
        raise EnvelopeGrandeDemaisError(
            f"payload de {len(plaintext)} bytes acima do teto do envelope ({maximo_em_claro()})"
        )
    pubkey = settings.PREMIUM_ENVELOPE_PUBKEY
    if not pubkey:
        if settings.PREMIUM_ENVELOPE_DEV_PLAINTEXT:
            return plaintext
        raise CifraPorConfigurarError(
            "cifra-envelope: PREMIUM_ENVELOPE_PUBKEY ausente (fail-closed). "
            "Gere a chave (docker/gen-secrets.sh) ou defina "
            "PREMIUM_ENVELOPE_DEV_PLAINTEXT=true em dev."
        )

    from nacl.public import PublicKey, SealedBox

    ct = SealedBox(PublicKey(base64.b64decode(pubkey))).encrypt(plaintext)
    # O envelope monta-se já em bytes, sem passar por um dicionário e por um
    # texto: cada cópia intermédia era mais uma vez o tamanho das evidências.
    ct_base64 = base64.b64encode(ct)
    del ct
    return b"".join((_cabeca(), ct_base64, _CAUDA))
