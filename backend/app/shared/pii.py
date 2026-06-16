"""
Cifragem centralizada de campos PII (Personally Identifiable Information).

Usa AES-128-CBC via Fernet (biblioteca cryptography), mesma abordagem
usada para TOTP secrets e conteúdo de evidências.

Campos cobertos:
- Utilizador.nome
- Empresa.nome, Empresa.nif, Empresa.email, Empresa.website
- SuperAdmin.nome
- AuditLog.ip_address, AuditLog.user_agent
- TokenRefresh.ip_address, TokenRefresh.user_agent
- PasswordResetToken.ip_address
- SuperAdminRefreshToken.ip_address, SuperAdminRefreshToken.user_agent
- Evidencia.ficheiro_nome

A chave `PII_ENCRYPTION_KEY` é obrigatória em produção.
Se estiver vazia (modo de desenvolvimento inicial), as funções
passam o valor sem alterar — backward-compatible durante migração.
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Instância singleton do Fernet — inicializada uma vez na primeira chamada
_fernet: Fernet | None = None
_pre_migracao_logged: bool = False


def _get_fernet() -> Fernet | None:
    """
    Devolve a instância Fernet configurada com PII_ENCRYPTION_KEY.
    Devolve None se a chave não estiver definida (modo sem cifra).
    """
    global _fernet
    if _fernet is not None:
        return _fernet

    from app.config import get_settings
    key = get_settings().PII_ENCRYPTION_KEY
    if not key:
        return None

    _fernet = Fernet(key.encode())
    return _fernet


def cifrar_pii(valor: str | None) -> str | None:
    """
    Cifra um campo PII com Fernet (AES-128-CBC + HMAC-SHA256).

    - Se valor é None: devolve None.
    - Se PII_ENCRYPTION_KEY não está definida: devolve valor sem alterações.
    - Caso contrário: devolve o token Fernet em string UTF-8.
    """
    if valor is None:
        return None
    fernet = _get_fernet()
    if fernet is None:
        return valor
    return fernet.encrypt(valor.encode()).decode()


def decifrar_pii(cifrado: str | None) -> str | None:
    """
    Decifra um campo PII cifrado com Fernet.

    - Se cifrado é None: devolve None.
    - Se PII_ENCRYPTION_KEY não está definida: devolve valor sem alterações.
    - Se o token for inválido (campo ainda não migrado ou corrompido):
      devolve o valor original e regista um warning. Isto garante
      compatibilidade com dados existentes durante a migração gradual.
    """
    if cifrado is None:
        return None
    fernet = _get_fernet()
    if fernet is None:
        return cifrado
    try:
        return fernet.decrypt(cifrado.encode()).decode()
    except (InvalidToken, Exception):
        # Campo ainda em texto limpo (dados pré-migração) ou token inválido
        global _pre_migracao_logged
        if not _pre_migracao_logged:
            logger.warning(
                "decifrar_pii: valores não cifrados detectados — dados pré-migração. "
                "Executar 'python scripts/cifrar_pii.py' para cifrar campos PII existentes. "
                "(esta mensagem só aparece uma vez)"
            )
            _pre_migracao_logged = True
        return cifrado
