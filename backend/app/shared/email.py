"""
Abstração de envio de email transacional.

Suporta dois provedores, controlados pela variável de ambiente EMAIL_PROVIDER:
  - "smtp"  (default) — usa fastapi-mail com servidor SMTP configurável
  - "brevo"           — usa a API transacional do Brevo (sib-api-v3-sdk)

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

    if provedor == "brevo":
        await _enviar_via_brevo(destinatario, assunto, corpo, settings)
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


async def _enviar_via_brevo(
    destinatario: str, assunto: str, corpo: str, settings
) -> None:
    """
    Envia usando a API transacional do Brevo (sib-api-v3-sdk).
    O SDK é síncrono — executa em thread executor para não bloquear o event loop.
    """
    def _chamar_api() -> None:
        import sib_api_v3_sdk
        from sib_api_v3_sdk.rest import ApiException

        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key["api-key"] = settings.BREVO_API_KEY

        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
            sib_api_v3_sdk.ApiClient(configuration)
        )

        email_obj = sib_api_v3_sdk.SendSmtpEmail(
            to=[{"email": destinatario}],
            sender={
                "name": settings.SMTP_FROM_NAME,
                "email": settings.SMTP_FROM_EMAIL,
            },
            subject=assunto,
            text_content=corpo,
        )

        try:
            api_instance.send_transac_email(email_obj)
        except ApiException as exc:
            logger.error(
                "Brevo API erro %s (%s) — verifique BREVO_API_KEY no .env",
                exc.status,
                exc.reason,
            )
            raise RuntimeError(
                f"Brevo API erro {exc.status}: {exc.reason}"
            ) from exc

    # Executa o SDK síncrono numa thread separada para não bloquear asyncio
    await asyncio.to_thread(_chamar_api)
    logger.info("Email de reset enviado via Brevo para %s", destinatario)
