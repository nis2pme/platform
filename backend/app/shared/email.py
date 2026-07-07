"""
Abstração de envio de email transacional.

Suporta dois provedores, controlados pela variável de ambiente EMAIL_PROVIDER:
  - "smtp"  (default) — usa fastapi-mail com servidor SMTP configurável (on-prem, universal)
  - "resend"          — usa a API HTTP do Resend (MESMA config do funil saas-trial:
                        RESEND_API_KEY/RESEND_FROM/RESEND_API_URL — um só serviço de
                        email para toda a plataforma)

Toda a lógica de envio fica aqui; o resto da aplicação chama apenas
`enviar_email_reset_password()` sem se preocupar com o provedor.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


async def enviar_email_reset_password(destinatario: str, link: str) -> None:
    """
    Envia email de recuperação de password para o endereço indicado.

    Args:
        destinatario: Endereço de email do destinatário.
        link:         Link de reset completo (APP_URL + token).

    Raises:
        RuntimeError: Se o envio falhar (para tratamento no caller).
    """
    from app.config import get_settings
    settings = get_settings()

    if not settings.EMAIL_ENABLED:
        logger.warning(
            "EMAIL_ENABLED=false — email NÃO enviado. Link de reset (apenas dev): %s", link
        )
        return

    assunto = "Recuperação de Palavra-passe — NIS2PME"
    corpo = (
        f"Olá,\n\n"
        f"Recebemos um pedido de recuperação de palavra-passe para a sua conta.\n\n"
        f"Clique no link abaixo para definir uma nova palavra-passe (válido 1 hora):\n"
        f"{link}\n\n"
        f"Se não solicitou este email, ignore-o. O link expira automaticamente.\n\n"
        f"NIS2PME"
    )

    provedor = settings.EMAIL_PROVIDER.lower()

    if provedor == "resend":
        await _enviar_via_resend(destinatario, assunto, corpo, settings)
    else:
        await _enviar_via_smtp(destinatario, assunto, corpo, settings)


async def _enviar_via_smtp(
    destinatario: str, assunto: str, corpo: str, settings
) -> None:
    """Envia usando fastapi-mail com servidor SMTP."""
    from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType

    conf = ConnectionConfig(
        MAIL_USERNAME=settings.SMTP_USER,
        MAIL_PASSWORD=settings.SMTP_PASSWORD,
        MAIL_FROM=settings.SMTP_FROM_EMAIL,
        MAIL_PORT=settings.SMTP_PORT,
        MAIL_SERVER=settings.SMTP_HOST,
        MAIL_FROM_NAME=settings.SMTP_FROM_NAME,
        MAIL_STARTTLS=settings.SMTP_TLS,
        MAIL_SSL_TLS=False,
        USE_CREDENTIALS=bool(settings.SMTP_USER),
    )
    mensagem = MessageSchema(
        subject=assunto,
        recipients=[destinatario],
        body=corpo,
        subtype=MessageType.plain,
    )
    fm = FastMail(conf)
    await fm.send_message(mensagem)
    logger.info("Email de reset enviado via SMTP para %s", destinatario)


async def _enviar_via_resend(
    destinatario: str, assunto: str, corpo: str, settings
) -> None:
    """
    Envia via API HTTP do Resend — a MESMA config/serviço que o funil saas-trial usa
    para o email de verificação (RESEND_API_KEY/RESEND_FROM/RESEND_API_URL).

    Stdlib (urllib) numa thread — sem dependências novas no open-core (mesma disciplina
    de app/premium/provisioning.py) e sem bloquear o event loop. O corpo da resposta do
    Resend NUNCA é exposto ao chamador/utilizador; só o código HTTP vai para o log.
    """
    import json
    import urllib.error
    import urllib.request

    def _chamar_api() -> None:
        payload = json.dumps(
            {
                "from": settings.RESEND_FROM,
                "to": [destinatario],
                "subject": assunto,
                "text": corpo,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            settings.RESEND_API_URL,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                "Content-Type": "application/json",
                # UA explícito: o default "Python-urllib/x" é bloqueado pela Cloudflare
                # à frente da API do Resend (erro 1010 "banned browser signature").
                "User-Agent": "nis2pme-backend/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"Resend devolveu {resp.status}")
        except urllib.error.HTTPError as exc:
            logger.error("Resend API erro %s — verifique RESEND_API_KEY/RESEND_FROM no .env", exc.code)
            raise RuntimeError(f"Resend API erro {exc.code}") from exc

    await asyncio.to_thread(_chamar_api)
    logger.info("Email de reset enviado via Resend para %s", destinatario)
