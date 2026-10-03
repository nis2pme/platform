"""
Router do módulo de notificações.

Tudo aqui é do próprio: o âmbito sai do token, não de um parâmetro. É por isso
que estas rotas não passam pela matriz de capacidades — não há nada a autorizar
quando o único conjunto acessível é o de quem está a pedir.

Os indicadores vivem em `/resumo` e aceitam EXATAMENTE os mesmos filtros da
listagem. É isso que faz um número significar o mesmo conjunto que a tabela
mostra, e é um endpoint separado para mudar de página não obrigar a recontar.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from app.database import get_session
from app.notificacoes import service
from app.notificacoes.catalogo import CATALOGO, CATEGORIAS, SEVERIDADES
from app.notificacoes.schemas import (
    CatalogoNotificacoesSchema,
    DefinicaoCodigoSchema,
    FiltrosMarcacaoSchema,
    ListaNotificacoesSchema,
    NotificacaoSchema,
    ResultadoNotificacoesMarcadasSchema,
    ResumoNotificacoesSchema,
)
from app.notificacoes.service import Estado
from app.shared.dependencies import CurrentUserDep

router = APIRouter(prefix="/notificacoes", tags=["Notificações"])

# Entidades a que uma notificação se pode referir. A rota de dispensa por
# entidade aceita só estas: sem lista fechada, o caminho ficava aberto a
# qualquer texto vindo do cliente.
_ENTIDADES = ("Incidente", "Tarefa", "ControloEmpresaV2")


def _validar(categoria: str | None, severidade: str | None) -> None:
    if categoria and categoria not in CATEGORIAS:
        raise HTTPException(
            status_code=422, detail=f"Categoria desconhecida: {categoria}."
        )
    if severidade and severidade not in SEVERIDADES:
        raise HTTPException(
            status_code=422, detail=f"Severidade desconhecida: {severidade}."
        )


def _validar_intervalo(inicio: datetime | None, fim: datetime | None) -> None:
    if inicio is not None and fim is not None and fim < inicio:
        raise HTTPException(
            status_code=400,
            detail="A data final não pode ser anterior à data inicial.",
        )


@router.get(
    "",
    response_model=ListaNotificacoesSchema,
    summary="Listar notificações do utilizador",
)
def listar_notificacoes(
    utilizador: CurrentUserDep,
    estado: Estado = Query("nao_lidas", description="nao_lidas | lidas | todas"),
    categoria: str | None = Query(None),
    severidade: str | None = Query(None),
    q: str | None = Query(None, description="Pesquisa textual simples"),
    data_inicio: datetime | None = Query(None),
    data_fim: datetime | None = Query(None),
    limite: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_session, scope="function"),
):
    """Página de notificações do utilizador autenticado."""
    _validar(categoria, severidade)
    _validar_intervalo(data_inicio, data_fim)

    total, notifs = service.listar(
        db,
        utilizador,
        estado=estado,
        categoria=categoria,
        severidade=severidade,
        q=q,
        data_inicio=data_inicio,
        data_fim=data_fim,
        limite=limite,
        offset=offset,
    )
    return ListaNotificacoesSchema(
        total=total,
        notificacoes=[NotificacaoSchema.de_modelo(n) for n in notifs],
    )


@router.get(
    "/resumo",
    response_model=ResumoNotificacoesSchema,
    summary="Contadores do conjunto filtrado",
)
def obter_resumo(
    utilizador: CurrentUserDep,
    estado: Estado = Query("nao_lidas"),
    categoria: str | None = Query(None),
    severidade: str | None = Query(None),
    q: str | None = Query(None),
    data_inicio: datetime | None = Query(None),
    data_fim: datetime | None = Query(None),
    incluir_controlos: bool = Query(
        False,
        description=(
            "Devolve também os controlos com avisos por ler. Só o ecrã da lista "
            "de controlos precisa disto."
        ),
    ),
    db: Session = Depends(get_session, scope="function"),
):
    """
    Contagens sobre o mesmo conjunto que a listagem devolve com estes filtros,
    mais o número por ler, que é o do sininho e não depende deles.
    """
    _validar(categoria, severidade)
    _validar_intervalo(data_inicio, data_fim)

    dados = service.resumo(
        db,
        utilizador,
        estado=estado,
        categoria=categoria,
        severidade=severidade,
        q=q,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )
    if incluir_controlos:
        dados["controlos_com_notificacoes"] = service.controlos_com_notificacoes(
            db, utilizador
        )
    return ResumoNotificacoesSchema(**dados)


@router.get(
    "/catalogo",
    response_model=CatalogoNotificacoesSchema,
    summary="Vocabulário das notificações",
)
def obter_catalogo():
    """
    Categorias, severidades e códigos que podem aparecer.

    Os filtros do ecrã são construídos a partir daqui. Uma lista escrita à mão
    no frontend deixaria de fora, em silêncio, tudo o que fosse acrescentado.
    """
    return CatalogoNotificacoesSchema(
        categorias=list(CATEGORIAS),
        severidades=list(SEVERIDADES),
        codigos=[
            DefinicaoCodigoSchema(
                codigo=d.codigo,
                categoria=d.categoria,
                severidade=d.severidade,
                entidade_tipo=d.entidade_tipo,
                acionavel=d.acionavel,
            )
            for d in sorted(CATALOGO.values(), key=lambda d: (d.categoria, d.codigo))
        ],
    )


@router.put(
    "/{notificacao_id}/lida",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Marcar notificação como lida",
)
def marcar_lida(
    notificacao_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Marca uma notificação específica como lida."""
    service.marcar_lida(db, notificacao_id, utilizador, lida=True)
    db.commit()


