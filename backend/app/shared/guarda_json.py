"""Teto de bytes e de estrutura para os corpos JSON, antes de alguém os analisar.

O FastAPI analisa o corpo JSON de uma rota ANTES de correr as dependências da
rota, incluindo a autenticação. Um anónimo que mande a qualquer rota com corpo um
JSON de `{}` repetidos põe o processo a construir milhões de objetos antes de
levar o 401: 8 MiB de `[{},{},…]` chegavam a ~228 MiB de memória (28 vezes o
corpo). O teto geral do corpo (o do nginx, pensado para as evidências enviadas
em multipart) deixava passar isto.

Aqui, e só para os pedidos cujo corpo o FastAPI trataria como JSON — sem
Content-Type, `application/json` ou `application/*+json`:
  - um teto de bytes próprio, muito abaixo do geral: o maior JSON legítimo que o
    frontend envia anda pelos 410 KiB (aplicar uma importação com 5000
    decisões), e as rotas de autenticação recebem poucas centenas de bytes;
  - um teto de estrutura: quantos `{`, `[` e `,` o corpo tem. É a estrutura, e
    não os bytes, que multiplica a memória quando se analisa, e contar estes três
    caracteres é feito em C. As vírgulas dentro de textos também contam; o teto é
    folgado para isso (o mesmo pedido das 5000 decisões tem ~20 000).

O corpo é lido aqui (no máximo o teto) e entregue à aplicação tal e qual; um
corpo recusado nunca chega a ela, por isso a recusa sai daqui (413) e não passa
pelos tratadores da aplicação. Os outros corpos (o multipart das evidências, os
uploads de backups e pareceres) não são analisados como JSON e passam sem
serem tocados: o teto deles é o do `LimiteDeCorpo`, que fica por fora deste.
"""
from __future__ import annotations

import json
from typing import Any, Callable

_ESTRUTURA = (b"{", b"[", b",")


def _cabecalho(scope, nome: bytes) -> bytes | None:
    for chave, valor in scope.get("headers") or []:
        if chave == nome:
            return valor
    return None


def e_corpo_json(scope) -> bool:
    """O FastAPI analisaria este corpo como JSON? A mesma regra que ele usa: sem
    Content-Type, ou `application/json`, ou `application/<algo>+json`."""
    tipo = _cabecalho(scope, b"content-type")
    if tipo is None:
        return True
    principal = tipo.split(b";", 1)[0].strip().lower()
    if not principal.startswith(b"application/"):
        return False
    subtipo = principal[len(b"application/"):]
    return subtipo == b"json" or subtipo.endswith(b"+json")


def _tem_corpo(scope) -> bool:
    """Em HTTP/1.1 um pedido só tem corpo com Content-Length ou Transfer-Encoding."""
    return (
        _cabecalho(scope, b"content-length") not in (None, b"0")
        or _cabecalho(scope, b"transfer-encoding") is not None
    )


def _tamanho_declarado(scope) -> int | None:
    valor = _cabecalho(scope, b"content-length")
    if valor is None:
        return None
    try:
        return int(valor)
    except ValueError:
        return -1


class GuardaJson:
    """Recusa com 413 um corpo JSON acima de `teto(caminho)` bytes ou com mais de
    `estrutura_max` `{`, `[` e `,` — antes de a aplicação o ler.

    `detalhe` é uma função do `scope` que dá o texto da recusa (na língua de quem
    pede), como no `LimiteDeCorpo`.
    """

    def __init__(
        self,
        app,
        teto: Callable[[str], int],
        estrutura_max: int,
        detalhe: Callable[[Any], Any],
    ) -> None:
        self.app = app
        self.teto = teto
        self.estrutura_max = estrutura_max
        self.detalhe = detalhe

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not _tem_corpo(scope) or not e_corpo_json(scope):
            await self.app(scope, receive, send)
            return

        maximo = self.teto(scope.get("path", ""))
        declarado = _tamanho_declarado(scope)
        if declarado is not None and (declarado < 0 or declarado > maximo):
            await self._recusar(scope, send)
            return

        partes: list[bytes] = []
        lidos = 0
        estrutura = 0
        mais = True
        while mais:
            mensagem = await receive()
            if mensagem["type"] != "http.request":
                # O cliente desligou-se a meio: não há a quem responder.
                return
            pedaco = mensagem.get("body", b"")
            lidos += len(pedaco)
            estrutura += sum(pedaco.count(c) for c in _ESTRUTURA)
            if lidos > maximo or estrutura > self.estrutura_max:
                await self._recusar(scope, send)
                return
            partes.append(pedaco)
            mais = mensagem.get("more_body", False)

        corpo = b"".join(partes)
        del partes
        entregue = False

        async def receber():
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        await self.app(scope, receber, send)

    async def _recusar(self, scope, send) -> None:
        corpo = json.dumps({"detail": self.detalhe(scope)}, ensure_ascii=False).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(corpo)).encode("ascii")),
                # O resto do corpo não vai ser lido: a ligação não serve para
                # outro pedido.
                (b"connection", b"close"),
            ],
        })
        await send({"type": "http.response.body", "body": corpo})
