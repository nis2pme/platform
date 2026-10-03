"""
Keyslot do dossiê por frase-passe: um ficheiro age v1 com um só destinatário
scrypt, de custo FIXO.

O `pyrage.passphrase` escolhe o custo do scrypt pela velocidade do CPU em que
corre e não deixa fixá-lo: no mesmo servidor saiu 2^17 (128 MiB) e 2^18, e num
portátil 2^19 (512 MiB), o que não cabe num backend de PME com mais trabalho
por cima. O keyslot é pequeno (a identidade do dossiê, uma linha de texto), por
isso escreve-se aqui, segundo a especificação pública do age
(age-encryption.org/v1): abre-se com qualquer implementação do age (o pyrage, o
crate `age`, o comando `age -d`), e só o custo deixa de variar.

Sem dependências da aplicação: o gerador dos fixtures da plataforma do auditor
e os testes de ponta a ponta carregam este ficheiro diretamente.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# Custo ao escrever: 2^17 × r=8 = 128 MiB, o mesmo dos backups.
N_LOG2 = 17
# O que os leitores aceitam. O custo vem no cabeçalho, escolhido por quem gerou
# o ficheiro, e paga-se antes de se saber se a frase-passe está certa; um
# degrau acima do que se escreve deixa uma versão futura subir o custo sem
# partir os leitores que já existem.
N_LOG2_MAX = 18

_VERSAO = b"age-encryption.org/v1"
_ROTULO_SCRYPT = b"age-encryption.org/v1/scrypt"
_BLOCO = 64 * 1024
# `-> scrypt <sal de 16 bytes em base64 sem `=`> <custo em decimal, sem zeros à esquerda>`
_STANZA = re.compile(rb"-> scrypt [A-Za-z0-9+/]{22} ([1-9][0-9]?)")


def _b64(dados: bytes) -> bytes:
    """base64 padrão sem `=`, como o age escreve argumentos e corpos."""
    return base64.b64encode(dados).rstrip(b"=")


def _hkdf(chave: bytes, sal: bytes | None, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=sal, info=info).derive(chave)


def cifrar(claro: bytes, passphrase: str, n_log2: int = N_LOG2) -> bytes:
    """`claro` cifrado para a frase-passe, no formato binário do age."""
    chave_ficheiro = os.urandom(16)
    sal = os.urandom(16)
    embrulho = Scrypt(salt=_ROTULO_SCRYPT + sal, length=32, n=2**n_log2, r=8, p=1).derive(
        passphrase.encode("utf-8")
    )
    # A chave do ficheiro embrulhada: 32 bytes → 43 caracteres, uma só linha
    # (o age parte os corpos às 64 colunas).
    corpo = ChaCha20Poly1305(embrulho).encrypt(b"\x00" * 12, chave_ficheiro, None)
    cabecalho = b"".join([
        _VERSAO, b"\n",
        b"-> scrypt ", _b64(sal), b" ", str(n_log2).encode("ascii"), b"\n",
        _b64(corpo), b"\n",
        b"---",
    ])
    mac = hmac.new(_hkdf(chave_ficheiro, None, b"header"), cabecalho, hashlib.sha256).digest()

    # Conteúdo: blocos de 64 KiB com ChaCha20-Poly1305; o nonce é o número do
    # bloco (11 bytes) e um byte que marca o último.
    nonce = os.urandom(16)
    aead = ChaCha20Poly1305(_hkdf(chave_ficheiro, nonce, b"payload"))
    blocos = [claro[i:i + _BLOCO] for i in range(0, len(claro), _BLOCO)] or [b""]
    cifrados = [
        aead.encrypt(i.to_bytes(11, "big") + (b"\x01" if i == len(blocos) - 1 else b"\x00"), bloco, None)
        for i, bloco in enumerate(blocos)
    ]
    return cabecalho + b" " + _b64(mac) + b"\n" + nonce + b"".join(cifrados)


def custo_scrypt(keyslot: bytes) -> int | None:
    """O log2 N do scrypt de um keyslot por frase-passe, lido só do cabeçalho
    (sem derivar nada). None se não for um ficheiro age com um destinatário
    scrypt bem formado."""
    linhas = keyslot[:256].split(b"\n", 2)
    if len(linhas) < 3 or linhas[0] != _VERSAO:
        return None
    encontrado = _STANZA.fullmatch(linhas[1])
    return int(encontrado.group(1)) if encontrado else None
