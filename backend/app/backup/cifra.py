"""
Cifra dos backups `.nbk`.

    linha 1: cabeçalho JSON (formato, modo, app_version, criado_em, recipiente, chave)
    resto:   age( tar.gz ) para uma identidade X25519, cifrado por partes

O payload nunca passa inteiro pela memória: o age cifra e decifra em blocos, e
cada bloco é autenticado — um byte trocado ou um ficheiro truncado é recusado.
Cifrar tudo em memória (o formato de pré-lançamento, 1) matava um backend de
1 GiB com 150 MB de evidências, e o backup agendado recomeçava a cada arranque
até encher o disco.

A identidade X25519 que abre os backups nunca fica no servidor em claro: vai
embrulhada pela frase-passe (scrypt + ChaCha20-Poly1305), no ficheiro de chave e
no cabeçalho de cada backup. Para CRIAR basta a parte pública — os backups
agendados correm sem a frase-passe, e quem copie o volume de dados não fica com
nada que abra os backups levados para fora.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import BinaryIO

import pyrage
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from app.shared.concorrencia import LIMITE_OPERACOES_PESADAS

FORMATO = 2

# Custo do scrypt que embrulha a identidade: 2^17 × r=8 = 128 MiB, ~0,3–0,6 s
# (medido a 2026-09-27: 0,32 s no servidor dev, 0,6 s num portátil). Fixo de
# propósito: o scrypt do `pyrage.passphrase` calibra-se pela velocidade do CPU e
# chegou a pedir 522 MiB — mais do que o backend de uma PME tem.
N_LOG2 = 17
# O que se aceita ao LER. O cabeçalho não é de confiança antes de decifrar, e um
# `n` enorme esgota a memória antes de qualquer verificação (medido: 2^22 mata o
# processo). O teto deixa margem a uma versão futura subir um degrau.
N_LOG2_MIN, N_LOG2_MAX = 17, 18
_R, _P = 8, 1
_AAD = b"nis2pme-backup-identidade-v2"


class ChaveErrada(Exception):
    """A frase-passe não abre a identidade (ou o embrulho foi trocado)."""


class Adulterado(Exception):
    """O ficheiro não decifra: corrompido, truncado ou alterado."""


def _b64(dados: bytes) -> str:
    return base64.b64encode(dados).decode("ascii")


def _derivar(passphrase: str, salt: bytes, n_log2: int) -> bytes:
    if not N_LOG2_MIN <= n_log2 <= N_LOG2_MAX:
        raise Adulterado(f"custo scrypt fora dos limites (2^{n_log2})")
    # 128 MiB por derivação: partilha a vaga das operações pesadas do núcleo.
    with LIMITE_OPERACOES_PESADAS.ocupar():
        return Scrypt(salt=salt, length=32, n=2**n_log2, r=_R, p=_P).derive(passphrase.encode("utf-8"))


def nova_chave(passphrase: str) -> dict:
    """Conteúdo do ficheiro de chave para uma frase-passe definida agora."""
    identidade = pyrage.x25519.Identity.generate()
    salt, nonce = os.urandom(16), os.urandom(12)
    chave = _derivar(passphrase, salt, N_LOG2)
    ct = ChaCha20Poly1305(chave).encrypt(nonce, str(identidade).encode("ascii"), _AAD)
    return {
        "versao": 2,
        "recipiente": str(identidade.to_public()),
        "embrulho": {"kdf": "scrypt", "n_log2": N_LOG2, "salt": _b64(salt), "nonce": _b64(nonce), "ct": _b64(ct)},
    }


def abrir_identidade(embrulho: dict, passphrase: str) -> pyrage.x25519.Identity:
    try:
        n_log2 = int(embrulho["n_log2"])
        salt, nonce, ct = (base64.b64decode(embrulho[k]) for k in ("salt", "nonce", "ct"))
    except (KeyError, TypeError, ValueError) as exc:
        raise Adulterado("embrulho da chave ilegível") from exc
    if embrulho.get("kdf") != "scrypt":
        raise Adulterado("embrulho da chave com KDF desconhecido")
    chave = _derivar(passphrase, salt, n_log2)
    try:
        texto = ChaCha20Poly1305(chave).decrypt(nonce, ct, _AAD)
    except InvalidTag as exc:
        raise ChaveErrada() from exc
    return pyrage.x25519.Identity.from_str(texto.decode("ascii"))


def cifrar(origem: Path, destino: BinaryIO, recipiente: str) -> None:
    """Cifra `origem` por partes para o recipiente, a seguir ao que já está em `destino`."""
    with origem.open("rb") as entrada:
        pyrage.encrypt_io(entrada, destino, [pyrage.x25519.Recipient.from_str(recipiente)])


def decifrar(origem: BinaryIO, destino: Path, identidade: pyrage.x25519.Identity) -> None:
    """Decifra por partes de `origem` (posicionado depois do cabeçalho) para `destino`.

    Os blocos já decifrados vão sendo escritos antes de o último ser verificado:
    numa falha o destino é apagado, e quem chama só usa o ficheiro depois de esta
    função voltar sem erro."""
    try:
        with destino.open("wb") as saida:
            pyrage.decrypt_io(origem, saida, [identidade])
    except Exception as exc:  # noqa: BLE001 — o pyrage levanta OSError ou DecryptError
        destino.unlink(missing_ok=True)
        raise Adulterado(str(exc)) from exc
