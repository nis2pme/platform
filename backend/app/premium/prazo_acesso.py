"""
Fim do acesso: quando avisar que o trial (SaaS) ou a licença (on-prem) acabam.

Até aqui o trial era suspenso no último dia sem aviso nenhum, e o fim da
licença on-prem só aparecia num cartão da aba Sistema que pouca gente abre.
Daqui sai, num só sítio, o que a faixa no topo da aplicação mostra e os avisos
que o tick diário envia (centro de notificações e email aos administradores).

A data do trial é a da borda de registo — é ela que suspende a conta — e chega
ao core no registo (`Empresa.trial_expira_em`). A da licença on-prem vem do
sidecar, que é quem a verifica.

Níveis da faixa, por dias que faltam:
  - trial:   informativa a 14, aviso a 7, urgente no último dia;
  - licença: informativa a 30, aviso a 7, urgente no último dia e durante a
    tolerância (depois dela os módulos premium ficam só de leitura, sem limite
    de tempo, e os dados continuam a poder exportar-se).
A faixa pode fechar-se por um dia, exceto nos últimos três.
"""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

logger = logging.getLogger(__name__)

INFO, AVISO, URGENTE = "info", "aviso", "urgente"

# (dias para informativa, para aviso, para urgente)
LIMIARES_TRIAL = (14, 7, 1)
LIMIARES_LICENCA = (30, 7, 1)
# Dias antes do fim em que sai uma notificação e um email.
MARCOS_TRIAL = (7, 3, 1)
MARCOS_LICENCA = (30, 14, 7, 1)
# Nos últimos dias a faixa deixa de se poder fechar.
SEM_FECHAR_DIAS = 3
# Estados da licença que não pedem aviso nenhum: sem premium, gerida pela
# plataforma (SaaS), sidecar inalcançável, ficheiro inválido (o cartão trata).
_LICENCA_SEM_AVISO = {"sem_premium", "gerida", "indisponivel", "invalida", ""}
_CODIGOS_FIM_DE_ACESSO = (
    "sistema.trial_a_terminar", "sistema.licenca_a_expirar", "sistema.licenca_em_tolerancia",
)


@dataclass
class Prazo:
    tipo: str = ""                         # "trial" | "licenca" | "" (nada a avisar)
    nivel: str = ""                        # "" | info | aviso | urgente
    termina_em: datetime | None = None
    # Trial: a partir de quando os dados podem ser apagados. Licença: fim da tolerância.
    tolerancia_ate: datetime | None = None
    dias: int | None = None
    em_tolerancia: bool = False
    expirada: bool = False
    fechavel: bool = True
    # On-prem: validação da licença pelo serviço ("em_falta", "so_leitura",
    # "nao_validada", "revogada"; "airgap" não avisa).
    heartbeat: str = ""
    dias_sem_heartbeat: int = 0
    # Não se conseguiu perguntar ao sidecar. Para a faixa é o mesmo que «nada a
    # avisar»; para o tick não é: não pode dar os avisos por resolvidos.
    indisponivel: bool = False


def _utc(instante: datetime | None) -> datetime | None:
    if instante is None:
        return None
    return instante.replace(tzinfo=timezone.utc) if instante.tzinfo is None else instante.astimezone(timezone.utc)


def _ler_data(texto: str | None) -> datetime | None:
    if not texto:
        return None
    try:
        return _utc(datetime.fromisoformat(texto.replace("Z", "+00:00")))
    except ValueError:
        return None


def dias_ate(termina: datetime, agora: datetime) -> int:
    """Dias que faltam, arredondados para cima: a 30 h do fim ainda faltam 2."""
    segundos = (termina - agora).total_seconds()
    return max(0, math.ceil(segundos / 86400))


def _nivel(dias: int, limiares: tuple[int, int, int]) -> str:
    info, aviso, urgente = limiares
    if dias <= urgente:
        return URGENTE
    if dias <= aviso:
        return AVISO
    if dias <= info:
        return INFO
    return ""


def prazo_trial(empresa, agora: datetime, tolerancia_dias: int) -> Prazo:
    """Prazo do trial SaaS. Só as empresas no plano trial com data da borda."""
    termina = _utc(getattr(empresa, "trial_expira_em", None))
    if getattr(empresa, "plano", None) != "trial" or termina is None:
        return Prazo()
    dias = dias_ate(termina, agora)
    return Prazo(
        tipo="trial",
        nivel=_nivel(dias, LIMIARES_TRIAL),
        termina_em=termina,
        tolerancia_ate=termina + timedelta(days=tolerancia_dias),
        dias=dias,
        expirada=termina <= agora,
        fechavel=dias > SEM_FECHAR_DIAS,
    )


