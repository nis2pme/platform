"""
Ajudantes comuns aos routers dos módulos premium: o que é feito ao PEDIDO antes
de ele seguir para o sidecar.
"""
from __future__ import annotations

import uuid
from typing import Annotated, Any, Container, TypeVar

from fastapi import HTTPException, Request
from pydantic import BaseModel, create_model

_C = TypeVar("_C")


def cliente_ou_503(cliente: _C | None) -> _C:
    """O cliente do módulo, ou 503 se o premium está desligado nesta instalação."""
    if cliente is None:
        raise HTTPException(status_code=503, detail={"codigo": "premium_indisponivel"})
    return cliente


def locale_do_pedido(request: Request) -> str:
    """Idioma do pedido para os textos que o sidecar compõe (o frontend envia
    Accept-Language): «en» ou «pt-PT», que são os que o sidecar conhece."""
    lang = (request.headers.get("Accept-Language") or "").lower()
    return "en" if lang.startswith("en") else "pt-PT"


def uuid_ou_none(valor) -> uuid.UUID | None:
    try:
        return uuid.UUID(valor)
    except (ValueError, TypeError, AttributeError):
        return None


def todos_opcionais(
    modelo: type[BaseModel],
    nome: str | None = None,
    *,
    anulaveis: Container[str] | None = None,
) -> type[BaseModel]:
    """O modelo de um PATCH: os mesmos campos de `modelo`, todos opcionais.

    Deriva-se do modelo de criação para que os dois não possam divergir: um campo
    novo no `…In` passa a poder ser alterado por PATCH sem mais nada, e com os
    mesmos tetos. Um campo que não vem no corpo fica «não definido»
    (`exclude_unset`), que não é o mesmo que vir vazio.

    `anulaveis` são os campos que aceitam `null` explícito (quem chama decide o que
    quer dizer); os outros recusam-no com 422. Sem ele, todos o aceitam.
    """
    campos: dict[str, Any] = {}
    for n, f in modelo.model_fields.items():
        tipo = f.annotation | None if anulaveis is None or n in anulaveis else f.annotation
        # Os tetos (`max_length`…) vivem nos metadados do campo e levam-se com ele.
        campos[n] = (Annotated[(tipo, *f.metadata)] if f.metadata else tipo, None)
    return create_model(nome or f"{modelo.__name__}Parcial", **campos)


def fundir_alteracoes(
    atual: dict,
    alteracoes: dict,
    *,
    pares: tuple[tuple[str, ...], ...] = (),
) -> dict:
    """O registo `atual` com só o que veio em `alteracoes`.

    É a regra de um PATCH, numa só função: o que não vem no corpo mantém-se, e o
    que vem — mesmo vazio — substitui. Um registo que o sidecar grava por
    substituição apagaria tudo o que o corpo não mencionasse; por isso o router
    manda o resultado desta função e não o corpo.

    `pares` são campos que só fazem sentido juntos (um identificador e o nome que
    lhe corresponde): se o corpo menciona algum, os outros do par deixam de valer
    o que tinham — o nome antigo era o da pessoa que lá estava — e ficam vazios
    quando o corpo não os traz. Os campos de um par são textos.

    Não altera `atual` nem `alteracoes`.
    """
    fundido = dict(atual)
    for par in pares:
        if any(campo in alteracoes for campo in par):
            for campo in par:
                fundido[campo] = ""
    fundido.update(alteracoes)
    return fundido
