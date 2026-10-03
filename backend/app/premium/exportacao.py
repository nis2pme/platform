"""Exportação dos dados premium: do stream do sidecar para um zip, em stream.

Os dados dos módulos premium são do cliente e saem em qualquer estado da
licença (em dia, só de leitura, revogada, expirada, sem licença). O sidecar
manda os ficheiros aos bocados; aqui entram num zip à medida que chegam e o zip
sai para o browser à medida que se faz — nada se junta em memória, por maior
que seja a empresa.

O zip escreve-se num destino sem `seek`: o `zipfile` põe o tamanho de cada
ficheiro num descritor a seguir aos dados, e o índice no fim. Um erro a meio
(o sidecar passou do teto, caiu) não deixa um zip partido: fecha-se com um
ficheiro que diz que a exportação ficou incompleta e porquê.
"""
from __future__ import annotations

import logging
import re
import zipfile
from collections import deque
from collections.abc import Iterable, Iterator
from itertools import chain

from fastapi import HTTPException, status

from app.premium.client import e_indisponibilidade

logger = logging.getLogger(__name__)

# Pasta de topo dentro do zip: extrair não espalha ficheiros onde se abriu.
PASTA = "dados_premium"
AVISO_INCOMPLETA = "ERRO-EXPORTACAO-INCOMPLETA.txt"

# Nomes que o sidecar pode mandar: caminhos relativos, sem `..`, só com letras,
# algarismos, `_` e `-`, e uma extensão conhecida. Um nome fora disto não entra
# no zip (quem extrai não pode ser levado a escrever fora da pasta).
_NOME = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.(?:json|csv|txt)$")
_NOME_MAX = 160

# Códigos estáveis do sidecar que podem seguir no aviso (nunca texto livre).
_CODIGOS_CONHECIDOS = frozenset({"exportacao_excede_teto", "exportacao_ocupada"})


class _Destino:
    """Um ficheiro só de escrita, sem `seek` nem `tell`: o que se escreve fica
    à espera de ser tirado e enviado."""

    def __init__(self) -> None:
        self._partes: deque[bytes] = deque()

    def write(self, dados) -> int:
        self._partes.append(bytes(dados))
        return len(dados)

    def flush(self) -> None:
        pass

    def tirar(self) -> bytes:
        dados = b"".join(self._partes)
        self._partes.clear()
        return dados


def nome_seguro(nome: str) -> str:
    """O caminho dentro do zip, ou ValueError se o nome não é aceitável."""
    if not isinstance(nome, str) or len(nome) > _NOME_MAX or ".." in nome or not _NOME.match(nome):
        raise ValueError(f"nome de ficheiro recusado: {nome[:60]!r}")
    return f"{PASTA}/{nome}"


def _codigo_do_erro(exc: BaseException) -> str:
    detalhes = ""
    try:
        detalhes = (exc.details() or "").strip()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — não é um erro do gRPC
        pass
    if detalhes in _CODIGOS_CONHECIDOS:
        return detalhes
    if e_indisponibilidade(exc):
        return "premium_indisponivel"
    if isinstance(exc, ValueError):
        return "nome_de_ficheiro_recusado"
    return "erro_interno"


def _texto_do_aviso(codigo: str) -> str:
    return (
        "A exportação ficou incompleta: os ficheiros acima estão inteiros, mas "
        "faltam os que vinham a seguir.\n"
        f"Motivo: {codigo}\n"
        "Tente de novo; se voltar a acontecer, fale com o administrador da "
        "plataforma (o registo do serviço premium diz o que se passou).\n\n"
        "The export is incomplete: the files above are whole, but the ones that "
        "came after are missing.\n"
        f"Reason: {codigo}\n"
        "Try again; if it happens again, contact the platform administrator (the "
        "premium service log says what happened).\n"
    )


def zip_em_stream(partes: Iterable[tuple[str, str | bytes]]) -> Iterator[bytes]:
    """O zip, aos bocados, a partir das partes do sidecar.

    Um erro a meio fecha o zip com o aviso de incompleta; quem desiste a meio
    (o gerador é fechado) fecha também as partes, e com isso o pedido ao sidecar.
    """
    destino = _Destino()
    zf = zipfile.ZipFile(destino, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6)
    atual = None
    iterador = iter(partes)
    try:
        erro: BaseException | None = None
        try:
            for tipo, valor in iterador:
                if tipo == "ficheiro":
                    if atual is not None:
                        atual.close()
                    atual = None
                    atual = zf.open(nome_seguro(valor), "w", force_zip64=True)  # type: ignore[arg-type]
                elif tipo == "dados":
                    if atual is None:
                        raise ValueError("dados antes do nome do ficheiro")
                    atual.write(valor)  # type: ignore[arg-type]
                bocado = destino.tirar()
                if bocado:
                    yield bocado
        except GeneratorExit:
            raise
        except Exception as exc:  # noqa: BLE001 — fecha-se o zip com o aviso
            erro = exc
        if atual is not None:
            atual.close()
            atual = None
        if erro is not None:
            codigo = _codigo_do_erro(erro)
            logger.warning("Exportação premium incompleta (%s): %s", codigo, erro)
            zf.writestr(f"{PASTA}/{AVISO_INCOMPLETA}", _texto_do_aviso(codigo))
        zf.close()
        resto = destino.tirar()
        if resto:
            yield resto
    finally:
        fechar = getattr(iterador, "close", None)
        if callable(fechar):
            fechar()


def abrir(partes: Iterator[tuple[str, str | bytes]]) -> Iterator[tuple[str, str | bytes]]:
    """Pede a primeira parte já, para uma recusa do sidecar (ocupado, em baixo)
    chegar ao cliente como um erro HTTP e não como um zip com um aviso."""
    try:
        primeira = next(partes)
    except StopIteration:
        return iter(())
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — traduzido a seguir
        detalhes = ""
        try:
            detalhes = (exc.details() or "").strip()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        if detalhes == "exportacao_ocupada":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"codigo": "exportacao_ocupada"},
                headers={"Retry-After": "30"},
            ) from exc
        codigo = exc.code() if callable(getattr(exc, "code", None)) else None  # type: ignore[attr-defined]
        if e_indisponibilidade(exc) or getattr(codigo, "name", "") in ("FAILED_PRECONDITION", "UNIMPLEMENTED"):
            # Sidecar em baixo, sem a base dele, ou de uma versão que ainda não
            # exporta (imagens atualizadas fora de ordem): o módulo está
            # indisponível, não é um defeito da plataforma.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"codigo": "premium_indisponivel"},
            ) from exc
        raise
    return chain([primeira], partes)
