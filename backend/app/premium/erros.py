"""
Tradução dos erros do sidecar em respostas HTTP — uma só, para todos os módulos.

Um erro gRPC do sidecar é uma conversa que correu bem com um desfecho negativo
(recusa de âmbito, argumento inválido, estado que impede a ação), ou uma falha de
transporte (o sidecar não responde). A resposta certa para cada um é diferente, e
quem usa a plataforma precisa da diferença: renovar a licença, corrigir o pedido,
esperar, ou reportar um defeito.

Os módulos diferem em pouco, e esse pouco entra como parâmetro:
  - `prefixo` dá o nome aos códigos genéricos (`<prefixo>_invalido`, `<prefixo>_erro`);
  - `utilizador` só vem nas escritas: a recusa de âmbito do sidecar deixa rasto;
  - `recusa_de_modulo` diz de que módulo é a recusa «sem direito ao módulo»: do da
    própria rota (402 `premium_inativo`) ou de outro, o de destino (402
    `modulo_em_falta` com o nome, nas ligações e na importação);
  - `extra` é a palavra do módulo sobre os códigos que só ele sabe ler.
"""
from __future__ import annotations

from typing import Callable

from fastapi import HTTPException

from app.premium.atores import registar_recusa_de_recurso
from app.premium.client import (PremiumIndisponivelError, e_indisponibilidade,
                                e_valor_fora_do_contrato)
from app.premium.recusas import PREFIXO_MODULO, recusa_de_licenca

# Um erro que só o módulo sabe ler: recebe o código gRPC e o texto, e devolve a
# resposta, ou `None` para seguir a tradução comum.
Extra = Callable[[object, str], "HTTPException | None"]

# Os módulos de que uma recusa pode vir a falar. Uma lista fechada porque o que sai
# daqui vai para o cliente: sem ela, o sidecar poderia induzir o núcleo a repetir
# texto arbitrário numa resposta HTTP.
MODULOS_CONHECIDOS = {
    "asset_inventory", "risk_analysis",
    # Um conetor por ferramenta de observação (e o do AD, cujo ficheiro
    # também chega por upload).
    "connector_m365", "connector_ad", "connector_gvm", "connector_wazuh",
}

# O sidecar não tem a base de dados: o módulo não está de pé, e não é quem pediu
# que o resolve.
_SEM_BASE = "premium-data-db"


def modulo_em_falta(detalhes: str | None) -> str | None:
    """Nome do módulo em falta, ou `None` se a recusa foi por outra razão."""
    if not detalhes or not detalhes.startswith(PREFIXO_MODULO):
        return None
    modulo = detalhes[len(PREFIXO_MODULO):].strip()
    return modulo if modulo in MODULOS_CONHECIDOS else None


def _indisponivel() -> HTTPException:
    return HTTPException(status_code=503, detail={"codigo": "premium_indisponivel"})


def traduzir_erro_grpc(
    exc: BaseException,
    modulo: str,
    *,
    prefixo: str | None = None,
    utilizador=None,
    recusa_de_modulo: bool = True,
    extra: Extra | None = None,
) -> HTTPException | None:
    """A resposta HTTP para um erro do sidecar, ou `None` se o erro não é dele.

    `None` é o que deixa quem chama voltar a levantar o erro original: um defeito
    nosso não se disfarça de erro do sidecar.
    """
    prefixo = prefixo or modulo
    if isinstance(exc, PremiumIndisponivelError):
        # O sidecar não está utilizável: canal por montar, material de TLS em
        # falta, transporte ausente. É indisponibilidade do módulo, não avaria da
        # plataforma — e a diferença é a que o cliente precisa de ver para saber
        # se age (renovar a licença, verificar a rede) ou se reporta um defeito.
        return _indisponivel()
    if e_valor_fora_do_contrato(exc):
        return HTTPException(status_code=400, detail={"codigo": "valor_fora_de_intervalo"})
    recusa = recusa_de_licenca(exc, incluir_modulo=recusa_de_modulo)
    if recusa is not None:
        return recusa
    try:
        import grpc  # type: ignore
    except ImportError:
        return None
    if not isinstance(exc, grpc.RpcError):
        return None

    codigo = exc.code()
    detalhes = exc.details() or ""
    if extra is not None:
        propria = extra(codigo, detalhes)
        if propria is not None:
            return propria

    if e_indisponibilidade(exc):
        # Sidecar em baixo ou pendurado. O 502 dizia «o upstream respondeu mal»;
        # aqui não respondeu de todo. Sem isto, a mesma avaria saía 502 ou 503
        # conforme a cache de entitlements estivesse quente — e um alerta não se
        # constrói sobre isso.
        return _indisponivel()
    if codigo == grpc.StatusCode.NOT_FOUND:
        return HTTPException(status_code=404, detail={"codigo": "nao_encontrado"})
    if codigo == grpc.StatusCode.PERMISSION_DENIED:
        if not recusa_de_modulo:
            # Falta o módulo de DESTINO (o inventário, o risco, uma ligação) e não a
            # permissão de quem pede: é 402 como qualquer outra funcionalidade em
            # falta, e leva o nome do módulo para o ecrã poder propor o upgrade.
            em_falta = modulo_em_falta(detalhes)
            if em_falta:
                return HTTPException(
                    status_code=402, detail={"codigo": "modulo_em_falta", "modulo": em_falta}
                )
        # Âmbito do ator: o registo não lhe está atribuído. Só as escritas trazem o
        # utilizador, e só elas deixam rasto (as ligações e a importação são de
        # administração, em âmbito total: o sidecar não as recusa por âmbito).
        if utilizador is not None:
            registar_recusa_de_recurso(utilizador, modulo)
        return HTTPException(status_code=403, detail={"codigo": "sem_permissao_recurso"})
    if codigo == grpc.StatusCode.INVALID_ARGUMENT:
        return HTTPException(
            status_code=400, detail={"codigo": f"{prefixo}_invalido", "msg": detalhes}
        )
    if codigo == grpc.StatusCode.FAILED_PRECONDITION:
        if detalhes.startswith(_SEM_BASE):
            return _indisponivel()
        # Estado que impede a ação, corrigível por quem pediu → 409. Um 5xx diria
        # que a plataforma avariou, e não avariou.
        return HTTPException(status_code=409, detail={"codigo": "estado_invalido", "msg": detalhes})
    if codigo == grpc.StatusCode.UNIMPLEMENTED:
        return HTTPException(status_code=501, detail={"codigo": "por_implementar"})
    return HTTPException(status_code=502, detail={"codigo": f"{prefixo}_erro"})


def executar_grpc(fn, *args, modulo: str, prefixo: str | None = None, utilizador=None,
                  recusa_de_modulo: bool = True, extra: Extra | None = None, **kwargs):
    """Faz a chamada ao sidecar e traduz o que correr mal em HTTP."""
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — traduzido abaixo; o resto sobe
        traduzida = traduzir_erro_grpc(
            exc, modulo, prefixo=prefixo, utilizador=utilizador,
            recusa_de_modulo=recusa_de_modulo, extra=extra,
        )
        if traduzida is None:
            raise
        raise traduzida from exc
