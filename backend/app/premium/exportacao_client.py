"""Cliente da exportação dos dados premium de um tenant (`ExportacaoService`).

O sidecar manda os ficheiros um a um, aos bocados: o nome de um ficheiro e a
seguir o conteúdo dele. Este cliente só os passa adiante, sem os guardar — quem
os embala num zip é `app.premium.exportacao`.

Não há portão de licença deste lado nem do outro: os dados são do cliente e
saem em qualquer estado da licença. Quem pode exportar decide-o a rota.
"""
from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from app.config import get_settings
from app.premium.client import ClienteSidecar


class ExportacaoClient(ClienteSidecar):
    _NOME_STUB = "ExportacaoServiceStub"

    # Prazo da exportação inteira. Uma PME exporta em segundos; o prazo só
    # impede que um sidecar pendurado prenda o pedido (e a ligação à base que a
    # exportação segura do lado dele) para sempre.
    PRAZO_S = 1800.0

    def partes_de(self, tenant_id: str) -> Iterator[tuple[str, str | bytes]]:
        """As partes do stream: `("ficheiro", nome)` ou `("dados", bytes)`.

        Um erro do sidecar sai quando se pede a parte seguinte (o primeiro, ao
        pedir a primeira). Fechar o iterador a meio cancela o pedido."""
        from app.premium.proto import premium_pb2  # type: ignore

        chamada = self._ensure_stub().ExportarDadosTenant(
            premium_pb2.ExportacaoTenantReq(tenant_id=tenant_id), timeout=self.PRAZO_S
        )
        try:
            for parte in chamada:
                qual = parte.WhichOneof("parte")
                if qual == "ficheiro":
                    yield ("ficheiro", parte.ficheiro)
                elif qual == "dados":
                    yield ("dados", parte.dados)
        finally:
            # Quem desistiu a meio (o browser fechou o download): o sidecar pára
            # e larga a ligação à base em vez de ler até ao fim para ninguém.
            cancelar = getattr(chamada, "cancel", None)
            if callable(cancelar):
                cancelar()


@lru_cache
def get_exportacao_client() -> ExportacaoClient | None:
    """Singleton do cliente, ou None se o premium não estiver configurado."""
    settings = get_settings()
    if not settings.PREMIUM_ENABLED or not settings.PREMIUM_SIDECAR_ADDR:
        return None
    return ExportacaoClient(settings.PREMIUM_SIDECAR_ADDR)
