"""
Router do módulo de Incidentes (core).

Autorização em dois gates que se acumulam:
  - require_capability("incidentes", VER)      → leitura (todos os papéis)
  - require_capability("incidentes", OPERAR)   → registar/editar/timeline
  - require_capability("incidentes", ELIMINAR) → soft delete (gestão)

A matriz de capacidades já tem a linha "incidentes" (app/shared/capacidades.py), com
a mesma segregação dos outros módulos (AUDITOR/CEO não operam). O relatório e os
documentos das notificações saem como payload localizado (o PDF é gerado no
cliente, como nos módulos premium). A plataforma não submete nada à autoridade:
regista o que foi enviado e guarda a cópia.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from app.incidentes import service
from app.incidentes.models import Incidente
from app.incidentes.schemas import (
    EstadoIn,
    EventoIn,
    EventoSchema,
    IncidenteAtualizarIn,
    IncidenteCriarIn,
    IncidenteDetalheSchema,
    IncidenteSchema,
    ListaIncidentesSchema,
    NotificacaoDetalheSchema,
    NotificacaoIn,
    NotificacaoResumoSchema,
    PainelIncidentesSchema,
)
from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, SessionDep, get_empresa_ativa
from app.shared.i18n import locale_de_request

router = APIRouter(
    prefix="/incidentes",
    tags=["Incidentes"],
    dependencies=[Depends(require_capability("incidentes", ClasseAcao.VER))],
)

OperarDep = Depends(require_capability("incidentes", ClasseAcao.OPERAR))
EliminarDep = Depends(require_capability("incidentes", ClasseAcao.ELIMINAR))


@router.get("", response_model=ListaIncidentesSchema, summary="Listar incidentes")
def listar(
    utilizador: CurrentUserDep, db: SessionDep, incluir_fechados: bool = True
):
    empresa = get_empresa_ativa(db, utilizador)
    return service.listar_incidentes(db, empresa.id, incluir_fechados)


@router.get("/painel", response_model=PainelIncidentesSchema, summary="Indicadores do módulo")
def obter_painel(utilizador: CurrentUserDep, db: SessionDep):
    empresa = get_empresa_ativa(db, utilizador)
    return service.painel(db, empresa.id)


@router.get("/taxonomia", summary="Taxonomia de incidentes (pt e en)")
def obter_taxonomia():
    # Declarada antes de "/{incidente_id}": senão "taxonomia" seria lido como id.
    return service.taxonomia()


@router.get("/{incidente_id}", response_model=IncidenteDetalheSchema, summary="Detalhe de um incidente")
def obter(incidente_id: uuid.UUID, request: Request, utilizador: CurrentUserDep, db: SessionDep):
    empresa = get_empresa_ativa(db, utilizador)
    return service.obter_incidente(db, incidente_id, empresa.id, locale_de_request(request))


@router.get("/{incidente_id}/relatorio", summary="Relatório pré-preenchido (payload localizado)")
def relatorio(incidente_id: uuid.UUID, request: Request, utilizador: CurrentUserDep, db: SessionDep):
    from app.premium.anexar_evidencia import enriquecer_documento

    empresa = get_empresa_ativa(db, utilizador)
    doc = service.documento_incidente(db, incidente_id, empresa.id, locale_de_request(request))
    # Hash estável + controlo-alvo para o "anexar como evidência" num clique.
    doc = enriquecer_documento(db, empresa, doc)
    # A exportação do relatório fica registada (mesma prática dos relatórios do core).
    # O título do incidente vai junto: sem ele a trilha dizia que um relatório
    # de incidente saiu da aplicação, mas não de que incidente se tratava.
    inc = db.get(Incidente, incidente_id)
    registar_acao(
        db, acao=Acao.RELATORIO_EXPORTADO, empresa_id=empresa.id,
        utilizador_id=utilizador.id, entidade_tipo="Incidente", entidade_id=incidente_id,
        dados_novos={"titulo": inc.titulo if inc else None, "tipo": "relatorio_incidente"},
        request=request,
    )
    return doc


@router.get(
    "/{incidente_id}/documento/{tipo}",
    summary="Documento de uma notificação à autoridade (payload localizado)",
)
def documento(incidente_id: uuid.UUID, tipo: str, request: Request, utilizador: CurrentUserDep, db: SessionDep):
    empresa = get_empresa_ativa(db, utilizador)
    return service.documento_notificacao(db, incidente_id, empresa.id, tipo, locale_de_request(request))


@router.get(
    "/{incidente_id}/notificacoes", response_model=list[NotificacaoResumoSchema],
    summary="Notificações registadas como enviadas",
)
def listar_notificacoes(incidente_id: uuid.UUID, utilizador: CurrentUserDep, db: SessionDep):
    empresa = get_empresa_ativa(db, utilizador)
    return service.listar_notificacoes(db, incidente_id, empresa.id)


@router.get(
    "/{incidente_id}/notificacoes/{notificacao_id}", response_model=NotificacaoDetalheSchema,
    summary="Uma notificação enviada, com a cópia do que se entregou",
)
def obter_notificacao(
    incidente_id: uuid.UUID, notificacao_id: uuid.UUID, utilizador: CurrentUserDep, db: SessionDep,
):
    empresa = get_empresa_ativa(db, utilizador)
    return service.obter_notificacao(db, incidente_id, empresa.id, notificacao_id)


@router.post(
    "", response_model=IncidenteSchema, status_code=201,
    summary="Registar um incidente", dependencies=[OperarDep],
)
def criar(dados: IncidenteCriarIn, request: Request, utilizador: CurrentUserDep, db: SessionDep):
    empresa = get_empresa_ativa(db, utilizador)
    return service.criar_incidente(db, empresa.id, dados, utilizador, request)


@router.patch(
    "/{incidente_id}", response_model=IncidenteSchema,
    summary="Atualizar um incidente", dependencies=[OperarDep],
)
def atualizar(
    incidente_id: uuid.UUID, dados: IncidenteAtualizarIn, request: Request,
    utilizador: CurrentUserDep, db: SessionDep,
):
    empresa = get_empresa_ativa(db, utilizador)
    return service.atualizar_incidente(db, incidente_id, empresa.id, dados, utilizador, request)


@router.post(
    "/{incidente_id}/estado", response_model=IncidenteSchema,
    summary="Alterar o estado da resposta", dependencies=[OperarDep],
)
def alterar_estado(
    incidente_id: uuid.UUID, dados: EstadoIn, request: Request,
    utilizador: CurrentUserDep, db: SessionDep,
):
    empresa = get_empresa_ativa(db, utilizador)
    return service.alterar_estado(db, incidente_id, empresa.id, dados.estado, dados.nota, utilizador, request)


@router.post(
    "/{incidente_id}/notificacoes", response_model=IncidenteDetalheSchema, status_code=201,
    summary="Registar uma notificação enviada (guarda a cópia do documento)", dependencies=[OperarDep],
)
def registar_notificacao(
    incidente_id: uuid.UUID, dados: NotificacaoIn, request: Request,
    utilizador: CurrentUserDep, db: SessionDep,
):
    empresa = get_empresa_ativa(db, utilizador)
    return service.registar_notificacao(db, incidente_id, empresa.id, dados, utilizador, request)


@router.post(
    "/{incidente_id}/eventos", response_model=EventoSchema, status_code=201,
    summary="Adicionar nota/ação/decisão/comunicação à linha temporal", dependencies=[OperarDep],
)
def adicionar_evento(
    incidente_id: uuid.UUID, dados: EventoIn, request: Request,
    utilizador: CurrentUserDep, db: SessionDep,
):
    empresa = get_empresa_ativa(db, utilizador)
    return service.adicionar_evento(db, incidente_id, empresa.id, dados, utilizador, request)


@router.delete(
    "/{incidente_id}", status_code=204,
    summary="Eliminar um incidente (soft delete)", dependencies=[EliminarDep],
)
def eliminar(incidente_id: uuid.UUID, request: Request, utilizador: CurrentUserDep, db: SessionDep):
    empresa = get_empresa_ativa(db, utilizador)
    service.eliminar_incidente(db, incidente_id, empresa.id, utilizador, request)
