"""
Notificações por email — alertas de prazos de incidentes + resumo semanal.

Gate duplo, verificado ANTES de qualquer ação (nesta ordem):
  1. EMAIL_NOTIFICACOES=true no .env  (interruptor mestre destas notificações)
  2. Email configurado: EMAIL_ENABLED=true + provedor com credenciais
     (SMTP_HOST para "smtp"; RESEND_API_KEY para "resend")

Dedup: cada envio regista uma chave única em `email_envios` (constraint UNIQUE
na BD) — o mesmo aviso nunca sai duas vezes, mesmo com ticks repetidos ou
re-arranques. O envio em si corre numa thread de fundo para não atrasar
pedidos nem ticks, e só arranca depois do commit que grava a chave: sem commit
não sai nada. Se o envio falhar, a chave é libertada e o tick seguinte volta a
tentar — a deduplicação nunca guarda uma falha.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, event
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.auth.models import RoleUtilizador, Utilizador
from app.notificacoes.models import EmailEnvio

logger = logging.getLogger(__name__)

# Papéis que recebem emails operacionais (alertas de incidentes).
_ROLES_ALERTA = (RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN)
# Papéis que recebem o resumo semanal (gestão incluída).
_ROLES_DIGEST = (RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN, RoleUtilizador.CEO)

# Os emails saem na língua da empresa (a que ela escolheu para a aplicação);
# sem escolha, em português.
_TEXTOS = {
    "pt": {
        "em_atraso": "EM ATRASO",
        "a_expirar": "a expirar nas próximas 24 horas",
        "assunto_prazo": "[NIS2PME] Prazo legal de incidente {estado}",
        "corpo_prazo": (
            "O incidente «{titulo}» tem um prazo legal {estado}: {marco}.\n"
            "Prazo: {prazo}\n"
            "Base legal: {referencia}\n\n"
            "Prepare o documento e entregue-o na MyCiber (a plataforma não submete à "
            "autoridade); depois registe o envio na secção Incidentes.\n\n"
            "— NIS2PME (aviso automático; configurável no .env: EMAIL_NOTIFICACOES)"
        ),
        "pend_incidentes": "- Incidentes: {n} prazo(s) legal(is) em risco ou em atraso",
        "pend_tarefas_atraso": "- Tarefas: {n} tarefa(s) em atraso",
        "pend_tarefas_vencer": "- Tarefas: {n} tarefa(s) a vencer em breve",
        "pend_formacao": "- Formação: o órgão de gestão ainda não tem formação registada (RJC, arts. 25.º e 27.º)",
        "assunto_trial": "[NIS2PME] O período de avaliação termina a {data}",
        "corpo_trial": (
            "O período de avaliação da NIS2PME termina a {data} ({dias}).\n\n"
            "Nesse dia a conta fica suspensa: ninguém da organização consegue entrar. "
            "Os dados ficam guardados até {apagar}; a partir daí são apagados.\n\n"
            "Para continuar, fale connosco antes do fim do período de avaliação.\n\n"
            "— NIS2PME (aviso automático)"
        ),
        "assunto_licenca": "[NIS2PME] A licença termina a {data}",
        "corpo_licenca": (
            "A licença da NIS2PME desta instalação termina a {data} ({dias}).\n\n"
            "Depois disso há um período de tolerância; no fim dele os módulos premium "
            "ficam só de leitura: os dados continuam a ver-se e a poder exportar-se, mas "
            "deixa de ser possível alterá-los. O núcleo da aplicação continua disponível.\n\n"
            "Para renovar, envie-nos o código de instalação que está em Definições → Sistema → Licença.\n\n"
            "— NIS2PME (aviso automático)"
        ),
        "assunto_tolerancia": "[NIS2PME] A licença terminou — tolerância até {ate}",
        "corpo_tolerancia": (
            "A licença da NIS2PME desta instalação terminou a {data}. Os módulos premium "
            "continuam a funcionar até {ate}; a partir daí ficam só de leitura (os dados "
            "continuam a ver-se e a poder exportar-se).\n\n"
            "Para renovar, envie-nos o código de instalação que está em Definições → Sistema → Licença.\n\n"
            "— NIS2PME (aviso automático)"
        ),
        "assunto_premium_pronto": "[NIS2PME] Módulos premium prontos a ativar",
        "corpo_premium_pronto": (
            "A licença dos módulos premium já chegou e foi validada.\n\n"
            "Para os ativar, um administrador entra na plataforma e vai a Definições → "
            "Sistema → Módulos premium → «Ativar». A aplicação reinicia uma vez (cerca de "
            "um minuto).\n\n"
            "— NIS2PME (aviso automático)"
        ),
        "assunto_premium_recusado": "[NIS2PME] O pedido dos módulos premium foi fechado",
        "corpo_premium_recusado": (
            "O pedido dos módulos premium desta instalação foi fechado ({tipo}).{motivo}\n\n"
            "Pode fazer um pedido novo em Definições → Sistema → Módulos premium.\n\n"
            "— NIS2PME (aviso automático)"
        ),
        "assunto_premium_falhou": "[NIS2PME] Os módulos premium não ficaram instalados",
        "corpo_premium_falhou": (
            "A {etapa} dos módulos premium não terminou (código: {codigo}). {revertido}\n\n"
            "Veja o estado em Definições → Sistema → Módulos premium.\n\n"
            "— NIS2PME (aviso automático)"
        ),
        "assunto_premium_ativado": "[NIS2PME] Módulos premium ativos",
        "corpo_premium_ativado": (
            "Os módulos premium desta instalação estão ativos.\n\n"
            "— NIS2PME (aviso automático)"
        ),
        "premium_tipos": {
            "rejeitado": "recusado pelo fornecedor", "expirado": "30 dias sem resposta",
            "cancelado": "cancelado", "sem_resposta": "sem resposta do serviço de licenças",
            "desconhecido": "o serviço de licenças já não o conhece",
        },
        "premium_motivo": " Motivo indicado pelo fornecedor: {motivo}",
        "premium_etapas": {"instalar": "instalação", "ativar": "ativação"},
        "premium_revertido": "A aplicação voltou a funcionar como estava antes.",
        "assunto_atualizacao_falhou": "[NIS2PME] Uma atualização não terminou",
        "corpo_atualizacao_falhou": (
            "A atualização {componente} para a versão {versao} não terminou (código: {codigo}). "
            "{revertido}\n\n"
            "Veja o estado em Definições → Sistema e Atualizações.\n\n"
            "— NIS2PME (aviso automático)"
        ),
        "componentes": {"nucleo": "da aplicação", "premium": "dos módulos premium"},
        "assunto_backup_incompleto": "[NIS2PME] Backup sem os dados dos módulos premium",
        "corpo_backup_incompleto": (
            "O backup desta instalação foi feito, mas sem os dados dos módulos premium "
            "(inventário, riscos, fornecedores): o serviço premium não os entregou.\n\n"
            "Confirme que os módulos premium estão a funcionar e faça um backup manual em "
            "Definições → Sistema → Backups. Até lá, um restauro não repõe esses dados.\n\n"
            "— NIS2PME (aviso automático)"
        ),
        "falta_1": "falta 1 dia",
        "faltam_n": "faltam {n} dias",
        "assunto_digest": "[NIS2PME] Resumo semanal — a precisar de atenção",
        "corpo_digest": (
            "Pendências de conformidade desta semana:\n\n{linhas}\n\n"
            "Entre na plataforma para tratar destes pontos.\n\n"
            "— NIS2PME (resumo automático semanal; configurável no .env: EMAIL_NOTIFICACOES)"
        ),
    },
    "en": {
        "em_atraso": "OVERDUE",
        "a_expirar": "expiring within the next 24 hours",
        "assunto_prazo": "[NIS2PME] Incident legal deadline {estado}",
        "corpo_prazo": (
            "The incident «{titulo}» has a legal deadline {estado}: {marco}.\n"
            "Deadline: {prazo}\n"
            "Legal basis: {referencia}\n\n"
            "Prepare the document and deliver it through MyCiber (the platform does not "
            "submit to the authority); then record the submission in the Incidents section.\n\n"
            "— NIS2PME (automatic notice; configurable in .env: EMAIL_NOTIFICACOES)"
        ),
        "pend_incidentes": "- Incidents: {n} legal deadline(s) at risk or overdue",
        "pend_tarefas_atraso": "- Tasks: {n} overdue task(s)",
        "pend_tarefas_vencer": "- Tasks: {n} task(s) due soon",
        "pend_formacao": "- Training: the management body has no recorded training yet (RJC, Articles 25 and 27)",
        "assunto_trial": "[NIS2PME] Your trial ends on {data}",
        "corpo_trial": (
            "Your NIS2PME trial ends on {data} ({dias}).\n\n"
            "On that day the account is suspended: nobody in the organisation can sign in. "
            "The data is kept until {apagar}; after that it is deleted.\n\n"
            "To continue, get in touch with us before the trial ends.\n\n"
            "— NIS2PME (automatic notice)"
        ),
        "assunto_licenca": "[NIS2PME] The license ends on {data}",
        "corpo_licenca": (
            "The NIS2PME license for this installation ends on {data} ({dias}).\n\n"
            "After that there is a grace period; when it ends the premium modules become "
            "read-only: the data can still be viewed and exported, but no longer changed. "
            "The core application remains available.\n\n"
            "To renew, send us the installation code shown in Settings → System → License.\n\n"
            "— NIS2PME (automatic notice)"
        ),
        "assunto_tolerancia": "[NIS2PME] The license has ended — grace period until {ate}",
        "corpo_tolerancia": (
            "The NIS2PME license for this installation ended on {data}. The premium modules "
            "keep working until {ate}; after that they become read-only (the data can still "
            "be viewed and exported).\n\n"
            "To renew, send us the installation code shown in Settings → System → License.\n\n"
            "— NIS2PME (automatic notice)"
        ),
        "assunto_premium_pronto": "[NIS2PME] Premium modules ready to activate",
        "corpo_premium_pronto": (
            "The license for the premium modules has arrived and has been validated.\n\n"
            "To activate them, an administrator signs in and goes to Settings → System → "
            "Premium modules → \"Activate\". The application restarts once (about a minute).\n\n"
            "— NIS2PME (automatic notice)"
        ),
        "assunto_premium_recusado": "[NIS2PME] The premium modules request was closed",
        "corpo_premium_recusado": (
            "The premium modules request for this installation was closed ({tipo}).{motivo}\n\n"
            "You can make a new request in Settings → System → Premium modules.\n\n"
            "— NIS2PME (automatic notice)"
        ),
        "assunto_premium_falhou": "[NIS2PME] The premium modules were not installed",
        "corpo_premium_falhou": (
            "The {etapa} of the premium modules did not finish (code: {codigo}). {revertido}\n\n"
            "See the status in Settings → System → Premium modules.\n\n"
            "— NIS2PME (automatic notice)"
        ),
        "assunto_premium_ativado": "[NIS2PME] Premium modules active",
        "corpo_premium_ativado": (
            "The premium modules of this installation are active.\n\n"
            "— NIS2PME (automatic notice)"
        ),
        "premium_tipos": {
            "rejeitado": "declined by the vendor", "expirado": "30 days without an answer",
            "cancelado": "cancelled", "sem_resposta": "no answer from the license service",
            "desconhecido": "the license service no longer knows it",
        },
        "premium_motivo": " Reason given by the vendor: {motivo}",
        "premium_etapas": {"instalar": "installation", "ativar": "activation"},
        "premium_revertido": "The application is running as it was before.",
        "assunto_atualizacao_falhou": "[NIS2PME] An update did not finish",
        "corpo_atualizacao_falhou": (
            "The update {componente} to version {versao} did not finish (code: {codigo}). "
            "{revertido}\n\n"
            "See the status in Settings → System and Updates.\n\n"
            "— NIS2PME (automatic notice)"
        ),
        "componentes": {"nucleo": "of the application", "premium": "of the premium modules"},
        "assunto_backup_incompleto": "[NIS2PME] Backup without the premium modules' data",
        "corpo_backup_incompleto": (
            "This installation's backup was made, but without the data of the premium "
            "modules (inventory, risks, suppliers): the premium service did not hand it over.\n\n"
            "Check that the premium modules are running and make a manual backup in "
            "Settings → System → Backups. Until then, a restore will not bring that data back.\n\n"
            "— NIS2PME (automatic notice)"
        ),
        "falta_1": "1 day left",
        "faltam_n": "{n} days left",
        "assunto_digest": "[NIS2PME] Weekly summary — needs attention",
        "corpo_digest": (
            "Compliance items pending this week:\n\n{linhas}\n\n"
            "Sign in to the platform to deal with these items.\n\n"
            "— NIS2PME (automatic weekly summary; configurable in .env: EMAIL_NOTIFICACOES)"
        ),
    },
}


def _lingua(locale: str | None) -> str:
    codigo = (locale or "pt").split("-")[0].lower()
    return codigo if codigo in _TEXTOS else "pt"


def _locale_da_empresa(db: Session, empresa_id: uuid.UUID) -> str:
    from app.empresas.models import Empresa

    empresa = db.get(Empresa, empresa_id)
    return _lingua(getattr(empresa, "locale_preferido", None))


def _email_da_empresa(db: Session, empresa_id: uuid.UUID) -> bool:
    """O interruptor «Avisos por email» das Definições da empresa.

    `notificacoes_email_ativas` é da instalação; este é o da empresa, que só
    desliga. Estava gravado e não era lido: desligá-lo não parava nenhum email.
    """
    from app.shared.politica_seguranca import politica

    return politica(db, empresa_id).email_notificacoes


def notificacoes_email_ativas() -> bool:
    """Gate duplo: interruptor mestre no .env E email configurado."""
    from app.config import get_settings
    s = get_settings()

    if not s.EMAIL_NOTIFICACOES:
        return False
    if not s.EMAIL_ENABLED:
        return False
    provedor = s.EMAIL_PROVIDER.lower()
    if provedor == "smtp" and not s.SMTP_HOST:
        return False
    if provedor == "resend" and not s.RESEND_API_KEY:
        return False
    return True


def _registar_envio(db: Session, empresa_id: uuid.UUID, chave: str) -> bool:
    """
    Reclama a chave de envio. True = primeira vez (pode enviar);
    False = já foi enviado antes. Usa SAVEPOINT para não perturbar
    a transação do chamador em caso de chave repetida.
    """
    try:
        with db.begin_nested():
            db.add(EmailEnvio(empresa_id=empresa_id, chave=chave))
        return True
    except IntegrityError:
        return False


def _emails_por_role(db: Session, empresa_id: uuid.UUID, roles) -> list[str]:
    """Emails dos utilizadores ativos da empresa com um dos papéis indicados."""
    utilizadores = db.exec(
        select(Utilizador).where(
            Utilizador.empresa_id == empresa_id,
            Utilizador.ativo.is_(True),
            Utilizador.role.in_(list(roles)),
        )
    ).all()
    return [u.email for u in utilizadores if u.email]


_PENDENTES = "emails_pendentes"
_LIGADA = "emails_ligada"


def _agendar_envio(
    db: Session,
    empresa_id: uuid.UUID,
    chave: str,
    destinatarios: list[str],
    assunto: str,
    corpo: str,
) -> None:
    """Guarda o envio na sessão. Só sai depois do commit que grava a chave;
    se a transação acabar sem commit, é descartado com ela."""
    if not destinatarios:
        return
    db.info.setdefault(_PENDENTES, []).append((empresa_id, chave, destinatarios, assunto, corpo))
    if db.info.get(_LIGADA):
        return
    db.info[_LIGADA] = True
    bind = db.get_bind()

    @event.listens_for(db, "after_commit")
    def _depois_do_commit(sessao: Session) -> None:
        for pedido in sessao.info.pop(_PENDENTES, []):
            _enviar_em_fundo(bind, *pedido)

    @event.listens_for(db, "after_transaction_end")
    def _fim_da_transacao(sessao: Session, transacao) -> None:
        # Só a transação de topo decide: um savepoint que recua (chave repetida
        # noutra empresa) não pode deitar fora os envios das outras.
        if transacao.parent is None:
            sessao.info.pop(_PENDENTES, None)


def _libertar_chave(bind, empresa_id: uuid.UUID, chave: str) -> None:
    """Apaga a chave de um envio que falhou, para o tick seguinte voltar a tentar."""
    try:
        with Session(bind) as sessao:
            sessao.execute(
                delete(EmailEnvio).where(EmailEnvio.empresa_id == empresa_id, EmailEnvio.chave == chave)
            )
            sessao.commit()
    except Exception:  # noqa: BLE001 — sem isto o aviso fica preso, mas a app não pode cair
        logger.exception("Não foi possível libertar a chave de envio %s.", chave)


def _enviar_em_fundo(
    bind,
    empresa_id: uuid.UUID,
    chave: str,
    destinatarios: list[str],
    assunto: str,
    corpo: str,
) -> None:
    """Envia numa thread daemon — nunca bloqueia o pedido/tick que originou o aviso.

    Se algum destinatário falhar, a chave é libertada e o próximo tick envia de
    novo a todos: um aviso repetido é melhor do que um prazo legal sem aviso."""

    def _worker() -> None:
        from app.shared.email import enviar_email
        falhou = False
        for dest in destinatarios:
            try:
                asyncio.run(enviar_email(dest, assunto, corpo))
            except Exception:  # noqa: BLE001 — email é melhor-esforço; a app não pode cair por SMTP
                falhou = True
                logger.exception("Falha a enviar email de notificação para %s.", dest)
        if falhou:
            _libertar_chave(bind, empresa_id, chave)

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Alerta imediato: prazo legal de incidente em risco/atraso
# ---------------------------------------------------------------------------

def alertar_prazo_incidente(
    db: Session,
    *,
    empresa_id: uuid.UUID,
    incidente_id: uuid.UUID,
    titulo_incidente: str,
    marco: str,
    em_atraso: bool,
    prazo: datetime | None = None,
) -> bool:
    """
    Envia UM email por marco e prazo de cada incidente aos admins quando um
    prazo legal entra em risco. Se o prazo mudar de data (outra base), é outro
    aviso. Devolve True se o email foi despachado.
    """
    from app.incidentes.textos import MARCOS_TXT, REFERENCIA_MARCO

    if not notificacoes_email_ativas() or not _email_da_empresa(db, empresa_id):
        return False
    chave = f"incidente:{incidente_id}:{marco}"
    if prazo is not None:
        chave += f":{prazo.isoformat()}"
    if not _registar_envio(db, empresa_id, chave):
        return False

    destinatarios = _emails_por_role(db, empresa_id, _ROLES_ALERTA)
    lingua = _locale_da_empresa(db, empresa_id)
    t = _TEXTOS[lingua]
    marco_txt = MARCOS_TXT[lingua].get(marco, marco)
    estado_txt = t["em_atraso"] if em_atraso else t["a_expirar"]
    assunto = t["assunto_prazo"].format(estado=estado_txt)
    corpo = t["corpo_prazo"].format(
        titulo=titulo_incidente, marco=marco_txt, estado=estado_txt,
        prazo=_data_hora(prazo) if prazo is not None else "—",
        referencia=REFERENCIA_MARCO[lingua].get(marco, "—"),
    )
    _agendar_envio(db, empresa_id, chave, destinatarios, assunto, corpo)
    logger.info(
        "Email de prazo de incidente despachado (incidente=%s, marco=%s, destinatários=%d).",
        incidente_id, marco, len(destinatarios),
    )
    return True


def _data_hora(instante: datetime) -> str:
    """Data e hora de Lisboa, com o desvio para UTC ao lado."""
    try:
        from zoneinfo import ZoneInfo

        local = instante.astimezone(ZoneInfo("Europe/Lisbon"))
    except Exception:  # noqa: BLE001 — sem base de fusos: fica em UTC
        local = instante
    desvio = local.strftime("%z") or "+0000"
    return f"{local.strftime('%d/%m/%Y %H:%M')} (UTC{desvio[:3]}:{desvio[3:]})"


# ---------------------------------------------------------------------------
# Fim do acesso: trial (SaaS) ou licença (on-prem)
# ---------------------------------------------------------------------------

def _data_curta(instante: datetime) -> str:
    """Data na hora de Portugal continental: um fim às 23:59 UTC já é o dia
    seguinte em Lisboa no verão, e o email tem de dizer o mesmo que o ecrã."""
    try:
        from zoneinfo import ZoneInfo

        instante = instante.astimezone(ZoneInfo("Europe/Lisbon"))
    except Exception:  # noqa: BLE001 — sem base de fusos: fica em UTC
        pass
    return instante.strftime("%d/%m/%Y")


def alertar_fim_de_acesso(
    db: Session,
    *,
    empresa_id: uuid.UUID,
    tipo: str,
    marco: str,
    termina_em: datetime,
    tolerancia_ate: datetime | None,
) -> bool:
    """
    Um email por marco (dias antes do fim, ou a entrada na tolerância) aos
    administradores. Devolve True se foi despachado.
    """
    if not notificacoes_email_ativas() or not _email_da_empresa(db, empresa_id):
        return False
    chave = f"acesso:{empresa_id}:{tipo}:{termina_em.date().isoformat()}:{marco}"
    if not _registar_envio(db, empresa_id, chave):
        return False

    lingua = _locale_da_empresa(db, empresa_id)
    t = _TEXTOS[lingua]
    dias = max(0, (termina_em - datetime.now(timezone.utc)).days + 1)
    valores = {
        "data": _data_curta(termina_em),
        "dias": t["falta_1"] if dias <= 1 else t["faltam_n"].format(n=dias),
        "apagar": _data_curta(tolerancia_ate) if tolerancia_ate else "",
        "ate": _data_curta(tolerancia_ate) if tolerancia_ate else "",
    }
    if tipo == "trial":
        assunto, corpo = t["assunto_trial"], t["corpo_trial"]
    elif marco == "tolerancia":
        assunto, corpo = t["assunto_tolerancia"], t["corpo_tolerancia"]
    else:
        assunto, corpo = t["assunto_licenca"], t["corpo_licenca"]
    _agendar_envio(
        db, empresa_id, chave, _emails_por_role(db, empresa_id, _ROLES_ALERTA),
        assunto.format(**valores), corpo.format(**valores),
    )
    logger.info("Email de fim de acesso despachado (empresa=%s, tipo=%s, marco=%s).", empresa_id, tipo, marco)
    return True


def alertar_pedido_premium(
    db: Session,
    *,
    empresa_id: uuid.UUID,
    evento: str,
    chave_evento: str,
    params: dict | None = None,
) -> bool:
    """
    Email aos administradores sobre o pedido dos módulos premium: pronto a ativar,
    fechado, falhou ou ativo. Uma vez por evento (`chave_evento`). Devolve True se
    foi despachado. Os outros passos (aprovado, instalado) só ficam no centro de
    notificações: não pedem nada a ninguém.
    """
    if evento not in {"pronto", "recusado", "falhou", "ativado"}:
        return False
    if not notificacoes_email_ativas() or not _email_da_empresa(db, empresa_id):
        return False
    chave = f"premium:{empresa_id}:{chave_evento}"
    if not _registar_envio(db, empresa_id, chave):
        return False
    t = _TEXTOS[_locale_da_empresa(db, empresa_id)]
    p = params or {}
    valores = {
        "tipo": t["premium_tipos"].get(p.get("tipo", ""), p.get("tipo", "")),
        "motivo": t["premium_motivo"].format(motivo=p["motivo"]) if p.get("motivo") else "",
        "etapa": t["premium_etapas"].get(p.get("etapa", ""), ""),
        "codigo": p.get("codigo") or "",
        "revertido": t["premium_revertido"] if p.get("revertido") else "",
    }
    _agendar_envio(
        db, empresa_id, chave, _emails_por_role(db, empresa_id, (RoleUtilizador.ADMIN,)),
        t[f"assunto_premium_{evento}"].format(**valores), t[f"corpo_premium_{evento}"].format(**valores),
    )
    logger.info("Email do pedido premium despachado (empresa=%s, evento=%s).", empresa_id, evento)
    return True


def alertar_atualizacao_falhada(
    db: Session,
    *,
    empresa_id: uuid.UUID,
    pedido_id: str,
    params: dict,
) -> bool:
    """
    Email aos administradores: uma atualização pedida pelo interface não terminou
    (falhou ou foi revertida). Uma vez por pedido. Devolve True se foi despachado.
    """
    if not notificacoes_email_ativas() or not _email_da_empresa(db, empresa_id):
        return False
    chave = f"atualizacao:{empresa_id}:{pedido_id}"
    if not _registar_envio(db, empresa_id, chave):
        return False
    t = _TEXTOS[_locale_da_empresa(db, empresa_id)]
    valores = {
        "componente": t["componentes"].get(params.get("componente", ""), ""),
        "versao": params.get("versao") or "",
        "codigo": params.get("codigo") or "",
        "revertido": t["premium_revertido"] if params.get("revertido") else "",
    }
    _agendar_envio(
        db, empresa_id, chave, _emails_por_role(db, empresa_id, (RoleUtilizador.ADMIN,)),
        t["assunto_atualizacao_falhou"], t["corpo_atualizacao_falhou"].format(**valores),
    )
    logger.info("Email de atualização falhada despachado (empresa=%s).", empresa_id)
    return True


def alertar_backup_incompleto(db: Session, *, empresa_id: uuid.UUID) -> bool:
    """
    Email aos administradores: o backup saiu sem os dados dos módulos premium.
    No máximo um por dia (o agendado é diário; os manuais não o repetem).
    Devolve True se foi despachado.
    """
    if not notificacoes_email_ativas() or not _email_da_empresa(db, empresa_id):
        return False
    chave = f"backup_incompleto:{empresa_id}:{datetime.now(timezone.utc).date().isoformat()}"
    if not _registar_envio(db, empresa_id, chave):
        return False
    t = _TEXTOS[_locale_da_empresa(db, empresa_id)]
    _agendar_envio(
        db, empresa_id, chave, _emails_por_role(db, empresa_id, (RoleUtilizador.ADMIN,)),
        t["assunto_backup_incompleto"], t["corpo_backup_incompleto"],
    )
    logger.info("Email de backup incompleto despachado (empresa=%s).", empresa_id)
    return True


# ---------------------------------------------------------------------------
# Resumo semanal: "a precisar de atenção"
# ---------------------------------------------------------------------------

def _pendencias_empresa(db: Session, empresa_id: uuid.UUID, lingua: str = "pt") -> list[str]:
    """Agrega as pendências dos módulos core (mesma lógica do dashboard)."""
    linhas: list[str] = []
    t = _TEXTOS[lingua]

    from app.incidentes.service import painel as painel_incidentes
    p_inc = painel_incidentes(db, empresa_id)
    if p_inc.prazos_em_risco:
        linhas.append(t["pend_incidentes"].format(n=p_inc.prazos_em_risco))

    from app.tarefas.service import painel as painel_tarefas
    p_tar = painel_tarefas(db, empresa_id)
    if p_tar.em_atraso:
        linhas.append(t["pend_tarefas_atraso"].format(n=p_tar.em_atraso))
    if p_tar.a_vencer:
        linhas.append(t["pend_tarefas_vencer"].format(n=p_tar.a_vencer))

    from app.formacao.service import painel as painel_formacao
    p_for = painel_formacao(db, empresa_id)
    if not p_for.orgao_gestao_ok:
        linhas.append(t["pend_formacao"])

    return linhas


def enviar_digest_semanal(db: Session) -> int:
    """
    Envia o resumo semanal a cada empresa com pendências (um email por
    utilizador de gestão). Dedup por (empresa, semana ISO). Devolve o
    número de empresas notificadas.
    """
    if not notificacoes_email_ativas():
        return 0

    from app.empresas.models import Empresa

    agora = datetime.now(timezone.utc)
    ano, semana, _ = agora.isocalendar()
    enviados = 0

    empresas = db.exec(
        select(Empresa).where(Empresa.ativo.is_(True), Empresa.suspenso.is_(False))
    ).all()
    for empresa in empresas:
        lingua = _lingua(getattr(empresa, "locale_preferido", None))
        try:
            linhas = _pendencias_empresa(db, empresa.id, lingua)
        except Exception:  # noqa: BLE001 — uma empresa com dados estranhos não pode travar as restantes
            logger.exception("Digest: falha a agregar pendências da empresa %s.", empresa.id)
            continue
        if not linhas:
            continue  # sem pendências, sem email — não treinar as pessoas a ignorar avisos
        if not _email_da_empresa(db, empresa.id):
            continue
        chave = f"digest:{empresa.id}:{ano}-W{semana:02d}"
        if not _registar_envio(db, empresa.id, chave):
            continue

        destinatarios = _emails_por_role(db, empresa.id, _ROLES_DIGEST)
        t = _TEXTOS[lingua]
        assunto = t["assunto_digest"]
        corpo = t["corpo_digest"].format(linhas="\n".join(linhas))
        _agendar_envio(db, empresa.id, chave, destinatarios, assunto, corpo)
        enviados += 1

    if enviados:
        db.commit()
        logger.info("Digest semanal: %d empresa(s) notificada(s).", enviados)
    return enviados