def prazo_licenca(estado, agora: datetime) -> Prazo:
    """Prazo da licença on-prem, a partir do estado que o sidecar devolve."""
    codigo = getattr(estado, "estado", "") or ""
    if codigo in _LICENCA_SEM_AVISO:
        return Prazo()
    termina = _ler_data(getattr(estado, "expires_at", None))
    tolerancia = _ler_data(getattr(estado, "grace_ate", None))
    heartbeat = getattr(estado, "heartbeat_estado", "") or ""
    p = Prazo(
        tipo="licenca",
        termina_em=termina,
        tolerancia_ate=tolerancia,
        heartbeat=heartbeat if heartbeat in ("em_falta", "so_leitura", "nao_validada", "revogada") else "",
        dias_sem_heartbeat=int(getattr(estado, "dias_sem_heartbeat", 0) or 0),
    )
    if codigo == "expirada":
        p.nivel, p.expirada, p.fechavel, p.dias = URGENTE, True, False, 0
    elif codigo == "em_grace":
        p.nivel, p.em_tolerancia, p.fechavel, p.dias = URGENTE, True, False, 0
    elif termina is not None:
        p.dias = dias_ate(termina, agora)
        p.nivel = _nivel(p.dias, LIMIARES_LICENCA)
        p.fechavel = p.dias > SEM_FECHAR_DIAS
    # Sem validação pelo serviço, ou revogada: os módulos estão só de leitura,
    # com ou sem data de fim. Não deixa a faixa mais leve do que já está.
    if p.heartbeat in ("so_leitura", "nao_validada", "revogada"):
        p.nivel, p.fechavel = URGENTE, False
    elif p.heartbeat == "em_falta" and p.nivel in ("", INFO):
        p.nivel = AVISO
    return p


def _tolerancia_trial_dias() -> int:
    from app.config import get_settings

    return get_settings().SAAS_TRIAL_GRACE_DIAS


def _nif(empresa) -> str:
    try:
        from app.shared.pii import decifrar_pii

        return decifrar_pii(empresa.nif) or ""
    except Exception:  # noqa: BLE001 — só serve o código de instalação; nunca bloqueia
        return ""


def obter_prazo(db: Session, empresa, premium=None, *, agora: datetime | None = None) -> Prazo:
    """O prazo desta empresa, conforme o modo de instalação. Nunca levanta."""
    from app.config import get_settings

    agora = agora or datetime.now(timezone.utc)
    if get_settings().DEPLOYMENT_MODE == "saas":
        return prazo_trial(empresa, agora, _tolerancia_trial_dias())
    try:
        if premium is None:
            from app.premium.client import get_premium_client

            premium = get_premium_client()
        estado = premium.estado_licenca(str(empresa.id), _nif(empresa))
    except Exception:  # noqa: BLE001 — informativo: sidecar em baixo = sem faixa
        logger.warning("prazo de acesso: sidecar inalcançável", exc_info=True)
        return Prazo(indisponivel=True)
    return prazo_licenca(estado, agora)


# ---------------------------------------------------------------------------
# Tick diário: notificação no centro + email aos administradores
# ---------------------------------------------------------------------------

def _marco(prazo: Prazo) -> str | None:
    """O marco em que estamos (o menor que já se atingiu), ou None."""
    if prazo.em_tolerancia:
        return "tolerancia"
    if prazo.dias is None or prazo.expirada:
        return None
    marcos = MARCOS_TRIAL if prazo.tipo == "trial" else MARCOS_LICENCA
    atingidos = [m for m in marcos if prazo.dias <= m]
    return str(min(atingidos)) if atingidos else None


def _destinatarios(db: Session, empresa_id: uuid.UUID) -> list:
    from app.auth.models import RoleUtilizador, Utilizador

    return list(db.exec(
        select(Utilizador).where(
            Utilizador.empresa_id == empresa_id,
            Utilizador.ativo.is_(True),  # type: ignore[union-attr]
            Utilizador.role.in_([RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN]),  # type: ignore[attr-defined]
        )
    ).all())


