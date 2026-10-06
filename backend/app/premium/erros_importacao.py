"""
Erros do sidecar na importação de ficheiros.

Sobre a tradução comum (`erros.py`), a importação lê os prefixos de recusa que só
ela conhece: o teto do plano, o lote de decisões grande demais, o corpo de erro
estruturado (código + linha) para o ecrã dizer «linha 42: …» no idioma de quem lê.
Tudo o que vem do sidecar é repetido ao cliente, por isso nada aqui aceita texto
livre.
"""
from __future__ import annotations

import json

from fastapi import HTTPException

from app.config import get_settings
from app.premium.erros import executar_grpc, traduzir_erro_grpc

# Prefixo estável da recusa do sidecar por teto de registos do plano.
_PREFIXO_LIMITE = "limite_do_plano:"

# Prefixo estável da recusa por lote de decisões acima do teto.
_PREFIXO_LOTE = "lote_grande:"


def lote_grande(detalhes: str | None) -> int | None:
    """Teto de decisões por pedido, ou `None` se a recusa foi por outra razão."""
    if not detalhes or not detalhes.startswith(_PREFIXO_LOTE):
        return None
    try:
        maximo = int(detalhes[len(_PREFIXO_LOTE):])
    except ValueError:
        return None
    return maximo if maximo > 0 else None


def limite_do_plano(detalhes: str | None) -> tuple[int, int] | None:
    """`(total, limite)` da recusa por teto do plano, ou `None`.

    Só números, e só dois: o que vem do sidecar é repetido ao cliente, por isso
    nada aqui aceita texto livre.
    """
    if not detalhes or not detalhes.startswith(_PREFIXO_LIMITE):
        return None
    partes = detalhes[len(_PREFIXO_LIMITE):].split(":")
    if len(partes) != 2:
        return None
    try:
        total, limite = int(partes[0]), int(partes[1])
    except ValueError:
        return None
    if total < 0 or limite < 0:
        return None
    return total, limite


def corpo_estruturado(detalhes: str | None) -> dict | None:
    """Corpo de erro do sidecar, ou `None` se não vier no formato acordado.

    Devolver `None` — em vez de um código de omissão — é o que permite a quem
    chama distinguir "o sidecar disse-me qual é o problema" de "veio texto que
    não sei ler". Sem essa distinção, uma recusa com nome era indistinguível de
    uma avaria.
    """
    try:
        corpo = json.loads(detalhes or "")
        detalhe = corpo.get("detail")
        if isinstance(detalhe, dict) and detalhe.get("codigo"):
            return detalhe
    except (ValueError, AttributeError):
        pass
    return None


def detalhe_do_sidecar(detalhes: str | None) -> dict:
    """O mesmo corpo, com um código genérico quando não há nada a ler. Nunca
    propaga texto cru do sidecar para o cliente."""
    return corpo_estruturado(detalhes) or {"codigo": "ficheiro_invalido"}


def _recusa_da_importacao(codigo, detalhes: str) -> HTTPException | None:
    nome = getattr(codigo, "name", "")
    if nome == "INVALID_ARGUMENT":
        # Lote de decisões acima do teto. Vem antes do corpo estruturado porque
        # não é um problema do ficheiro: dizer "ficheiro inválido" a quem escolheu
        # máquinas de mais manda-o procurar no sítio errado.
        maximo = lote_grande(detalhes)
        if maximo:
            return HTTPException(
                status_code=400, detail={"codigo": "lote_grande", "maximo": maximo}
            )
        # O sidecar devolve um corpo estruturado (código + linha) para o frontend
        # poder dizer "linha 42: …" no idioma do utilizador.
        return HTTPException(status_code=400, detail=detalhe_do_sidecar(detalhes))
    if nome == "RESOURCE_EXHAUSTED":
        return HTTPException(
            status_code=413,
            detail={"codigo": "ficheiro_grande", "limite_mb": get_settings().IMPORT_MAX_SIZE_MB},
        )
    if nome == "FAILED_PRECONDITION":
        # Teto de registos do plano. É 402 como o módulo em falta — é a mesma
        # família de recusa (o plano não dá) e o ecrã propõe o upgrade em vez de
        # dizer que a plataforma avariou.
        numeros = limite_do_plano(detalhes)
        if numeros:
            total, limite = numeros
            return HTTPException(
                status_code=402,
                detail={"codigo": "limite_do_plano", "total": total, "limite": limite},
            )
        # Travão por confirmar (ex.: T3, relatório mais antigo do que o que já
        # entrou). É uma recusa com resposta possível — o corpo leva o código e os
        # números para o ecrã perguntar.
        travao = corpo_estruturado(detalhes)
        if travao:
            return HTTPException(status_code=409, detail=travao)
    return None


def traduzir_erro_importacao(exc: BaseException) -> HTTPException | None:
    """A resposta HTTP para um erro do sidecar na importação, ou `None`.

    A falta de módulo é a do destino (o inventário, o risco, uma ligação): quem
    escreve num módulo tem de o ter, e o ecrã propõe o upgrade com o nome dele.
    """
    return traduzir_erro_grpc(
        exc, "importacao", recusa_de_modulo=False, extra=_recusa_da_importacao,
    )


def executar_importacao(fn, *args, **kwargs):
    """Faz a chamada ao sidecar na importação e traduz os erros em HTTP."""
    return executar_grpc(
        fn, *args, modulo="importacao", recusa_de_modulo=False,
        extra=_recusa_da_importacao, **kwargs,
    )
