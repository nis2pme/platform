"""
Chaves Fernet da instalação, com rotação.

Há três: a dos dados pessoais (`PII_ENCRYPTION_KEY`), a das evidências
(`EVIDENCE_ENCRYPTION_KEY`) e a do segredo do 2FA (`TOTP_ENCRYPTION_KEY`). Cada uma
pode ter uma chave anterior em `<NOME>_PREV`. Com ela definida, decifra-se com a
atual ou com a anterior e cifra-se sempre com a atual — é o `MultiFernet`.

A anterior só existe durante uma rotação. O comando `python -m
app.shared.rodar_chaves` guia os passos: pôr a chave atual como anterior e gerar
uma nova, voltar a cifrar tudo com a nova, confirmar que nada ficou na antiga e só
então retirá-la. Uma chave nunca muda em silêncio: um backup antigo restaura com as
chaves que levava, e os dados dele decifram com elas.
"""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, MultiFernet

NOMES = ("PII_ENCRYPTION_KEY", "EVIDENCE_ENCRYPTION_KEY", "TOTP_ENCRYPTION_KEY")


@lru_cache(maxsize=16)
def _multi(atual: str, anterior: str) -> MultiFernet:
    chaves = [Fernet(atual.encode())]
    if anterior and anterior != atual:
        chaves.append(Fernet(anterior.encode()))
    return MultiFernet(chaves)


def fernet(nome: str) -> MultiFernet | None:
    """A cifra da chave `nome`, com a anterior se houver. None sem chave definida."""
    from app.config import get_settings

    settings = get_settings()
    atual = getattr(settings, nome)
    if not atual:
        return None
    return _multi(atual, getattr(settings, f"{nome}_PREV", "") or "")


def fernet_obrigatorio(nome: str) -> MultiFernet:
    """Como `fernet`, mas sem chave levanta — para quem não tem alternativa."""
    cifra = fernet(nome)
    if cifra is None:
        raise RuntimeError(f"{nome} não está definida.")
    return cifra
