"""
Contexto TLS para os serviços do fornecedor que o core contacta (o relay dos
dossiês e o diretório de auditores): as CAs públicas do sistema e, se estiver
configurada, a CA privada do fornecedor (`NIS2PME_TLS_CA_PEM`).

O que por lá passa já vai cifrado ponta-a-ponta (o dossiê) ou assinado (o
diretório); a CA serve para a ligação se estabelecer quando o serviço usa um
certificado emitido por ela. Uma CA mal escrita não impede nada: fica registada
e o contexto continua só com as públicas.
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


@lru_cache(maxsize=4)
def _contexto(valor: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    pem = pem_da_ca(valor)
    if valor.strip() and not pem:
        logger.warning("NIS2PME_TLS_CA_PEM não é um certificado PEM (nem o base64 de um): ignorada")
    elif pem:
        try:
            ctx.load_verify_locations(cadata=pem)
        except ssl.SSLError as exc:
            logger.warning("NIS2PME_TLS_CA_PEM inválida (%s): ignorada", exc)
            ctx = ssl.create_default_context()
    return ctx


def contexto_fornecedor() -> ssl.SSLContext:
    """Contexto para os pedidos https ao relay e ao diretório de auditores."""
    return _contexto(get_settings().NIS2PME_TLS_CA_PEM or "")
