"""
Abstração de envio de email transacional.

Suporta dois provedores, controlados pela variável de ambiente EMAIL_PROVIDER:
  - "smtp"  (default) — usa aiosmtplib com servidor SMTP configurável (on-prem, universal)
  - "resend"          — usa a API HTTP do Resend (MESMA config do funil saas-trial:
                        RESEND_API_KEY/RESEND_FROM/RESEND_API_URL — um só serviço de
                        email para toda a plataforma)

Toda a lógica de envio fica aqui; o resto da aplicação chama `enviar_email()`
(genérico) ou `enviar_email_reset_password()` sem se preocupar com o provedor.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

# Teto de espera por um servidor SMTP. O default da biblioteca é um minuto: um
# host errado (ou um IP que engole pacotes) prendia o pedido do administrador
# esse tempo todo, e prendia também a thread que envia as notificações.
TIMEOUT_SMTP_SEGUNDOS = 15


async def enviar_email(destinatario: str, assunto: str, corpo: str) -> None:
    """
    Envia um email de texto simples pelo provedor configurado.

    Não verifica EMAIL_ENABLED nem outros gates — isso é responsabilidade
    do chamador (o reset tem o seu; as notificações têm o gate duplo em
    app/notificacoes/email.py).

    Raises:
        RuntimeError / exceções do provedor se o envio falhar.
    """
    from app.config import get_settings
    settings = get_settings()

    if settings.EMAIL_PROVIDER.lower() == "resend":
        await _enviar_via_resend(destinatario, assunto, corpo, settings)
    else:
        await _enviar_via_smtp(destinatario, assunto, corpo, settings)


_RESET_TEXTOS = {
    "pt": {
        "assunto": "Recuperação de Palavra-passe — NIS2PME",
        "corpo": (
            "Olá,\n\n"
            "Recebemos um pedido de recuperação de palavra-passe para a sua conta.\n\n"
            "Clique no link abaixo para definir uma nova palavra-passe (válido 1 hora):\n"
            "{link}\n\n"
            "Se não solicitou este email, ignore-o. O link expira automaticamente.\n\n"
            "NIS2PME"
        ),
    },
    "en": {
        "assunto": "Password recovery — NIS2PME",
        "corpo": (
            "Hello,\n\n"
            "We received a password recovery request for your account.\n\n"
            "Click the link below to set a new password (valid for 1 hour):\n"
            "{link}\n\n"
            "If you did not request this email, ignore it. The link expires automatically.\n\n"
            "NIS2PME"
        ),
    },
}


def textos_email(tabela: dict, locale: str | None) -> dict:
    """Escolhe a língua de um email: a da empresa, senão português."""
    return tabela.get((locale or "pt").split("-")[0].lower(), tabela["pt"])


async def enviar_email_reset_password(destinatario: str, link: str, locale: str | None = None) -> None:
    """
    Envia email de recuperação de password para o endereço indicado.

    Args:
        destinatario: Endereço de email do destinatário.
        link:         Link de reset completo (APP_URL + token).
        locale:       Língua do destinatário (a da empresa); português por defeito.

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

    textos = textos_email(_RESET_TEXTOS, locale)
    await enviar_email(destinatario, textos["assunto"], textos["corpo"].format(link=link))


async def enviar_email_smtp(
    destinatario: str,
    assunto: str,
    corpo: str,
    *,
    host: str,
    porta: int,
    utilizador: str,
    password: str,
    from_email: str,
    from_name: str,
    starttls: bool,
    ssl_tls: bool,
    timeout: int = TIMEOUT_SMTP_SEGUNDOS,
) -> None:
    """
    Envia por um servidor SMTP indicado explicitamente.

    Existe separada do `_enviar_via_smtp` para que o ecrã de definições possa
    experimentar uma configuração ANTES de a gravar: a que está guardada continua
    intacta enquanto o administrador afina a nova.

    STARTTLS e TLS implícito excluem-se: pedidos os dois, fica o implícito, que é
    o que a porta 465 espera. Sem isto a biblioteca recusaria a ligação e o
    administrador via um erro sem relação com o que configurou.

    **Todos os parâmetros de ligação vão explícitos, nenhum por omissão.** Não é
    verbosidade: o que aqui fica por dizer passa a ser decidido pela biblioteca, e
    muda quando ela mudar. Dois deles guardam comportamento que ninguém veria
    quebrar — o `validate_certs`, sem o qual um intermediário se faz passar pelo
    servidor, e o `start_tls`, cujo valor por omissão é *subir para TLS se o
    servidor o anunciar* (com ele omisso, uma instalação com `SMTP_TLS=false`
    passaria a cifrar sozinha, ou a deixar de o fazer, sem uma linha de aviso).
    """
    import aiosmtplib
    from email.message import EmailMessage
    from email.utils import formataddr

    mensagem = EmailMessage()
    # `formataddr` cita o nome e codifica-o em RFC 2047 quando tem acentos, sem
    # tocar no endereço — que tem de continuar legível para o servidor.
    mensagem["From"] = formataddr((from_name, from_email)) if from_name else from_email
    mensagem["To"] = destinatario
    mensagem["Subject"] = assunto
    mensagem.set_content(corpo)

    await aiosmtplib.send(
        mensagem,
        # O envelope vai explícito em vez de ser extraído dos cabeçalhos: quem
        # recebe a mensagem não decide para onde ela é entregue.
        sender=from_email,
        recipients=[destinatario],
        hostname=host,
        port=porta,
        # Cadeia vazia NÃO é ausência de utilizador: o cliente autentica sempre
        # que o nome não for None, e tentaria AUTH com utilizador vazio contra um
        # relay interno que não pede credenciais nenhumas.
        username=utilizador or None,
        password=password or None,
        timeout=timeout,
        use_tls=ssl_tls,
        start_tls=starttls and not ssl_tls,
        # Fixado, não herdado: é o que impede alguém no caminho de se fazer passar
        # pelo servidor de email.
        validate_certs=True,
        # None mantém o EHLO com o FQDN da máquina, que é o que sempre se enviou.
        local_hostname=None,
        # Sem certificados de cliente nem contexto TLS próprio. Declarados para
        # que uma omissão futura da biblioteca não os invente.
        cert_bundle=None,
        client_cert=None,
        client_key=None,
        tls_context=None,
    )


async def _enviar_via_smtp(
    destinatario: str, assunto: str, corpo: str, settings
) -> None:
    """Envia usando o servidor SMTP configurado na instalação."""
    await enviar_email_smtp(
        destinatario,
        assunto,
        corpo,
        host=settings.SMTP_HOST,
        porta=settings.SMTP_PORT,
        utilizador=settings.SMTP_USER,
        password=settings.SMTP_PASSWORD,
        from_email=settings.SMTP_FROM_EMAIL,
        from_name=settings.SMTP_FROM_NAME,
        starttls=settings.SMTP_TLS,
        ssl_tls=settings.SMTP_SSL,
    )
    logger.info("Email enviado via SMTP para %s", destinatario)


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
