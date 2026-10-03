"""Leva a anonimização de uma pessoa aos dados do sidecar premium.

O sidecar guarda cópias do nome de quem é responsável por um ativo, dono de um
risco, avaliador de um fornecedor, autor de uma importação, quem decidiu um
alerta. Anonimizar a conta só no core deixava o nome em todas essas linhas.

Ao contrário das outras chamadas premium, esta NÃO é fail-soft: se o sidecar
estiver configurado e não responder, quem chama tem de parar — um apagamento a
pedido do titular que diz «feito» sem o ter feito é pior do que um erro.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class SidecarIndisponivel(Exception):
    """O sidecar está configurado mas a anonimização não chegou lá."""


def anonimizar_pessoa(tenant_id: str, utilizador_id: str, nome_substituto: str) -> int:
    """Troca o nome da pessoa em todas as cópias do sidecar. Devolve as linhas
    alteradas; 0 quando o premium não está configurado (não há cópias)."""
    from app.config import get_settings

    settings = get_settings()
    if not settings.PREMIUM_ENABLED or not settings.PREMIUM_SIDECAR_ADDR:
        return 0
    try:
        import grpc

        from app.premium.client import criar_canal_sidecar
        from app.premium.proto import premium_pb2, premium_pb2_grpc

        canal = criar_canal_sidecar(grpc, settings.PREMIUM_SIDECAR_ADDR)
        try:
            resp = premium_pb2_grpc.PurgaServiceStub(canal).AnonimizarPessoa(
                premium_pb2.AnonimizarPessoaReq(
                    tenant_id=tenant_id,
                    utilizador_id=utilizador_id,
                    nome_substituto=nome_substituto,
                ),
                timeout=30,
            )
        finally:
            canal.close()
    except Exception as erro:  # noqa: BLE001 — qualquer falha trava a anonimização
        logger.error("Anonimização no sidecar falhou (tenant=%s): %s", tenant_id, erro)
        raise SidecarIndisponivel(str(erro)) from erro
    logger.info("Anonimização no sidecar: %s linhas (tenant=%s).", resp.linhas_alteradas, tenant_id)
    return int(resp.linhas_alteradas)
