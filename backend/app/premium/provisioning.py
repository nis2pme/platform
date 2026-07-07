"""
Provisionamento de entitlements no signup (SaaS) — best-effort.

Quando o core cria um tenant em modo saas, pede ao GATEWAY (o escritor ÚNICO dos entitlements,
sobre mTLS interno) para conceder o plano desse tenant. É best-effort: se falhar, NÃO quebra o
signup — apenas regista; a IA fica indisponível até o entitlement ser escrito (reconciliação
futura). O core NUNCA toca na premium-db; só chama o endpoint token-gated do gateway. O mesmo
endpoint serve, em produção, o webhook de billing (um só escritor para trial e pago).

Stdlib apenas (urllib + ssl) — sem dependências novas no open-core. Reutiliza o cert de
cliente do mesh (core-client) que o core já usa para falar com o sidecar.
"""
import json
import logging
import os
import ssl
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_PROVISION_URL = os.getenv("GATEWAY_PROVISION_URL", "")          # ex.: https://gateway-ingress:8090
_PROVISION_TOKEN = os.getenv("GATEWAY_PROVISION_TOKEN", "")
_MTLS_CERT = os.getenv("GATEWAY_PROVISION_MTLS_CERT", "/app/mtls/core-client.crt")
_MTLS_KEY = os.getenv("GATEWAY_PROVISION_MTLS_KEY", "/app/mtls/core-client.key")
_MTLS_CA = os.getenv("GATEWAY_PROVISION_MTLS_CA", "/app/mtls/ca.crt")

_ssl_ctx: ssl.SSLContext | None = None
_ssl_carregado = False


def _contexto_mtls() -> ssl.SSLContext | None:
    """Contexto mTLS (cert de cliente core + verificação da CA do servidor). None se o material faltar."""
    global _ssl_ctx, _ssl_carregado
    if _ssl_carregado:
        return _ssl_ctx
    _ssl_carregado = True
    if not (os.path.isfile(_MTLS_CA) and os.path.isfile(_MTLS_CERT) and os.path.isfile(_MTLS_KEY)):
        logger.warning("Material mTLS do provisionamento ausente — chamada ao gateway sem mTLS.")
        _ssl_ctx = None
        return None
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=_MTLS_CA)
    ctx.load_cert_chain(certfile=_MTLS_CERT, keyfile=_MTLS_KEY)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    _ssl_ctx = ctx
    return ctx


def provisionar_plano(tenant_id, plano: str, timeout: float = 8.0) -> bool:
    """
    Concede `plano` ao tenant via o gateway (escritor único). Devolve True se 2xx.
    **Best-effort: nunca levanta exceções** — o signup não pode quebrar por causa do plano.
    """
    if not _PROVISION_URL or not _PROVISION_TOKEN:
        logger.info(
            "Provisionamento desligado (GATEWAY_PROVISION_URL/TOKEN vazios) — tenant %s fica sem plano.",
            tenant_id,
        )
        return False

    url = f"{_PROVISION_URL.rstrip('/')}/internal/provision"
    body = json.dumps({"tenant_id": str(tenant_id), "plan": plano}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-Provision-Token": _PROVISION_TOKEN},
    )
    ctx = _contexto_mtls() if url.lower().startswith("https") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if resp.status in (200, 204):
                logger.info("Tenant %s provisionado (plano '%s').", tenant_id, plano)
                return True
            logger.warning("Provisionamento do tenant %s devolveu %s.", tenant_id, resp.status)
            return False
    except Exception as exc:  # noqa: BLE001 — best-effort, nunca quebra o signup
        logger.warning(
            "Provisionamento do tenant %s falhou (%s) — IA indisponível até reconciliar.",
            tenant_id, exc,
        )
        return False
