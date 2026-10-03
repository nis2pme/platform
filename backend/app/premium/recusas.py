"""Recusas do sidecar que são sobre a licença, e não sobre o pedido.

Enquanto a cache de direitos do núcleo ainda não viu uma mudança de licença, é
o sidecar quem recusa. Estas duas recusas chegam ao cliente com o mesmo código
que o gate do núcleo (`require_feature`) dá, em todos os módulos:

- `licenca_so_leitura` → 403 `licenca_so_leitura`;
- `sem_direito_modulo:<feature>` → 402 `premium_inativo`.

Sem isto saíam como «estado inválido» (409), «indisponível» (503) ou «sem
permissão» (403), e mandavam procurar o problema no sítio errado.
"""
from __future__ import annotations

import re

from fastapi import HTTPException

LICENCA_SO_LEITURA = "licenca_so_leitura"
PREFIXO_MODULO = "sem_direito_modulo:"
_FEATURE = re.compile(r"^[a-z0-9_]{1,40}$")


def recusa_de_licenca(exc: BaseException, *, incluir_modulo: bool = True) -> HTTPException | None:
    """A resposta HTTP para uma recusa de licença do sidecar, ou `None`."""
    try:
        import grpc  # type: ignore
    except ImportError:
        return None
    if not isinstance(exc, grpc.RpcError):
        return None
    detalhes = (exc.details() or "").strip()
    codigo = exc.code()
    if codigo == grpc.StatusCode.FAILED_PRECONDITION and detalhes == LICENCA_SO_LEITURA:
        return HTTPException(status_code=403, detail={"codigo": LICENCA_SO_LEITURA})
    if incluir_modulo and codigo == grpc.StatusCode.PERMISSION_DENIED and detalhes.startswith(PREFIXO_MODULO):
        detalhe: dict = {"codigo": "premium_inativo"}
        feature = detalhes[len(PREFIXO_MODULO):].strip()
        # Só um nome de feature bem formado segue para o cliente, nunca texto livre.
        if _FEATURE.match(feature):
            detalhe["feature"] = feature
        return HTTPException(status_code=402, detail=detalhe)
    return None
