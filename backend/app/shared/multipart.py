"""Uploads multipart: quem não pode enviar não chega a pôr o ficheiro no disco.

O FastAPI lê e analisa o corpo de uma rota com parâmetros `File`/`Form` ANTES de
correr as dependências — autenticação e capacidades incluídas. O Starlette
escreve cada ficheiro acima de 1 MiB num temporário em disco. Por isso qualquer
pedido, mesmo anónimo, punha o ficheiro inteiro no disco do backend antes de
levar o 401/403 (medido: 8 MiB em disco, anónimo, antes do 401). Nas rotas com
tetos altos — os backups (4 GiB) e o parecer do auditor (256 MiB) — isso enche o
disco do servidor.

Duas peças:

- `GuardaMultipart` (middleware): um corpo multipart sem um access token válido é
  recusado com 401 sem a aplicação o ler. Vale para todas as rotas de upload; as
  regras do token são as da autenticação (`payload_de_access_token`).
- `formulario_autorizado` (dependência): nas rotas de ficheiros grandes a
  capacidade também tem de passar antes da leitura. Essas rotas não declaram
  `File`/`Form` e recebem o formulário por esta dependência, posta DEPOIS das de
  autorização na assinatura — o FastAPI resolve as dependências por ordem, e o
  corpo só é lido quando todas as anteriores passaram.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import HTTPException, Request, status
from pydantic import TypeAdapter, ValidationError
from starlette.datastructures import FormData, UploadFile


def _cabecalho(scope, nome: bytes) -> bytes | None:
    for chave, valor in scope.get("headers") or []:
        if chave == nome:
            return valor
    return None


def e_multipart(scope) -> bool:
    tipo = _cabecalho(scope, b"content-type") or b""
    return tipo.split(b";", 1)[0].strip().lower() == b"multipart/form-data"


def _token_bearer(scope) -> str | None:
    valor = (_cabecalho(scope, b"authorization") or b"").decode("latin-1")
    esquema, _, token = valor.partition(" ")
    if esquema.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


class GuardaMultipart:
    """Recusa com 401 um corpo multipart sem access token válido, sem o guardar.

    `validar` recebe o token e levanta `HTTPException` se não servir; `detalhe`
    dá, a partir do `scope`, o texto da recusa na língua de quem pede.

    Às vezes o corpo é lido e deitado fora antes do 401 (aos bocados: nem disco
    nem memória), porque fechar a ligação com o nginx a meio de o passar faz o
    nginx responder 502 em vez do 401 (medido: 1 vez em 3 com 64 MiB) — e o
    browser só renova a sessão num 401. Lê-se:
      - um corpo declarado até `drenar_ate` bytes (as rotas gerais, onde o nginx
        já tem o corpo inteiro e o passa depressa);
      - qualquer corpo com um access token nosso que só expirou (`so_expirado`):
        é alguém com sessão, que tem de receber o 401 para a renovar.
    Um anónimo com um corpo grande leva o corte da ligação sem leitura.
    """

    def __init__(
        self,
        app,
        validar: Callable[[str], Any],
        detalhe: Callable[[Any, str], str],
        drenar_ate: int = 0,
        so_expirado: Callable[[str], bool] = lambda token: False,
    ) -> None:
        self.app = app
        self.validar = validar
        self.detalhe = detalhe
        self.drenar_ate = drenar_ate
        self.so_expirado = so_expirado

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not e_multipart(scope):
            await self.app(scope, receive, send)
            return
        token = _token_bearer(scope)
        motivo = "Não autenticado."
        tem_sessao = False
        if token is not None:
            try:
                self.validar(token)
            except HTTPException as exc:
                motivo = str(exc.detail)
                tem_sessao = self.so_expirado(token)
            else:
                await self.app(scope, receive, send)
                return
        drenado = await self._drenar(scope, receive, sem_teto=tem_sessao)
        await self._recusar(scope, send, motivo, fechar=not drenado)

    async def _drenar(self, scope, receive, sem_teto: bool) -> bool:
        """Lê e deita fora o corpo. False se não o leu todo (grande, ou sem tamanho
        declarado, para quem não tem sessão). O teto de corpo da rota (o
        `LimiteDeCorpo`, por fora desta guarda) continua a valer."""
        teto = None if sem_teto else self.drenar_ate
        if teto is not None:
            try:
                declarado = int(_cabecalho(scope, b"content-length") or b"-1")
            except ValueError:
                return False
            if declarado < 0 or declarado > teto:
                return False
        lidos = 0
        while True:
            mensagem = await receive()
            if mensagem["type"] != "http.request":
                return False
            lidos += len(mensagem.get("body", b""))
            if teto is not None and lidos > teto:
                return False
            if not mensagem.get("more_body", False):
                return True

    async def _recusar(self, scope, send, motivo: str, fechar: bool) -> None:
        corpo = json.dumps({"detail": self.detalhe(scope, motivo)}, ensure_ascii=False).encode("utf-8")
        cabecalhos = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(corpo)).encode("ascii")),
            (b"www-authenticate", b"Bearer"),
        ]
        if fechar:
            # O resto do corpo não vai ser lido: a ligação não serve para outro pedido.
            cabecalhos.append((b"connection", b"close"))
        await send({"type": "http.response.start", "status": status.HTTP_401_UNAUTHORIZED,
                    "headers": cabecalhos})
        await send({"type": "http.response.body", "body": corpo})


def formulario_autorizado(max_ficheiros: int = 1, max_campos: int = 4):
    """Dependência que lê o multipart só depois das dependências anteriores.

    Tem de ficar na assinatura DEPOIS do utilizador, da sessão e dos portões de
    capacidade. Os temporários dos ficheiros fecham-se no fim do pedido.
    """

    async def _ler(request: Request):
        formulario = await request.form(max_files=max_ficheiros, max_fields=max_campos)
        try:
            yield formulario
        finally:
            await formulario.close()

    return _ler


def _em_falta(campo: str) -> HTTPException:
    # A mesma forma que o FastAPI dá a um campo obrigatório em falta.
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=[{"type": "missing", "loc": ["body", campo], "msg": "Field required"}],
    )


def ficheiro_do_formulario(formulario: FormData, campo: str = "ficheiro") -> UploadFile:
    valor = formulario.get(campo)
    if not isinstance(valor, UploadFile):
        raise _em_falta(campo)
    return valor


_BOOLEANO = TypeAdapter(bool)


def booleano_do_formulario(formulario: FormData, campo: str, omissao: bool = False) -> bool:
    """Um campo booleano de formulário, com as mesmas regras do `Form(bool)`."""
    valor = formulario.get(campo)
    if valor is None:
        return omissao
    if isinstance(valor, UploadFile):
        raise _em_falta(campo)
    try:
        return _BOOLEANO.validate_python(valor)
    except ValidationError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[{"type": "bool_parsing", "loc": ["body", campo],
                     "msg": "Input should be a valid boolean"}],
        )