def avisar_empresa(db: Session, empresa, prazo: Prazo) -> bool:
    """Cria a notificação do marco atual (uma vez por marco) e envia o email.
    Devolve True se nasceu um aviso novo. Não faz commit."""
    from app.notificacoes.catalogo import Codigo, chave
    from app.notificacoes.email import alertar_fim_de_acesso
    from app.notificacoes.service import criar_notificacao

    if prazo.indisponivel:
        # Sem resposta do sidecar não se sabe nada: os avisos ficam como estão.
        return False
    marco = _marco(prazo)
    if marco is None or prazo.termina_em is None:
        # Sem marco atual (prolongado para lá dos avisos, plano pago, licença
        # renovada): os avisos que ainda houver deixaram de ser verdade.
        _resolver_outros(db, empresa.id, chave_atual=None)
        return False
    if prazo.tipo == "trial":
        codigo = Codigo.TRIAL_A_TERMINAR
    elif marco == "tolerancia":
        codigo = Codigo.LICENCA_EM_TOLERANCIA
    else:
        codigo = Codigo.LICENCA_A_EXPIRAR
    # A chave usa o dia (UTC); os params levam o instante completo, para o ecrã
    # o mostrar na hora de quem lê — a mesma data que a faixa mostra.
    data = prazo.termina_em.date().isoformat()
    params = {"data": prazo.termina_em.isoformat(), "dias": prazo.dias or 0}
    if marco == "tolerancia" and prazo.tolerancia_ate is not None:
        params["ate"] = prazo.tolerancia_ate.isoformat()

    from app.notificacoes.models import Notificacao

    chave_marco = chave(codigo, data, marco)
    novo = False
    for u in _destinatarios(db, empresa.id):
        # Uma vez por marco e por pessoa, mesmo depois de lida: a deduplicação
        # do catálogo só vale enquanto está por ler, e o tick corre todos os dias.
        ja_teve = db.exec(
            select(Notificacao.id).where(
                Notificacao.utilizador_id == u.id,
                Notificacao.chave_dedup == chave_marco,
            )
        ).first()
        if ja_teve:
            continue
        if criar_notificacao(
            db, empresa_id=empresa.id, utilizador_id=u.id, codigo=codigo,
            params=params, dedup_partes=(data, marco),
        ) is not None:
            novo = True
    db.flush()
    # O aviso do marco atual substitui todos os outros de fim de acesso da
    # empresa: os marcos já passados e os de uma data que entretanto mudou
    # (trial prolongado, licença renovada) deixaram de descrever a realidade.
    _resolver_outros(db, empresa.id, chave_atual=chave_marco)
    alertar_fim_de_acesso(
        db, empresa_id=empresa.id, tipo=prazo.tipo, marco=marco,
        termina_em=prazo.termina_em, tolerancia_ate=prazo.tolerancia_ate,
    )
    return novo


def _resolver_outros(db: Session, empresa_id: uuid.UUID, *, chave_atual: str | None) -> None:
    """Dá por lidos os avisos de fim de acesso da empresa, menos o do marco atual."""
    from app.notificacoes.models import Notificacao
    from app.notificacoes.service import dar_por_resolvidas

    consulta = select(Notificacao).where(
        Notificacao.empresa_id == empresa_id,
        Notificacao.codigo.in_(_CODIGOS_FIM_DE_ACESSO),  # type: ignore[attr-defined]
        Notificacao.lida.is_(False),  # type: ignore[union-attr]
    )
    if chave_atual is not None:
        consulta = consulta.where(Notificacao.chave_dedup != chave_atual)
    dar_por_resolvidas(db, db.exec(consulta).all())


def avisar_fim_de_acesso(db: Session, premium=None, *, agora: datetime | None = None) -> int:
    """Corpo do tick diário. Devolve quantas empresas receberam um aviso novo."""
    from app.config import get_settings
    from app.empresas.models import Empresa

    agora = agora or datetime.now(timezone.utc)
    consulta = select(Empresa).where(
        Empresa.deleted_at.is_(None),  # type: ignore[union-attr]
        Empresa.suspenso.is_(False),  # type: ignore[attr-defined]
    )
    if get_settings().DEPLOYMENT_MODE == "saas":
        from sqlalchemy import or_

        from app.notificacoes.models import Notificacao

        # Os trials com data, e quem ainda tem avisos por ler (passou a plano
        # pago ou deixou de ter data): esses são para limpar.
        com_avisos = select(Notificacao.empresa_id).where(
            Notificacao.codigo.in_(_CODIGOS_FIM_DE_ACESSO),  # type: ignore[attr-defined]
            Notificacao.lida.is_(False),  # type: ignore[union-attr]
        )
        consulta = consulta.where(or_(
            (Empresa.plano == "trial") & Empresa.trial_expira_em.is_not(None),  # type: ignore[union-attr]
            Empresa.id.in_(com_avisos),  # type: ignore[attr-defined]
        ))
    avisadas = 0
    for empresa in db.exec(consulta).all():
        try:
            if avisar_empresa(db, empresa, obter_prazo(db, empresa, premium, agora=agora)):
                avisadas += 1
            db.commit()
        except Exception:  # noqa: BLE001 — uma empresa não pára as outras
            db.rollback()
            logger.exception("Aviso de fim de acesso falhou para a empresa %s.", empresa.id)
    if avisadas:
        logger.info("Fim de acesso: %d empresa(s) avisada(s).", avisadas)
    return avisadas
