"""
Router da política de capacidades — definições de permissões da empresa.

Prefixo base: /api (incluído em main.py)
Prefixo do router: /politica
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.politica import service
from app.politica.schemas import (
    AlterarCelulasSchema,
    AlterarInterruptorSchema,
    MatrizSchema,
    PoliticaSchema,
)
from app.shared.capacidades import ORDEM_PAPEIS, ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, SessionDep

router = APIRouter(prefix="/politica", tags=["Política de permissões"])

LeituraDep = Depends(require_capability("politica", ClasseAcao.VER))
EdicaoDep = Depends(require_capability("politica", ClasseAcao.GOVERNAR))


@router.get(
    "",
    response_model=PoliticaSchema,
    summary="Política de permissões da empresa",
    dependencies=[LeituraDep],
)
def obter_politica(utilizador: CurrentUserDep):
    """Estado atual de cada interruptor."""
    return PoliticaSchema(
        interruptores=service.estado_da_politica(utilizador.empresa_id)
    )


# ── Edição célula a célula ──────────────────────────────────────────────────────
#
# As duas vistas são o MESMO estado visto de duas maneiras: a mesma tabela, o
# mesmo leitor e — o que interessa — os mesmos invariantes, validados no
# servidor. Este ecrã não traz validação própria: se um dia divergissem, ganhava
# a que estivesse mais à frente do pedido, e não é a do ecrã.
#
# Declaradas ANTES da rota do interruptor: essa tem um parâmetro de um segmento
# e, vindo primeiro, apanhava também /matriz.


@router.get(
    "/matriz",
    response_model=MatrizSchema,
    summary="A matriz de capacidades desta empresa, célula a célula",
    dependencies=[LeituraDep],
)
def obter_matriz(utilizador: CurrentUserDep):
    """A matriz inteira: o que vale, o que a plataforma trazia, e o que se mexe."""
    return MatrizSchema(
        papeis=[papel.value for papel in ORDEM_PAPEIS],
        modulos=service.matriz_para_edicao(utilizador.empresa_id),
    )


@router.put(
    "/matriz",
    response_model=MatrizSchema,
    summary="Alterar células da matriz",
)
def alterar_matriz(
    dados: AlterarCelulasSchema,
    request: Request,
    db: SessionDep,
    utilizador=EdicaoDep,
):
    """
    Aplica um lote de alterações e devolve a matriz inteira.

    Em lote porque os invariantes são propriedades da matriz toda: separadas, uma
    alteração que só é válida acompanhada de outra seria recusada. Tudo passa, ou
    nada é gravado.
    """
    return MatrizSchema(
        papeis=[papel.value for papel in ORDEM_PAPEIS],
        modulos=service.aplicar_celulas(db, utilizador, dados.alteracoes, request),
    )


@router.put(
    "/{chave}",
    response_model=PoliticaSchema,
    summary="Ligar ou desligar um interruptor",
)
def alterar_interruptor(
    chave: str,
    dados: AlterarInterruptorSchema,
    request: Request,
    db: SessionDep,
    utilizador=EdicaoDep,
):
    """
    Aplica o interruptor e devolve a política inteira.

    Devolve tudo, e não só o interruptor tocado: dois interruptores podem
    partilhar uma célula, e o ecrã tem de ficar a ver o estado real de todos.
    """
    interruptores = service.aplicar_interruptor(
        db, utilizador, chave, dados.ligar, request
    )
    return PoliticaSchema(interruptores=interruptores)
