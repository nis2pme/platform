"""
Cliente da pesquisa premium (seam do core para o sidecar).

Contrato: `PesquisaService.Pesquisar` — UMA chamada devolve ativos + riscos +
fornecedores já filtrados pelos direitos do tenant (o sidecar é a autoridade
dos entitlements) e limitados.

Robustez (decisão de desenho): NÃO se faz uma sondagem de saúde antes de cada
pesquisa — isso duplicaria os round-trips a cada tecla. Faz-se a chamada com
timeout curto e, se falhar, devolve-se vazio (o core mostra os SEUS resultados
na mesma) e marca-se o sidecar como indisponível durante um curto período, para
não o martelar a cada tecla seguinte.
"""
import logging
import threading
import time

from app.config import get_settings
from app.pesquisa.schemas import ResultadoPesquisaSchema

logger = logging.getLogger(__name__)
settings = get_settings()

# Orçamento por pesquisa: acima disto não vale a pena esperar num type-ahead.
TIMEOUT_S = 1.0
# Quanto tempo se ignora o sidecar depois de uma falha (circuit breaker).
ARREFECIMENTO_S = 30.0

_lock = threading.Lock()
_indisponivel_ate: float = 0.0


def _em_arrefecimento() -> bool:
    with _lock:
        return time.monotonic() < _indisponivel_ate


def _marcar_indisponivel() -> None:
    global _indisponivel_ate
    with _lock:
        _indisponivel_ate = time.monotonic() + ARREFECIMENTO_S


def repor_circuito() -> None:
    """Fecha o circuito (usado em testes)."""
    global _indisponivel_ate
    with _lock:
        _indisponivel_ate = 0.0


def sidecar_configurado() -> bool:
    """O sidecar EXISTE nesta instalação? (só configuração, sem rede)"""
    return bool(settings.PREMIUM_ENABLED and settings.PREMIUM_SIDECAR_ADDR)


def pesquisar_premium(
    tenant_id: str, q: str, locale: str = "pt", limite: int = 0
) -> tuple[list[ResultadoPesquisaSchema], bool]:
    """
    Pesquisa no sidecar.

    Returns:
        (resultados, indisponivel) — `indisponivel=True` quando o sidecar existe
        mas não deu resposta útil (o chamador mostra só os resultados do core).
    """
    if not sidecar_configurado():
        return [], False  # instalação sem premium: não é "indisponível", é ausente
    if _em_arrefecimento():
        return [], True

    try:
        import grpc  # noqa: F401  (só existe quando o extra premium está instalado)

        from app.premium.client import criar_canal_sidecar
        from app.premium.proto import premium_pb2, premium_pb2_grpc

        canal = criar_canal_sidecar(grpc, settings.PREMIUM_SIDECAR_ADDR)
        try:
            stub = premium_pb2_grpc.PesquisaServiceStub(canal)
            resp = stub.Pesquisar(
                premium_pb2.PesquisaReq(
                    tenant_id=tenant_id, q=q, locale=locale, limite_por_tipo=limite
                ),
                timeout=TIMEOUT_S,
            )
        finally:
            canal.close()
    except Exception as exc:  # noqa: BLE001 — fail-soft deliberado, ver docstring
        logger.info("pesquisa premium indisponível (%s) — só resultados do core", exc)
        _marcar_indisponivel()
        return [], True

    return (
        [
            ResultadoPesquisaSchema(
                tipo=r.tipo, id=r.id, titulo=r.titulo, subtitulo=r.subtitulo
            )
            for r in resp.resultados
        ],
        False,
    )
