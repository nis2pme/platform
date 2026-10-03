"""
Provisionamento de entitlements no signup (SaaS) — best-effort.

Quando o core cria um tenant em modo saas, pede ao GATEWAY (o escritor ÚNICO dos entitlements,
sobre mTLS interno) para conceder o plano desse tenant. É best-effort: se falhar, NÃO quebra o
signup — apenas regista; a IA fica indisponível até o entitlement ser escrito (reconciliação
futura). O core NUNCA toca na premium-db; só chama o endpoint token-gated do gateway. O mesmo
endpoint serve, em produção, o webhook de billing (um só escritor para trial e pago).

Stdlib apenas (urllib + ssl) — sem dependências novas no open-core. Reutiliza o cert de
cliente do mesh (core-client) que o core já usa para falar com o sidecar.

Reconciliação: o resultado fica registado na empresa (`plano` pedido +
`plano_provisionado_em` quando o gateway confirmou). Um tick volta a pedir o
plano dos trials recentes que ficaram por confirmar — o upsert do gateway é
idempotente, por isso repetir nunca faz mal.

O core só cria **trials**: o token que tem (`GATEWAY_PROVISION_TOKEN_TRIAL`) só
serve para isso no gateway, e só num tenant ainda sem plano. Um plano pago é do
superadmin, que tem o token completo — um core comprometido (é o serviço exposto
à Internet) não dá planos pagos a ninguém.
"""
import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from app.shared.segredo import ler_segredo

logger = logging.getLogger(__name__)

_PROVISION_URL = os.getenv("GATEWAY_PROVISION_URL", "")          # ex.: https://gateway-ingress:8090
# Chega em ficheiro (GATEWAY_PROVISION_TOKEN_TRIAL_FILE, os `secrets:` do compose).
_PROVISION_TOKEN = ler_segredo("GATEWAY_PROVISION_TOKEN_TRIAL")
_MTLS_CERT = os.getenv("GATEWAY_PROVISION_MTLS_CERT", "/app/mtls/core-client.crt")
_MTLS_KEY = os.getenv("GATEWAY_PROVISION_MTLS_KEY", "/app/mtls/core-client.key")
_MTLS_CA = os.getenv("GATEWAY_PROVISION_MTLS_CA", "/app/mtls/ca.crt")

_ssl_ctx: ssl.SSLContext | None = None


def _contexto_mtls() -> ssl.SSLContext | None:
    """Contexto mTLS (cert de cliente core + verificação da CA do servidor). None se o
    material faltar — e nesse caso não fica em cache: os certificados aparecem
    quando o serviço que os gera acaba, e a chamada seguinte já os usa."""
    global _ssl_ctx
    if _ssl_ctx is not None:
        return _ssl_ctx
    if not (os.path.isfile(_MTLS_CA) and os.path.isfile(_MTLS_CERT) and os.path.isfile(_MTLS_KEY)):
        logger.warning("Material mTLS do provisionamento ausente — provisionamento adiado.")
        return None
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=_MTLS_CA)
    ctx.load_cert_chain(certfile=_MTLS_CERT, keyfile=_MTLS_KEY)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    _ssl_ctx = ctx
    return ctx


def utc_sem_fuso(instante: datetime) -> datetime:
    """Instante em UTC sem fuso — é assim que `empresas` guarda as datas."""
    if instante.tzinfo is not None:
        instante = instante.astimezone(timezone.utc).replace(tzinfo=None)
    return instante


