"""
Configuração de SMTP da instalação (on-prem).

Persiste as variáveis SMTP_*/EMAIL_* no ficheiro .env (montado como bind mount
em /app/.env no container) e aplica-as imediatamente ao processo em execução,
sem necessitar reinício. A escrita segura no .env vive em app.setup.env_file.

O servidor de saída de correio é da INSTALAÇÃO, não de cada organização: existe
um por máquina, e em SaaS o envio é central. Por isso fica aqui e não na base de
dados — a credencial não entra em exportações, dossiês nem cópias da base.

Dois ecrãs escrevem por aqui — o assistente de primeira execução e as definições
— e ambos passam pelo mesmo `aplicar_config_email`. Duas escritas com regras
diferentes acabariam sempre por divergir numa delas.
"""
import logging

from fastapi import HTTPException, status
from pydantic import ValidationError

from app.config import get_settings
from app.setup.env_file import atualizar_env
from app.setup.schemas import SetupEmailSchema

logger = logging.getLogger(__name__)

_COMENTARIO = "# --- Email SMTP (configurado na aplicação) ---"


def aplicar_config_email(
    *,
    ativo: bool,
    host: str | None = None,
    porta: int | None = None,
    utilizador: str | None = None,
    password: str | None = None,
    from_email: str | None = None,
    from_name: str | None = None,
    starttls: bool | None = None,
    ssl: bool | None = None,
    notificacoes: bool | None = None,
    limpar_credenciais: bool = False,
) -> dict:
    """
    Escreve a configuração de email no .env e aplica-a de imediato.

    Campos a None não são tocados — quem só quer mudar a porta não tem de
    reenviar tudo. Em particular, `password=None` mantém a que está guardada
    (o ecrã nunca a recebe, logo não a pode devolver) e `password=""` limpa-a,
    que é como se passa a usar um relé sem autenticação.

    Devolve {"email_ativo": bool}.
    """
    updates: dict[str, str] = {"EMAIL_ENABLED": "true" if ativo else "false"}
    if ativo:
        updates["EMAIL_PROVIDER"] = "smtp"
    if notificacoes is not None:
        updates["EMAIL_NOTIFICACOES"] = "true" if notificacoes else "false"

    if host is not None:
        updates["SMTP_HOST"] = host
    if porta is not None:
        updates["SMTP_PORT"] = str(porta)
    if from_email is not None:
        updates["SMTP_FROM_EMAIL"] = from_email
    if from_name is not None:
        updates["SMTP_FROM_NAME"] = from_name
    if starttls is not None:
        updates["SMTP_TLS"] = "true" if starttls else "false"
    if ssl is not None:
        updates["SMTP_SSL"] = "true" if ssl else "false"

    if limpar_credenciais:
        # Desligar o email não apagava a conta nem a password do .env. Ficavam a
        # viver num ficheiro que já não servia nada — e quem desliga um serviço
        # espera que as credenciais dele deixem de estar lá.
        updates["SMTP_USER"] = ""
        updates["SMTP_PASSWORD"] = ""
    else:
        if utilizador is not None:
            updates["SMTP_USER"] = utilizador
        if password is not None:
            updates["SMTP_PASSWORD"] = password

    atualizar_env(updates, comentario=_COMENTARIO)

    try:
        get_settings()
    except ValidationError as exc:
        logger.exception("Configuração de email inválida após gravação no .env.")
        # Não ecoar o texto da exceção ao utilizador (pode revelar internos/valores) —
        # mensagem estável; o detalhe completo fica no log do servidor acima.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Configuração SMTP inválida. Verifique host, porta e credenciais.",
        ) from exc

    return {"email_ativo": get_settings().EMAIL_ENABLED}


def configurar_email_smtp(dados: SetupEmailSchema) -> dict:
    """Caminho do assistente de primeira execução: configura ou desativa o SMTP."""
    if not dados.usar_smtp:
        return aplicar_config_email(ativo=False)

    return aplicar_config_email(
        ativo=True,
        host=dados.smtp_host,
        porta=dados.smtp_port,
        utilizador=dados.smtp_user or "",
        password=dados.smtp_password or "",
        from_email=dados.smtp_from_email,
        from_name=dados.smtp_from_name,
        starttls=dados.smtp_tls,
        ssl=dados.smtp_ssl,
    )
