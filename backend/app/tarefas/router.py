"""
Router do módulo de Tarefas recorrentes (core).

Autorização em dois gates que se acumulam:
  - require_capability("tarefas", VER)      → leitura (todos os papéis)
  - require_capability("tarefas", OPERAR)   → criar/editar/registar conclusões
  - require_capability("tarefas", ELIMINAR) → soft delete (gestão)

A matriz de capacidades tem a linha "tarefas" (app/shared/capacidades.py), com a
mesma segregação dos outros módulos (AUDITOR/CEO não operam). O registo de execução
sai como payload localizado (o PDF é gerado no cliente, como nos módulos premium).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, SessionDep, get_empresa_ativa
from app.shared.i18n import locale_de_request
from app.tarefas import service
from app.tarefas.schemas import (
    ConclusaoIn,
    ItemCatalogoSchema,
    ListaTarefasSchema,
    PainelTarefasSchema,
    TarefaAtualizarIn,
    TarefaCriarIn,
    TarefaDetalheSchema,
    TarefaSchema,
)

router = APIRouter(
    prefix="/tarefas",
    tags=["Tarefas"],
    dependencies=[Depends(require_capability("tarefas", ClasseAcao.VER))],
)

OperarDep = Depends(require_capability("tarefas", ClasseAcao.OPERAR))
EliminarDep = Depends(require_capability("tarefas", ClasseAcao.ELIMINAR))


@router.get("", response_model=ListaTarefasSchema, summary="Listar tarefas")
def listar(utilizador: CurrentUserDep, db: SessionDep, incluir_inativas: bool = False):
    empresa = get_empresa_ativa(db, utilizador)
    return service.listar_tarefas(db, empresa.id, incluir_inativas)


@router.get("/painel", response_model=PainelTarefasSchema, summary="Indicadores do módulo")
def obter_painel(utilizador: CurrentUserDep, db: SessionDep):
    empresa = get_empresa_ativa(db, utilizador)
    return service.painel(db, empresa.id)


@router.get("/catalogo", response_model=list[ItemCatalogoSchema], summary="Obrigações pré-definidas")
def obter_catalogo(request: Request, utilizador: CurrentUserDep, db: SessionDep):
    # Não toca na base de dados — mas mantém o gate de leitura do módulo.
    return service.catalogo(locale_de_request(request))


@router.get("/{tarefa_id}", response_model=TarefaDetalheSchema, summary="Detalhe de uma tarefa")
def obter(tarefa_id: uuid.UUID, utilizador: CurrentUserDep, db: SessionDep):
    empresa = get_empresa_ativa(db, utilizador)
    return service.obter_tarefa(db, tarefa_id, empresa.id)


@router.get("/{tarefa_id}/relatorio", summary="Registo de execução (payload localizado)")
def relatorio(tarefa_id: uuid.UUID, request: Request, utilizador: CurrentUserDep, db: SessionDep):
    from app.premium.anexar_evidencia import enriquecer_documento

    empresa = get_empresa_ativa(db, utilizador)
    doc = service.documento_tarefa(db, tarefa_id, empresa.id, locale_de_request(request))
    # Hash estável + controlo-alvo para o "anexar como evidência" num clique.
    doc = enriquecer_documento(db, empresa, doc)
    # A exportação do registo fica registada (mesma prática dos relatórios do core).
    registar_acao(
        db, acao=Acao.RELATORIO_EXPORTADO, empresa_id=empresa.id,
        utilizador_id=utilizador.id, entidade_tipo="Tarefa", entidade_id=tarefa_id,
        dados_novos={"tipo": "registo_execucao_tarefa"}, request=request,
    )
    return doc


@router.post(
    "", response_model=TarefaSchema, status_code=201,
    summary="Criar uma tarefa (do catálogo ou personalizada)", dependencies=[OperarDep],
)
def criar(dados: TarefaCriarIn, request: Request, utilizador: CurrentUserDep, db: SessionDep):
    empresa = get_empresa_ativa(db, utilizador)
    return service.criar_tarefa(db, empresa.id, dados, utilizador, request)


@router.patch(
    "/{tarefa_id}", response_model=TarefaSchema,
    summary="Atualizar uma tarefa", dependencies=[OperarDep],
)
def atualizar(
    tarefa_id: uuid.UUID, dados: TarefaAtualizarIn, request: Request,
    utilizador: CurrentUserDep, db: SessionDep,
):
    empresa = get_empresa_ativa(db, utilizador)
    return service.atualizar_tarefa(db, tarefa_id, empresa.id, dados, utilizador, request)


@router.post(
    "/{tarefa_id}/conclusoes", response_model=TarefaSchema, status_code=201,
    summary="Registar que a tarefa foi cumprida", dependencies=[OperarDep],
)
def registar_conclusao(
    tarefa_id: uuid.UUID, dados: ConclusaoIn, request: Request,
    utilizador: CurrentUserDep, db: SessionDep,
):
    empresa = get_empresa_ativa(db, utilizador)
    return service.registar_conclusao(db, tarefa_id, empresa.id, dados, utilizador, request)


@router.delete(
    "/{tarefa_id}", status_code=204,
    summary="Eliminar uma tarefa (soft delete)", dependencies=[EliminarDep],
)
def eliminar(tarefa_id: uuid.UUID, request: Request, utilizador: CurrentUserDep, db: SessionDep):
    empresa = get_empresa_ativa(db, utilizador)
    service.eliminar_tarefa(db, tarefa_id, empresa.id, utilizador, request)
