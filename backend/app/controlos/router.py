"""
Router do módulo de controlos.
Endpoints finos — toda a lógica fica em service.py.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlmodel import Session

from app.auth.models import RoleUtilizador
from app.controlos import service
from app.controlos.schemas import (
    AlterarEstadoSchema,
    AprovarControloSchema,
    ControloDetalheSchema,
    ControloListaSchema,
    DashboardScoreSchema,
    DelegarControlosLoteSchema,
    DelegarControloSchema,
    DominioSchema,
    RelatorioAuditoriaSchema,
    ReprovarControloSchema,
    ResultadoDelegacaoLoteSchema,
)
from app.database import get_session
from app.shared.dependencies import CurrentUserDep, get_empresa_ativa, require_role
from app.shared.utils import parse_accept_language

router = APIRouter(tags=["Controlos"])


# ---------------------------------------------------------------------------
# GET /dominios — lista domínios com scores
# ---------------------------------------------------------------------------

@router.get(
    "/dominios",
    response_model=list[DominioSchema],
    summary="Listar domínios CyFun com scores",
)
async def listar_dominios(
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """Devolve os 5 domínios CyFun com o score de maturidade desta empresa."""
    empresa = get_empresa_ativa(db, utilizador)
    locale = parse_accept_language(request.headers.get("accept-language"))
    return service.listar_dominios(db, empresa.id, empresa, locale=locale)


# ---------------------------------------------------------------------------
# GET /dashboard — scores completo para spider chart
# ---------------------------------------------------------------------------

@router.get(
    "/dashboard",
    response_model=DashboardScoreSchema,
    summary="Dashboard de maturidade",
)
async def dashboard(
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """
    Calcula scores globais, por domínio e controlos críticos em falta.
    Usado pelo spider chart e panel executivo (CEO).
    """
    empresa = get_empresa_ativa(db, utilizador)
    locale = parse_accept_language(request.headers.get("accept-language"))
    return service.calcular_dashboard(
        db,
        empresa,
        utilizador=utilizador,
        locale=locale,
    )


# ---------------------------------------------------------------------------
# GET /controlos — listagem (filtrada por role)
# ---------------------------------------------------------------------------

@router.get(
    "/controlos",
    response_model=list[ControloListaSchema],
    summary="Listar controlos",
)
async def listar_controlos(
    request: Request,
    utilizador: CurrentUserDep,
    dominio_id: uuid.UUID | None = None,
    db: Session = Depends(get_session),
):
    """
    Lista controlos UCF com estado da empresa.
    Implementadores veem apenas os controlos que lhes foram delegados.
    """
    empresa = get_empresa_ativa(db, utilizador)
    locale = parse_accept_language(request.headers.get("accept-language"))
    return service.listar_controlos(db, empresa, utilizador, dominio_id, locale=locale)


# ---------------------------------------------------------------------------
# GET /controlos/{controlo_id} — detalhe
# ---------------------------------------------------------------------------

@router.get(
    "/controlos/{controlo_id}",
    response_model=ControloDetalheSchema,
    summary="Detalhe de um controlo",
)
async def get_controlo(
    controlo_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """Detalhe completo: guias, exemplos, checks e estado da empresa."""
    empresa = get_empresa_ativa(db, utilizador)
    locale = parse_accept_language(request.headers.get("accept-language"))
    return service.get_controlo_detalhe(db, empresa, controlo_id, utilizador, locale=locale)


# ---------------------------------------------------------------------------
# PUT /controlos/{controlo_empresa_id}/estado — alterar estado
# ---------------------------------------------------------------------------

@router.put(
    "/controlos/{controlo_empresa_id}/estado",
    response_model=ControloListaSchema,
    summary="Alterar estado de um controlo",
)
async def alterar_estado(
    controlo_empresa_id: uuid.UUID,
    dados: AlterarEstadoSchema,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """
    Altera o estado de implementação.
    - Implementador: em_progresso ↔ implementado (apenas seus controlos)
    - Admin: qualquer estado não-aprovação
    """
    empresa = get_empresa_ativa(db, utilizador)
    locale = parse_accept_language(request.headers.get("accept-language"))
    service.alterar_estado(
        db, controlo_empresa_id, dados.estado, empresa, utilizador, request
    )
    return service.get_controlo_lista_item(
        db,
        empresa,
        utilizador,
        controlo_empresa_id,
        locale=locale,
    )


# ---------------------------------------------------------------------------
# POST /controlos/{controlo_empresa_id}/checks/{check_id}/concluir
# ---------------------------------------------------------------------------

@router.post(
    "/controlos/{controlo_empresa_id}/checks/{check_id}/concluir",
    status_code=status.HTTP_200_OK,
    summary="Marcar check como concluído",
)
async def concluir_check(
    controlo_empresa_id: uuid.UUID,
    check_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """
    Marca um check de maturidade como concluído e recalcula o nível do controlo.
    """
    empresa = get_empresa_ativa(db, utilizador)
    novo_nivel = service.concluir_check(
        db, controlo_empresa_id, check_id, empresa, utilizador, request
    )
    return {"nivel_maturidade_atual": novo_nivel}


# ---------------------------------------------------------------------------
# DELETE /controlos/{controlo_empresa_id}/checks/{check_id}/concluir
# ---------------------------------------------------------------------------

@router.delete(
    "/controlos/{controlo_empresa_id}/checks/{check_id}/concluir",
    status_code=status.HTTP_200_OK,
    summary="Reverter check para não concluído",
)
async def reverter_check(
    controlo_empresa_id: uuid.UUID,
    check_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """Reverte um check para não concluído e recalcula o nível."""
    empresa = get_empresa_ativa(db, utilizador)
    novo_nivel = service.reverter_check(
        db, controlo_empresa_id, check_id, empresa, utilizador, request
    )
    return {"nivel_maturidade_atual": novo_nivel}


# ---------------------------------------------------------------------------
# POST /controlos/{controlo_empresa_id}/aprovar (auditor only)
# ---------------------------------------------------------------------------

@router.post(
    "/controlos/{controlo_empresa_id}/aprovar",
    status_code=status.HTTP_200_OK,
    summary="Aprovar controlo",
    dependencies=[Depends(require_role(RoleUtilizador.AUDITOR))],
)
async def aprovar(
    controlo_empresa_id: uuid.UUID,
    dados: AprovarControloSchema,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """Aprova um controlo marcado como 'implementado'. Apenas auditores."""
    empresa = get_empresa_ativa(db, utilizador)
    service.aprovar_controlo(
        db, controlo_empresa_id, empresa, utilizador, dados.texto_relatorio, request
    )
    return {"mensagem": "Controlo aprovado com sucesso."}


# ---------------------------------------------------------------------------
# POST /controlos/{controlo_empresa_id}/reprovar (auditor only)
# ---------------------------------------------------------------------------

@router.post(
    "/controlos/{controlo_empresa_id}/reprovar",
    status_code=status.HTTP_200_OK,
    summary="Reprovar controlo",
    dependencies=[Depends(require_role(RoleUtilizador.AUDITOR))],
)
async def reprovar(
    controlo_empresa_id: uuid.UUID,
    dados: ReprovarControloSchema,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """Reprova um controlo. Apenas auditores."""
    empresa = get_empresa_ativa(db, utilizador)
    service.reprovar_controlo(
        db,
        controlo_empresa_id,
        empresa,
        utilizador,
        dados.texto_relatorio,
        dados.nota,
        request,
    )
    return {"mensagem": "Controlo reprovado. O implementador deve rever a implementação."}


# ---------------------------------------------------------------------------
# GET /controlos/{controlo_empresa_id}/relatorios-auditoria
# ---------------------------------------------------------------------------

@router.get(
    "/controlos/{controlo_empresa_id}/relatorios-auditoria",
    response_model=list[RelatorioAuditoriaSchema],
    summary="Histórico de relatórios de auditoria",
)
async def historico_relatorios(
    controlo_empresa_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
    limite: int | None = Query(None, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Devolve o histórico de relatórios de auditoria de um controlo."""
    empresa = get_empresa_ativa(db, utilizador)
    return service.get_historico_relatorios(
        db,
        controlo_empresa_id,
        empresa,
        utilizador,
        limite=limite,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# POST /controlos/{controlo_empresa_id}/delegar (admin only)
# ---------------------------------------------------------------------------

@router.post(
    "/controlos/delegacoes/lote",
    response_model=ResultadoDelegacaoLoteSchema,
    status_code=status.HTTP_200_OK,
    summary="Delegar múltiplos controlos de uma vez",
    dependencies=[Depends(require_role(RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN))],
)
async def delegar_lote(
    dados: DelegarControlosLoteSchema,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """Aplica delegações e remoções de delegação numa única operação."""
    empresa = get_empresa_ativa(db, utilizador)
    alterados = service.delegar_controlos_lote(
        db,
        dados.implementador_id,
        dados.adicionar_ids,
        dados.remover_ids,
        empresa,
        utilizador,
        request,
    )
    return ResultadoDelegacaoLoteSchema(alterados=alterados)


@router.post(
    "/controlos/{controlo_empresa_id}/delegar",
    status_code=status.HTTP_200_OK,
    summary="Delegar controlo a implementador",
    dependencies=[Depends(require_role(RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN))],
)
async def delegar(
    controlo_empresa_id: uuid.UUID,
    dados: DelegarControloSchema,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """
    Atribui (ou remove) delegação de um controlo a um implementador.
    Apenas administradores.
    """
    empresa = get_empresa_ativa(db, utilizador)
    service.delegar_controlo(
        db, controlo_empresa_id, dados.implementador_id, empresa, utilizador, request
    )
    msg = (
        "Controlo delegado com sucesso."
        if dados.implementador_id
        else "Delegação removida."
    )
    return {"mensagem": msg}
