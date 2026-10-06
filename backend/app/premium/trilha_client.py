"""Entrega as cabeças da trilha de auditoria ao sidecar (testemunho externo).

O sidecar leva-as no heartbeat seguinte, assinadas pela instalação; o serviço de
licenças do fornecedor guarda-as e devolve-as no recibo. Esta chamada devolve o
que o fornecedor testemunhou da última vez.

Fail-soft, como as leituras premium: sem sidecar (instalação sem premium) não há
a quem entregar, e uma falha passageira não pode travar o tick nem a trilha.
"""
from __future__ import annotations

import logging

from app.premium import client as premium_client
from app.premium.client import ClienteSidecar

logger = logging.getLogger(__name__)


class TrilhaClient(ClienteSidecar):
    _NOME_STUB = "PremiumProviderStub"

    def testemunhar(self, cabecas: list[dict]):
        from app.premium.proto import premium_pb2  # type: ignore

        return self._ensure_stub().TestemunharTrilha(
            premium_pb2.TestemunharTrilhaReq(
                cabecas=[premium_pb2.CabecaTrilha(**c) for c in cabecas]
            ),
            timeout=15,
        )


def testemunhar(cabecas: list[dict]) -> dict | None:
    """`{"testemunhadas": [...], "testemunhadas_em": str, "ativo": bool}`, ou
    None sem sidecar configurado ou sem resposta."""
    from app.config import get_settings

    settings = get_settings()
    if not settings.PREMIUM_ENABLED or not settings.PREMIUM_SIDECAR_ADDR:
        return None
    try:
        resp = premium_client.cliente_partilhado(
            TrilhaClient, settings.PREMIUM_SIDECAR_ADDR
        ).testemunhar(cabecas)
    except Exception as erro:  # noqa: BLE001 — fail-soft: o tick tenta na hora seguinte
        logger.warning("Testemunho da trilha: o sidecar não respondeu (%s).", erro)
        return None
    return {
        "testemunhadas": [
            {"ambito": c.ambito, "sequencia": c.sequencia, "head_hash": c.head_hash}
            for c in resp.testemunhadas
        ],
        "testemunhadas_em": resp.testemunhadas_em,
        "ativo": resp.ativo,
    }
