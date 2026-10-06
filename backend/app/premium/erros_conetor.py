"""
Erros do sidecar nas ligações às fontes técnicas e nas verificações.

Sobre a tradução comum (`erros.py`), as ligações acrescentam as recusas de
pré-condição que o ecrã sabe explicar por código. O texto do sidecar é técnico e
só em português; passado tal e qual, era o que aparecia no aviso, também a quem
usa a aplicação em inglês.
"""
from __future__ import annotations

from fastapi import HTTPException

from app.premium.erros import executar_grpc, traduzir_erro_grpc

# Texto da recusa → (estado HTTP, código estável que o ecrã traduz).
_PRECONDICOES = (
    # A instalação não tem a chave que cifra as credenciais: nada se guarda, e
    # não é quem pediu que o resolve — é quem administra o servidor.
    ("CONNECTOR_SECRETS_KEY", 503, "cifra_por_configurar"),
    ("ligacao_em_falta", 409, "ligacao_em_falta"),
    ("so_onprem", 403, "so_onprem"),
    ("desativado", 409, "conetor_desativado"),
    ("não configurado", 409, "conetor_nao_configurado"),
)


def _precondicao_conhecida(codigo, detalhes: str) -> HTTPException | None:
    if getattr(codigo, "name", "") != "FAILED_PRECONDITION":
        return None
    for trecho, estado, codigo_estavel in _PRECONDICOES:
        if trecho in detalhes:
            return HTTPException(status_code=estado, detail={"codigo": codigo_estavel})
    return None


def traduzir_erro_conetor(exc: BaseException) -> HTTPException | None:
    """A resposta HTTP para um erro do sidecar numa ligação, ou `None`.

    A falta de módulo é a da fonte pedida (o sidecar decide por fonte): é o plano
    que não dá, não a pessoa que não pode — 402 com o nome do módulo.
    """
    return traduzir_erro_grpc(
        exc, "conetores", prefixo="conetor", recusa_de_modulo=False,
        extra=_precondicao_conhecida,
    )


def executar_conetor(fn, *args, **kwargs):
    """Faz a chamada ao sidecar numa ligação e traduz os erros em HTTP."""
    return executar_grpc(
        fn, *args, modulo="conetores", prefixo="conetor", recusa_de_modulo=False,
        extra=_precondicao_conhecida, **kwargs,
    )
