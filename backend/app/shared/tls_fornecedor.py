"""
Contexto TLS para os serviços do fornecedor que o core contacta (o relay dos
dossiês, o diretório de auditores e o serviço de licenças).

Com a CA privada do fornecedor configurada (`NIS2PME_TLS_CA_PEM`), confia-se
**só** nela, como a app do auditor: os serviços do fornecedor usam certificados
dela, e aceitar também as CAs públicas deixava quem conseguisse um certificado
público para o nome (por um DNS desviado, por exemplo) pôr-se no meio e ficar
com os tokens do convite e do canal de resposta. Uma CA mal escrita fecha tudo:
nada valida, em vez de cair em silêncio nas públicas. Sem CA configurada (uma
instalação antiga), ficam as CAs do sistema.
"""
import base64
import binascii
import logging
import ssl
from functools import lru_cache

from app.config import get_settings

logger = logging.getLogger(__name__)

_MARCA_PEM = "-----BEGIN CERTIFICATE-----"


def pem_da_ca(valor: str) -> str:
    """O PEM, a partir do próprio PEM ou do base64 dele (as duas formas que o
    sidecar também aceita). Devolve "" se não for nenhuma das duas."""
    texto = (valor or "").strip()
    if not texto or _MARCA_PEM in texto:
        return texto
    try:
        decifrado = base64.b64decode(texto, validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ""
    return decifrado if _MARCA_PEM in decifrado else ""


def _so_esta_ca() -> ssl.SSLContext:
    """Contexto cliente com a verificação de sempre (nome e cadeia) e sem
    nenhuma raiz carregada: só valida o que lhe for acrescentado."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


@lru_cache(maxsize=4)
def _contexto(valor: str) -> ssl.SSLContext:
    if not valor.strip():
        return ssl.create_default_context()
    ctx = _so_esta_ca()
    pem = pem_da_ca(valor)
    if not pem:
        logger.error("NIS2PME_TLS_CA_PEM não é um certificado PEM (nem o base64 de um): "
                     "nenhum serviço do fornecedor fica acessível até ser corrigida")
        return ctx
    try:
        ctx.load_verify_locations(cadata=pem)
    except ssl.SSLError as exc:
        logger.error("NIS2PME_TLS_CA_PEM inválida (%s): nenhum serviço do fornecedor fica "
                     "acessível até ser corrigida", exc)
        return _so_esta_ca()
    return ctx


def contexto_fornecedor() -> ssl.SSLContext:
    """Contexto para os pedidos https aos serviços do fornecedor (relay,
    diretório de auditores, serviço de licenças)."""
    return _contexto(get_settings().NIS2PME_TLS_CA_PEM or "")