@router.put(
    "/{notificacao_id}/nao-lida",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Repor uma notificação como não lida",
)
def marcar_nao_lida(
    notificacao_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """
    Repõe a notificação na lista por ler.

    "Vi, mas ainda não tratei" é um estado real de quem trabalha com prazos, e
    sem isto a única forma de o representar era não abrir a notificação.
    """
    service.marcar_lida(db, notificacao_id, utilizador, lida=False)
    db.commit()


@router.post(
    "/marcar-lidas",
    response_model=ResultadoNotificacoesMarcadasSchema,
    summary="Marcar como lidas as notificações que correspondem aos filtros",
)
def marcar_lidas_em_lote(
    utilizador: CurrentUserDep,
    filtros: FiltrosMarcacaoSchema | None = None,
    db: Session = Depends(get_session, scope="function"),
):
    """Corpo vazio marca tudo o que está por ler; com filtros, marca só esses."""
    f = filtros or FiltrosMarcacaoSchema()
    _validar(f.categoria, f.severidade)
    _validar_intervalo(f.data_inicio, f.data_fim)

    marcadas = service.marcar_lidas_em_lote(
        db,
        utilizador,
        categoria=f.categoria,
        severidade=f.severidade,
        q=f.q,
        data_inicio=f.data_inicio,
        data_fim=f.data_fim,
    )
    db.commit()
    return ResultadoNotificacoesMarcadasSchema(marcadas=marcadas)


@router.put(
    "/entidade/{entidade_tipo}/{entidade_id}/lidas",
    response_model=ResultadoNotificacoesMarcadasSchema,
    summary="Dispensar os avisos informativos de uma entidade",
)
def marcar_lidas_por_entidade(
    entidade_tipo: str,
    entidade_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """
    Chamado ao abrir o detalhe de um incidente, tarefa ou controlo.

    Dispensa apenas os avisos informativos. Os acionáveis ficam: um prazo legal
    ultrapassado tem de continuar à vista enquanto estiver ultrapassado, e sai
    quando for cumprido ou quando alguém o dispensar de propósito.
    """
    if entidade_tipo not in _ENTIDADES:
        raise HTTPException(
            status_code=422, detail=f"Entidade desconhecida: {entidade_tipo}."
        )
    marcadas = service.marcar_lidas_por_entidade(
        db,
        entidade_tipo=entidade_tipo,
        entidade_id=entidade_id,
        utilizador=utilizador,
    )
    db.commit()
    return ResultadoNotificacoesMarcadasSchema(marcadas=marcadas)


@router.put(
    "/controlo/{controlo_empresa_id}/lidas",
    response_model=ResultadoNotificacoesMarcadasSchema,
    summary="Dispensar os avisos informativos de um controlo",
)
def marcar_lidas_por_controlo(
    controlo_empresa_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Forma abreviada da rota por entidade, mantida por já estar em uso."""
    marcadas = service.marcar_lidas_por_entidade(
        db,
        entidade_tipo="ControloEmpresaV2",
        entidade_id=controlo_empresa_id,
        utilizador=utilizador,
    )
    db.commit()
    return ResultadoNotificacoesMarcadasSchema(marcadas=marcadas)