def provisionar_plano(tenant_id, plano: str, timeout: float = 8.0, *, expira_em: datetime | None = None) -> bool:
    """
    Concede `plano` ao tenant via o gateway (escritor único). Devolve True se 2xx.
    **Best-effort: nunca levanta exceções** — o signup não pode quebrar por causa do plano.

    `expira_em` é o fim do trial decidido pela borda: com ela, os direitos do
    trial acabam no mesmo dia em que a conta é suspensa. Sem ela, o gateway
    conta o prazo a partir de agora.
    """
    if not _PROVISION_URL or not _PROVISION_TOKEN:
        logger.info(
            "Provisionamento desligado (GATEWAY_PROVISION_URL/TOKEN_TRIAL vazios) — tenant %s fica sem plano.",
            tenant_id,
        )
        return False
    if plano != "trial" or expira_em is None:
        # Só trials, e sempre com o fim decidido pela borda (o gateway recusa sem ele).
        logger.warning("Provisionamento do tenant %s recusado: o core só cria trials com data de fim.", tenant_id)
        return False

    url = f"{_PROVISION_URL.rstrip('/')}/internal/provision"
    if not url.lower().startswith("https://"):
        # O token viajaria em claro e sem a verificação mTLS do gateway.
        logger.error("GATEWAY_PROVISION_URL tem de ser https:// (mTLS interno) — provisionamento recusado.")
        return False
    ctx = _contexto_mtls()
    if ctx is None:
        return False
    pedido = {"tenant_id": str(tenant_id), "plan": plano}
    pedido["expires_at"] = utc_sem_fuso(expira_em).replace(tzinfo=timezone.utc).isoformat()
    body = json.dumps(pedido).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-Provision-Token": _PROVISION_TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if resp.status in (200, 204):
                logger.info("Tenant %s provisionado (plano '%s').", tenant_id, plano)
                return True
            logger.warning("Provisionamento do tenant %s devolveu %s.", tenant_id, resp.status)
            return False
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            # O tenant já tem outro plano no gateway (o operador converteu-o
            # entretanto): nada a fazer daqui — o superadmin completa o resto.
            logger.info("Tenant %s já tem plano definido no gateway — o trial não se reescreve.", tenant_id)
        else:
            logger.warning("Provisionamento do tenant %s recusado (HTTP %s).", tenant_id, exc.code)
        return False
    except Exception as exc:  # noqa: BLE001 — best-effort, nunca quebra o signup
        logger.warning(
            "Provisionamento do tenant %s falhou (%s) — IA indisponível até reconciliar.",
            tenant_id, exc,
        )
        return False


def provisionar_e_registar(db, empresa, plano: str, *, expira_em: datetime | None = None) -> bool:
    """Pede o plano ao gateway e, se confirmado, carimba a empresa.

    Best-effort como `provisionar_plano`: nunca levanta. O carimbo é o que
    permite ao tick distinguir "confirmado" de "por reconciliar".
    """
    if provisionar_plano(empresa.id, plano, expira_em=expira_em):
        empresa.plano_provisionado_em = datetime.now(timezone.utc)
        db.add(empresa)
        db.commit()
        return True
    return False


# Só se reconciliam tenants criados há menos de N dias: um plano pedido há meses
# e nunca confirmado é um caso para o operador, não para insistir para sempre.
RECONCILIAR_JANELA_DIAS = 30


def reconciliar_provisionamento(db, *, agora: datetime | None = None) -> int:
    """Volta a pedir ao gateway o trial das empresas cujo provisionamento no
    registo não foi confirmado. Devolve quantas ficaram confirmadas nesta volta.

    Só trials (os outros planos são do superadmin). E o carimbo é condicional:
    se o operador mudou o plano enquanto o pedido estava a caminho, este tick não
    confirma por cima — o gateway recusa o trial (409) e o superadmin completa o
    plano novo."""
    from sqlalchemy import update
    from sqlmodel import select

    from app.empresas.models import Empresa

    agora = agora or datetime.now(timezone.utc)
    limite = agora - timedelta(days=RECONCILIAR_JANELA_DIAS)
    pendentes = db.exec(
        select(Empresa.id, Empresa.trial_expira_em).where(
            Empresa.plano == "trial",
            Empresa.trial_expira_em.is_not(None),  # type: ignore[union-attr]
            Empresa.plano_provisionado_em.is_(None),  # type: ignore[union-attr]
            Empresa.deleted_at.is_(None),  # type: ignore[union-attr]
            Empresa.created_at >= limite,
        )
    ).all()
    confirmadas = 0
    for empresa_id, expira_em in pendentes:
        if not provisionar_plano(empresa_id, "trial", expira_em=expira_em):
            continue
        feito = db.execute(
            update(Empresa)
            .where(
                Empresa.id == empresa_id,
                Empresa.plano == "trial",
                Empresa.trial_expira_em == expira_em,
                Empresa.plano_provisionado_em.is_(None),  # type: ignore[union-attr]
            )
            .values(plano_provisionado_em=agora)
        )
        db.commit()
        confirmadas += feito.rowcount or 0
    if pendentes:
        logger.info(
            "Reconciliação de planos: %d por confirmar, %d confirmadas nesta volta.",
            len(pendentes), confirmadas,
        )
    return confirmadas
