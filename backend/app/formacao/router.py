"""
Router do módulo de Formação (core).

Autorização em dois gates que se acumulam:
  - require_capability("formacao", VER)      → leitura (todos os papéis)
  - require_capability("formacao", OPERAR)   → criar/editar/participantes
  - require_capability("formacao", ELIMINAR) → soft delete (gestão)

A matriz de capacidades tem a linha "formacao" (app/shared/capacidades.py). O
registo de formação sai como payload localizado (o PDF é gerado no cliente).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from app.formacao import service
from app.formacao.schemas import (
    AcaoAtualizarIn,
    AcaoCriarIn,
    AcaoDetalheSchema,
    AcaoSchema,
    EstadoIn,
    ListaAcoesSchema,
    LoteParticipantesSchema,
    PainelFormacaoSchema,
    ParticipantesLoteIn,
    ParticipanteSchema,
    PresencaIn,
)
from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, SessionDep, get_empresa_ativa
from app.shared.i18n import locale_de_request

router = APIRouter(
    prefix="/formacao",
    tags=["Formação"],
    dependencies=[Depends(require_capability("formacao", ClasseAcao.VER))],
)

OperarDep = Depends(require_capability("formacao", ClasseAcao.OPERAR))
EliminarDep = Depends(require_capability("formacao", ClasseAcao.ELIMINAR))


@router.get("", response_model=ListaAcoesSchema, summary="Listar ações de formação")
def listar(utilizador: CurrentUserDep, db: SessionDep, incluir_canceladas: bool = True):
    empresa = get_empresa_ativa(db, utilizador)
    return service.listar_acoes(db, empresa.id, incluir_canceladas)


@router.get("/painel", response_model=PainelFormacaoSchema, summary="Indicadores do módulo")
def obter_painel(utilizador: CurrentUserDep, db: SessionDep):
    empresa = get_empresa_ativa(db, utilizador)
    return service.painel(db, empresa.id)


@router.get("/registo", summary="Registo de formação (payload localizado)")
def registo(request: Request, utilizador: CurrentUserDep, db: SessionDep):
    from app.premium.anexar_evidencia import enriquecer_documento

    empresa = get_empresa_ativa(db, utilizador)
    doc = service.documento_formacao(db, empresa.id, locale_de_request(request))
    # Hash estável + controlo-alvo para o "anexar como evidência" num clique.
    doc = enriquecer_documento(db, empresa, doc)
    # A exportação do registo fica registada (mesma prática dos relatórios do core).
    registar_acao(
        db, acao=Acao.RELATORIO_EXPORTADO, empresa_id=empresa.id,
        utilizador_id=utilizador.id, entidade_tipo="AcaoFormacao", entidade_id=None,
        dados_novos={"tipo": "registo_formacao"}, request=request,
    )
    return doc


@router.get("/{acao_id}", response_model=AcaoDetalheSchema, summary="Detalhe de uma ação")
def obter(acao_id: uuid.UUID, utilizador: CurrentUserDep, db: SessionDep):
    empresa = get_empresa_ativa(db, utilizador)
    return service.obter_acao(db, acao_id, empresa.id)


@router.post(
    "", response_model=AcaoSchema, status_code=201,
    summary="Registar uma ação de formação", dependencies=[OperarDep],
)
def criar(dados: AcaoCriarIn, request: Request, utilizador: CurrentUserDep, db: SessionDep):
    empresa = get_empresa_ativa(db, utilizador)
    return service.criar_acao(db, empresa.id, dados, utilizador, request)


@router.patch(
    "/{acao_id}", response_model=AcaoSchema,
    summary="Atualizar uma ação de formação", dependencies=[OperarDep],
)
def atualizar(
    acao_id: uuid.UUID, dados: AcaoAtualizarIn, request: Request,
    utilizador: CurrentUserDep, db: SessionDep,
):
    empresa = get_empresa_ativa(db, utilizador)
    return service.atualizar_acao(db, acao_id, empresa.id, dados, utilizador, request)


@router.post(
    "/{acao_id}/estado", response_model=AcaoSchema,
    summary="Alterar o estado (marcar realizada)", dependencies=[OperarDep],
)
def alterar_estado(
    acao_id: uuid.UUID, dados: EstadoIn, request: Request,
    utilizador: CurrentUserDep, db: SessionDep,
):
    empresa = get_empresa_ativa(db, utilizador)
    return service.alterar_estado(db, acao_id, empresa.id, dados.estado, utilizador, request)


@router.post(
    "/{acao_id}/participantes", response_model=LoteParticipantesSchema, status_code=201,
    summary="Inscrever participantes (lote)", dependencies=[OperarDep],
)
def adicionar_participantes(
    acao_id: uuid.UUID, dados: ParticipantesLoteIn, request: Request,
    utilizador: CurrentUserDep, db: SessionDep,
):
    """Uma turma inteira num pedido; os já inscritos vêm contados em `ignorados`."""
    empresa = get_empresa_ativa(db, utilizador)
    return service.adicionar_participantes(db, acao_id, empresa.id, dados, utilizador, request)


@router.patch(
    "/{acao_id}/participantes/{participante_id}/presenca", response_model=ParticipanteSchema,
    summary="Marcar um participante como presente ou ausente", dependencies=[OperarDep],
)
def alterar_presenca(
    acao_id: uuid.UUID, participante_id: uuid.UUID, dados: PresencaIn, request: Request,
    utilizador: CurrentUserDep, db: SessionDep,
):
    empresa = get_empresa_ativa(db, utilizador)
    return service.alterar_presenca(
        db, acao_id, participante_id, empresa.id, dados.presente, utilizador, request
    )


@router.delete(
    "/{acao_id}/participantes/{participante_id}", status_code=204,
    summary="Remover um participante", dependencies=[OperarDep],
)
def remover_participante(
    acao_id: uuid.UUID, participante_id: uuid.UUID, request: Request,
    utilizador: CurrentUserDep, db: SessionDep,
):
    empresa = get_empresa_ativa(db, utilizador)
    service.remover_participante(db, acao_id, participante_id, empresa.id, utilizador, request)


@router.delete(
    "/{acao_id}", status_code=204,
    summary="Eliminar uma ação (soft delete)", dependencies=[EliminarDep],
)
def eliminar(acao_id: uuid.UUID, request: Request, utilizador: CurrentUserDep, db: SessionDep):
    empresa = get_empresa_ativa(db, utilizador)
    service.eliminar_acao(db, acao_id, empresa.id, utilizador, request)
