"""
Selagem de envelope (custódia de dados) do open-core.

Antes de qualquer payload de cliente sair do core para o sidecar premium, é selado
em envelope com a chave PÚBLICA X25519 (sealed box) do gateway. Só o worker do
gateway, que detém a chave privada, consegue abrir — o sidecar (partilhado no SaaS)
e o ingress recebem apenas ciphertext. Isto é custódia de dados, não lógica premium:
proteger os dados do cliente ao saírem, não decidir nada sobre a análise.

Fail-closed: sem chave pública o core RECUSA selar, exceto se
PREMIUM_ENVELOPE_DEV_PLAINTEXT=true (apenas desenvolvimento local).
"""
from __future__ import annotations

import base64
import json

from app.config import get_settings

settings = get_settings()


def cifrar_envelope(plaintext: bytes) -> bytes:
    """
    Sela `plaintext` com a chave pública do gateway e devolve um envelope etiquetado
    `{"_nev":1,"kid","ct"}` (o `kid` permite rotação de chaves sem mismatch).

    Sem chave pública configurada: levanta RuntimeError (fail-closed), exceto quando
    PREMIUM_ENVELOPE_DEV_PLAINTEXT=true — nunca degrada em silêncio.
    """
    pubkey = settings.PREMIUM_ENVELOPE_PUBKEY
    if not pubkey:
        if settings.PREMIUM_ENVELOPE_DEV_PLAINTEXT:
            return plaintext
        raise RuntimeError(
            "cifra-envelope: PREMIUM_ENVELOPE_PUBKEY ausente (fail-closed). "
            "Gere a chave (docker/gen-secrets.sh) ou defina "
            "PREMIUM_ENVELOPE_DEV_PLAINTEXT=true em dev."
        )

    from nacl.public import PublicKey, SealedBox

    ct = SealedBox(PublicKey(base64.b64decode(pubkey))).encrypt(plaintext)
    envelope = {
        "_nev": 1,
        "kid": settings.PREMIUM_ENVELOPE_KID or "",
        "ct": base64.b64encode(ct).decode("ascii"),
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")
