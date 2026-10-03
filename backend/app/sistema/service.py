"""
Saúde do sistema (on-prem) — verificações baratas, pensadas para um admin
não-técnico: cada bloco devolve um estado simples que a UI pinta de
verde/amarelo/vermelho. Nenhuma verificação pode derrubar o endpoint:
falhas individuais ficam contidas no próprio bloco.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlmodel import Session

logger = logging.getLogger(__name__)

# Diretórios persistentes (volumes) cuja capacidade interessa vigiar.
_DIRETORIOS = {"uploads": Path("/app/uploads"), "dados": Path("/app/data")}
_BACKUPS_DIR = Path("/app/data/backups")


def _check_base_dados(db: Session) -> dict:
    try:
        db.execute(text("SELECT 1"))
        tamanho = db.execute(text("SELECT pg_database_size(current_database())")).scalar()
        return {"ok": True, "tamanho_mb": round((tamanho or 0) / 1_048_576, 1)}
    except Exception:  # noqa: BLE001
        logger.exception("Saúde: verificação da base de dados falhou.")
        return {"ok": False}


def _check_disco() -> dict:
    out: dict = {}
    for nome, caminho in _DIRETORIOS.items():
        try:
            uso = shutil.disk_usage(caminho)
            livre_pct = round(uso.free / uso.total * 100, 1)
            out[nome] = {
                "livre_gb": round(uso.free / 1_073_741_824, 1),
                "total_gb": round(uso.total / 1_073_741_824, 1),
                "livre_pct": livre_pct,
                "ok": livre_pct >= 10.0,
            }
        except OSError:
            # Diretório inexistente (ex.: dev fora do container) — omitir em vez de alarmar.
            continue
    return out


def _check_premium() -> dict:
    from app.config import get_settings
    settings = get_settings()

    if not settings.PREMIUM_ENABLED or not settings.PREMIUM_SIDECAR_ADDR:
        return {"configurado": False, "ok": None}
    try:
        import grpc
        from app.premium.client import criar_canal_sidecar

        canal = criar_canal_sidecar(grpc, settings.PREMIUM_SIDECAR_ADDR)
        try:
            grpc.channel_ready_future(canal).result(timeout=3)
            return {"configurado": True, "ok": True}
        finally:
            canal.close()
    except Exception:  # noqa: BLE001
        logger.warning("Saúde: sidecar premium não respondeu.")
        return {"configurado": True, "ok": False}


def _check_email() -> dict:
    from app.config import get_settings
    from app.notificacoes.email import notificacoes_email_ativas

    settings = get_settings()
    provedor = settings.EMAIL_PROVIDER.lower()
    configurado = settings.EMAIL_ENABLED and (
        (provedor == "smtp" and bool(settings.SMTP_HOST))
        or (provedor == "resend" and bool(settings.RESEND_API_KEY))
    )
    return {
        "configurado": configurado,
        "provedor": provedor if configurado else None,
        "notificacoes_ativas": notificacoes_email_ativas(),
        "flag_notificacoes": settings.EMAIL_NOTIFICACOES,
    }


def _check_tls() -> dict:
    from app.config import get_settings
    try:
        from app.setup.https_service import inspecionar_certificado_ativo
        cert = inspecionar_certificado_ativo()
    except Exception:  # noqa: BLE001
        cert = None
    return {"modo": get_settings().TLS_MODE, "cert": cert}


def _check_updates() -> dict:
    try:
        from app.updates.service import obter_estado
        return obter_estado()
    except Exception:  # noqa: BLE001
        return {}


def _check_ticks(app_state) -> dict:
    ticks = getattr(app_state, "ticks", []) or []
    vivos = sum(1 for t in ticks if not t.done())
    return {"total": len(ticks), "vivos": vivos, "ok": vivos == len(ticks) and len(ticks) > 0}


def _check_backups() -> dict:
    """Data do backup mais recente + estado do agendamento diário — para o
    cartão poder avisar quando os backups automáticos não vão acontecer."""
    from app.backup.service import obter_agendado, passphrase_definida

    out: dict = {
        "ultimo": None,
        "agendado": obter_agendado(),
        "passphrase_definida": passphrase_definida(),
    }
    try:
        ficheiros = [f for f in _BACKUPS_DIR.iterdir() if f.is_file()]
    except OSError:
        return out
    if not ficheiros:
        return out
    from datetime import datetime, timezone
    mais_recente = max(f.stat().st_mtime for f in ficheiros)
    out["ultimo"] = datetime.fromtimestamp(mais_recente, tz=timezone.utc).isoformat()
    return out


def obter_saude(db: Session, app_state) -> dict:
    """Fotografia da saúde da instalação — só leituras, todas com falha contida."""
    from app.config import get_settings

    return {
        "versao": get_settings().APP_VERSION,
        "base_dados": _check_base_dados(db),
        "disco": _check_disco(),
        "premium": _check_premium(),
        "email": _check_email(),
        "tls": _check_tls(),
        "updates": _check_updates(),
        "ticks": _check_ticks(app_state),
        "backups": _check_backups(),
    }


# ---------------------------------------------------------------------------
# Configuração do servidor de saída de correio
# ---------------------------------------------------------------------------

def obter_config_email() -> dict:
    """A configuração em vigor, sem a password — dela só se diz se existe."""
    from app.config import get_settings

    s = get_settings()
    return {
        "ativo": s.EMAIL_ENABLED,
        "notificacoes": s.EMAIL_NOTIFICACOES,
        "provedor": s.EMAIL_PROVIDER.lower(),
        "smtp_host": s.SMTP_HOST,
        "smtp_port": s.SMTP_PORT,
        "smtp_user": s.SMTP_USER,
        "smtp_from_email": s.SMTP_FROM_EMAIL,
        "smtp_from_name": s.SMTP_FROM_NAME,
        "smtp_tls": s.SMTP_TLS,
        "smtp_ssl": s.SMTP_SSL,
        "password_definida": bool(s.SMTP_PASSWORD),
    }


def password_efetiva(password_pedida: str | None, host: str | None, utilizador: str | None) -> str:
    """
    A password a usar: a que vem no pedido, ou a guardada se o campo não veio.

    O ecrã nunca recebe a password, logo também não a devolve. Sem esta
    resolução, gravar uma mudança de porta apagava-a — e o teste de envio
    passava a correr com uma credencial diferente da que vai ficar gravada.

    A guardada só vale para o servidor e a conta a que pertence. Antes ia para
    qualquer servidor que viesse no pedido: o administrador punha um servidor seu,
    carregava em «testar» (ou gravava) e recebia a credencial, que o ecrã nunca
    lhe mostra. Outro servidor ou outra conta pedem a password outra vez (400
    `password_obrigatoria`); a password vazia continua a querer dizer «sem
    autenticação».
    """
    from app.config import get_settings

    if password_pedida is not None:
        return password_pedida
    s = get_settings()
    if not s.SMTP_PASSWORD:
        return ""
    mesmo_servidor = (host or "").strip().lower() == (s.SMTP_HOST or "").strip().lower()
    mesma_conta = (utilizador or "").strip() == (s.SMTP_USER or "").strip()
    if mesmo_servidor and mesma_conta:
        return s.SMTP_PASSWORD
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"codigo": "password_obrigatoria"},
    )


def guardar_config_email(dados) -> dict:
    """Aplica e persiste a configuração vinda do ecrã de definições."""
    from app.setup.email_service import aplicar_config_email

    if dados.ativo and not dados.limpar_credenciais and dados.smtp_password is None:
        # Manter a guardada só com o mesmo servidor e a mesma conta (levanta 400).
        password_efetiva(None, dados.smtp_host, dados.smtp_user or "")

    if not dados.ativo:
        return aplicar_config_email(
            ativo=False,
            notificacoes=dados.notificacoes,
            limpar_credenciais=dados.limpar_credenciais,
        )

    return aplicar_config_email(
        ativo=True,
        host=dados.smtp_host,
        porta=dados.smtp_port,
        utilizador=dados.smtp_user or "",
        password=dados.smtp_password,
        from_email=dados.smtp_from_email,
        from_name=dados.smtp_from_name,
        starttls=dados.smtp_tls,
        ssl=dados.smtp_ssl,
        notificacoes=dados.notificacoes,
        limpar_credenciais=dados.limpar_credenciais,
    )


async def testar_config_email(dados, destinatario: str, locale: str | None = None) -> None:
    """
    Experimenta uma configuração enviando um email, sem gravar nada.

    Levanta a exceção da biblioteca se falhar — quem chama traduz para uma
    resposta com código estável, e o detalhe fica no log do servidor.
    """
    from app.shared.email import enviar_email_smtp
    from app.shared.i18n import MsgsI18n, traduzir

    await enviar_email_smtp(
        destinatario,
        traduzir(MsgsI18n.EMAIL_TESTE_ASSUNTO, locale),
        traduzir(MsgsI18n.EMAIL_TESTE_CORPO, locale),
        host=dados.smtp_host,
        porta=dados.smtp_port,
        utilizador=dados.smtp_user or "",
        password=password_efetiva(dados.smtp_password, dados.smtp_host, dados.smtp_user or ""),
        from_email=dados.smtp_from_email,
        from_name=dados.smtp_from_name,
        starttls=dados.smtp_tls,
        ssl_tls=dados.smtp_ssl,
    )
