"""Pergunta ao sidecar se há versão nova dos módulos premium (on-prem).

O sidecar pergunta ao fornecedor, assinando com a chave da instalação, e devolve o
manifesto assinado pela raiz e um token de download desta licença. Licença ativa ou
em tolerância: todas as versões; expirada ou revogada: só as de segurança.

Fail-soft: sem sidecar, ou sem resposta, não há atualização a oferecer — nunca um
erro no ecrã.
"""
from __future__ import annotations

import logging

from app.premium import client as premium_client
from app.premium.client import ClienteSidecar

logger = logging.getLogger(__name__)

# O sidecar fala com o fornecedor (desafio + pedido): mais do que uma leitura local.
_PRAZO_S = 90

ESTADOS = frozenset({
    "disponivel", "atualizado", "sem_licenca", "sem_servico", "resposta_invalida", "nao_aplicavel",
})


class AtualizacaoClient(ClienteSidecar):
    _NOME_STUB = "PremiumProviderStub"

    def verificar(self, versao_premium: str, versao_nucleo: str):
        from app.premium.proto import premium_pb2  # type: ignore

        return self._ensure_stub().VerificarAtualizacaoPremium(
            premium_pb2.AtualizacaoPremiumReq(versao_premium=versao_premium, versao_nucleo=versao_nucleo),
            timeout=_PRAZO_S,
        )


def verificar(versao_premium: str, versao_nucleo: str) -> dict:
    """`estado` ∈ ESTADOS ou `indisponivel` (sem sidecar, ou não respondeu). Com
    `disponivel` vêm também versao, seguranca, direito, manifesto, assinatura,
    ficheiro, token e expira_em."""
    from app.config import get_settings

    settings = get_settings()
    if not settings.PREMIUM_ENABLED or not settings.PREMIUM_SIDECAR_ADDR:
        return {"estado": "indisponivel"}
    try:
        resp = premium_client.cliente_partilhado(
            AtualizacaoClient, settings.PREMIUM_SIDECAR_ADDR
        ).verificar(versao_premium, versao_nucleo)
    except Exception as erro:  # noqa: BLE001 — fail-soft: tenta-se na verificação seguinte
        logger.warning("Atualização premium: o sidecar não respondeu (%s).", erro)
        return {"estado": "indisponivel"}
    if resp.estado not in ESTADOS:
        return {"estado": "resposta_invalida"}
    return {
        "estado": resp.estado,
        "versao": resp.versao or None,
        "seguranca": bool(resp.seguranca),
        "direito": resp.direito or None,
        "manifesto": resp.manifesto,
        "assinatura": resp.assinatura,
        "ficheiro": resp.ficheiro,
        "token": resp.token,
        "expira_em": resp.expira_em or None,
    }
