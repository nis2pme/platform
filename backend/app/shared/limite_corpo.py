"""Teto ao tamanho do corpo dos pedidos, imposto antes de alguém o ler para memória.

Middleware ASGI partilhado. O mesmo ficheiro vive, byte a byte, nos outros
serviços HTTP da plataforma; os testes de cada um confirmam que a cópia é igual
à do núcleo.

Porquê na aplicação, se o proxy à frente também corta: quem chegue direto pela
rede interna não passa pelo proxy, e um corpo enviado aos bocados (chunked, sem
Content-Length) só se mede enquanto se lê.

Como recusa (413):
  - pelo Content-Length: antes de a aplicação correr, sem ler um só byte;
  - sem Content-Length: conta os bytes à medida que a aplicação os pede; a
    leitura que passa o teto falha, e o resto do corpo nunca é lido.

A leitura falha com uma HTTPException 413. Numa aplicação FastAPI simples, é a
própria aplicação que a responde, pelo seu tratador de erros. Mas pelo caminho a
exceção pode chegar transformada: o FastAPI faz de qualquer falha na leitura do
corpo um 400 genérico, e os middlewares `@app.middleware("http")` embrulham-na
num ExceptionGroup, que o FastAPI já não reconhece. Por isso o middleware não
confia no que a aplicação responde depois de o teto rebentar: se não for um 413,
troca a resposta pelo seu próprio 413.

Se a aplicação já tiver começado a responder quando o teto rebenta, já não há
413 possível: a exceção sobe e o servidor corta a ligação. Um segundo início de
resposta seria um erro de protocolo, e uma resposta fechada a meio pareceria
completa a quem a recebe.

O teto é por rota: `teto(caminho)` devolve o máximo em bytes para esse caminho.
Uma função, e não uma tabela, para reconhecer também rotas com parâmetros, que o
middleware vê antes do router, por exemplo:

    def teto(caminho: str) -> int:
        if caminho.startswith("/api/upload/"):   # /api/upload/{convite_id}
            return 200 * 1024 * 1024
        return 64 * 1024
"""
from __future__ import annotations

import json
from typing import Any, Callable

from starlette.exceptions import HTTPException


class CorpoDemasiadoGrande(HTTPException):
    """O corpo do pedido passou o teto da rota."""

    def __init__(self, detalhe: Any) -> None:
        super().__init__(status_code=413, detail=detalhe)


def _tamanho_declarado(scope) -> int | None:
    """O Content-Length do pedido; -1 se vier ilegível (conta como acima do teto)."""
    for nome, valor in scope.get("headers") or []:
        if nome == b"content-length":
            try:
                return int(valor)
            except ValueError:
                return -1
    return None


class LimiteDeCorpo:
    """Recusa com 413 um corpo acima de `teto(caminho)` bytes, antes de o ler.

    `detalhe` é o que vai em `detail` na resposta: um valor fixo (um código
    estável, por exemplo) ou uma função que o calcula a partir do `scope` do
    pedido (para responder na língua de quem pede).
    """

    def __init__(self, app, teto: Callable[[str], int], detalhe: Any = None) -> None:
        self.app = app
        self.teto = teto
        self.detalhe = {"codigo": "corpo_grande"} if detalhe is None else detalhe

    def _detalhe(self, scope) -> Any:
        return self.detalhe(scope) if callable(self.detalhe) else self.detalhe

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        maximo = self.teto(scope.get("path", ""))
        declarado = _tamanho_declarado(scope)
        if declarado is not None and (declarado < 0 or declarado > maximo):
            await self._responder_413(scope, send)
            return

        lidos = 0
        excedido = False     # o teto já rebentou
        comecou = False      # a resposta da aplicação já começou a sair
        substituida = False  # a resposta da aplicação foi trocada pelo nosso 413

        async def receber():
            nonlocal lidos, excedido
            if excedido:
                raise CorpoDemasiadoGrande(self._detalhe(scope))
            mensagem = await receive()
            if mensagem["type"] == "http.request":
                lidos += len(mensagem.get("body", b""))
                if lidos > maximo:
                    excedido = True
                    raise CorpoDemasiadoGrande(self._detalhe(scope))
            return mensagem

        async def enviar(mensagem) -> None:
            nonlocal comecou, substituida
            if substituida:
                return
            if mensagem["type"] == "http.response.start":
                if excedido and mensagem["status"] != 413:
                    substituida = True
                    await self._responder_413(scope, send)
                    return
                comecou = True
            await send(mensagem)

        try:
            await self.app(scope, receber, enviar)
        except Exception:
            # Uma exceção que não vem do teto não é connosco. Com a resposta já
            # começada, deixa-se subir: o servidor corta a ligação.
            if not excedido or comecou:
                raise
            if not substituida:
                await self._responder_413(scope, send)

    async def _responder_413(self, scope, send) -> None:
        corpo = json.dumps({"detail": self._detalhe(scope)}, ensure_ascii=False).encode("utf-8")
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
