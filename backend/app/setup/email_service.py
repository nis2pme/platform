"""
Configuração de SMTP via wizard de setup (on-prem).

Persiste as variáveis SMTP_*/EMAIL_* no ficheiro .env (montado como bind mount
em /app/.env no container) e aplica-as imediatamente ao processo em execução,
sem necessitar reinício. A escrita segura no .env vive em app.setup.env_file.
"""
import logging

from fastapi import HTTPException, status
from pydantic import ValidationError

from app.config import get_settings
from app.setup.env_file import atualizar_env
from app.setup.schemas import SetupEmailSchema

logger = logging.getLogger(__name__)


def configurar_email_smtp(dados: SetupEmailSchema) -> dict:
    """
    Configura (ou desativa) o envio de email via SMTP, persistindo no .env.
    Devolve {"email_ativo": bool}.
    """
    if not dados.usar_smtp:
        atualizar_env({"EMAIL_ENABLED": "false"})
        return {"email_ativo": False}

    updates = {
        "EMAIL_ENABLED": "true",
        "EMAIL_PROVIDER": "smtp",
        "SMTP_HOST": dados.smtp_host,
        "SMTP_PORT": str(dados.smtp_port),
        "SMTP_USER": dados.smtp_user or "",
        "SMTP_PASSWORD": dados.smtp_password or "",
        "SMTP_FROM_EMAIL": dados.smtp_from_email,
        "SMTP_FROM_NAME": dados.smtp_from_name,
        "SMTP_TLS": "true" if dados.smtp_tls else "false",
    }
    atualizar_env(
        updates,
        comentario="# --- Email SMTP (configurado via wizard de setup) ---",
    )

    try:
        get_settings()
    except ValidationError as exc:
        logger.exception("Configuração SMTP inválida após gravação no .env.")
        # Não ecoar o texto da exceção ao utilizador (pode revelar internos/valores) —
        # mensagem estável; o detalhe completo fica no log do servidor acima.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Configuração SMTP inválida. Verifique host, porta e credenciais.",
        ) from exc

    return {"email_ativo": True}
