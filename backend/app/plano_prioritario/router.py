"""
Router do módulo de Plano de Ações Prioritárias.
Endpoints finos — toda a lógica fica em service.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session

from app.auth.models import RoleUtilizador, Utilizador
from app.database import get_session
from app.plano_prioritario import service
from app.plano_prioritario.schemas import (
    PlanoOut,
    QuestionarioRespostasIn,
    QuestionarioRespostasOut,
)
from app.shared.dependencies import get_current_user, get_empresa_ativa, require_role
from app.shared.utils import parse_accept_language

router = APIRouter(prefix="/plano-prioritario", tags=["Plano Prioritário"])


# ---------------------------------------------------------------------------
# POST /questionario — guardar respostas + gerar plano
# ---------------------------------------------------------------------------

@router.post(
    "/questionario",
    response_model=QuestionarioRespostasOut,
    summary="Guardar respostas ao questionário e gerar plano",
    dependencies=[Depends(require_role(RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN))],
)
async def guardar_questionario(
    payload: QuestionarioRespostasIn,
    utilizador: Utilizador = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """
    Recebe as respostas ao questionário de 10 perguntas.
    Guarda/atualiza as respostas e regenera o plano de ações.
    """
    empresa = get_empresa_ativa(db, utilizador)
    qr = service.guardar_respostas(
        db, empresa, utilizador, payload.respostas,
    )
    # Regenerar plano após guardar respostas
    service.gerar_plano(db, empresa)
    return qr


# ---------------------------------------------------------------------------
# GET /questionario — obter respostas actuais
# ---------------------------------------------------------------------------

@router.get(
    "/questionario",
    response_model=QuestionarioRespostasOut | None,
    summary="Obter respostas actuais ao questionário",
    dependencies=[Depends(require_role(RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN))],
)
async def obter_questionario(
    utilizador: Utilizador = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Devolve as respostas guardadas ou null se não preenchido."""
    empresa = get_empresa_ativa(db, utilizador)
    return service.obter_respostas(db, empresa.id)


# ---------------------------------------------------------------------------
# GET /plano — obter plano gerado
# ---------------------------------------------------------------------------

@router.get(
    "/plano",
    response_model=PlanoOut,
    summary="Obter resumo do plano de ações prioritárias",
)
async def obter_plano(
    request: Request,
    utilizador: Utilizador = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """
    Devolve apenas os primeiros controlos não conformes mostrados no dashboard.
    Se o plano não existir, gera-o automaticamente (sem questionário = sem mapeamento).
    """
    empresa = get_empresa_ativa(db, utilizador)
    locale = parse_accept_language(request.headers.get("accept-language"))

    if not service.plano_existe(db, empresa.id):
        service.gerar_plano(db, empresa)

    return service.obter_plano(db, empresa, locale=locale, utilizador=utilizador)


# ---------------------------------------------------------------------------
# POST /plano/regenerar — forçar regeneração do plano
# ---------------------------------------------------------------------------

@router.post(
    "/plano/regenerar",
    response_model=PlanoOut,
    summary="Regenerar plano de ações prioritárias",
    dependencies=[Depends(require_role(RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN))],
)
async def regenerar_plano(
    utilizador: Utilizador = Depends(get_current_user),
    db: Session = Depends(get_session),
):
    """Força a regeneração do plano com os dados e respostas actuais."""
    empresa = get_empresa_ativa(db, utilizador)
    service.gerar_plano(db, empresa)
    return service.obter_plano(db, empresa)
