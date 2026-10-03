"""
Router do módulo de controlos.
Endpoints finos — toda a lógica fica em service.py.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, status
from sqlmodel import Session

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
    MarcarNaoAplicavelSchema,
    RelatorioAuditoriaSchema,
    ReprovarControloSchema,
    ResultadoDelegacaoLoteSchema,
)
from app.database import get_session
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, get_empresa_ativa
from app.shared.utils import parse_accept_language

# Quem pode a ação; sobre que controlo em concreto decide-se no service, com o
# registo em mão (`exigir_ambito`).
GovernarDep = Depends(require_capability("controlos", ClasseAcao.GOVERNAR))
AprovarDep = Depends(require_capability("controlos", ClasseAcao.APROVAR))
DelegarDep = Depends(require_capability("controlos", ClasseAcao.DELEGAR))
VerDep = Depends(require_capability("controlos", ClasseAcao.VER))
OperarDep = Depends(require_capability("controlos", ClasseAcao.OPERAR))

# Chão de LEITURA para tudo o que este router serve — incluindo os scores por
# domínio e o painel de maturidade, que respondiam a quem tivesse o módulo
# fechado. Quem escreve também vê (é invariante da política), por isso o chão
# não aperta nenhuma das ações abaixo; o que faz é impedir que uma rota nova
# nasça sem qualquer verificação.
router = APIRouter(tags=["Controlos"], dependencies=[VerDep])


# ---------------------------------------------------------------------------
# GET /dominios — lista domínios com scores
# ---------------------------------------------------------------------------

@router.get(
    "/dominios",
    response_model=list[DominioSchema],
    summary="Listar os objetivos do referencial com scores",
)
def listar_dominios(
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Devolve os objetivos do referencial (6 no QNRCS) com o score de maturidade da empresa."""
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
def dashboard(
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """
    Calcula scores globais, por objetivo e controlos críticos em falta.
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
    dependencies=[VerDep],
)
def listar_controlos(
    request: Request,
    utilizador: CurrentUserDep,
    dominio_id: uuid.UUID | None = None,
    db: Session = Depends(get_session, scope="function"),
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
    dependencies=[VerDep],
)
def get_controlo(
    controlo_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
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
    dependencies=[OperarDep],
)
def alterar_estado(
    controlo_empresa_id: uuid.UUID,
    dados: AlterarEstadoSchema,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
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
# POST/DELETE /controlos/{controlo_empresa_id}/nao-aplicavel — scoping
# ---------------------------------------------------------------------------

@router.post(
    "/controlos/{controlo_empresa_id}/nao-aplicavel",
    response_model=ControloListaSchema,
    summary="Marcar controlo como não aplicável (scoping de exclusão)",
    dependencies=[GovernarDep],
)
def marcar_nao_aplicavel(
    controlo_empresa_id: uuid.UUID,
    dados: MarcarNaoAplicavelSchema,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Exclui o controlo do âmbito: sai das contas de conformidade mas fica
    visível, com a justificação registada e contestável pelo auditor."""
    empresa = get_empresa_ativa(db, utilizador)
    locale = parse_accept_language(request.headers.get("accept-language"))
    service.marcar_nao_aplicavel(
        db, controlo_empresa_id, empresa, utilizador, dados.justificacao, request
    )
    return service.get_controlo_lista_item(
        db, empresa, utilizador, controlo_empresa_id, locale=locale
    )


@router.delete(
    "/controlos/{controlo_empresa_id}/nao-aplicavel",
    response_model=ControloListaSchema,
    summary="Repor a aplicabilidade de um controlo",
    dependencies=[GovernarDep],
)
def reaplicar_controlo(
    controlo_empresa_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Reverte o "não aplicável": o controlo volta a 'não iniciado' e reentra
    em todas as contas de conformidade."""
    empresa = get_empresa_ativa(db, utilizador)
    locale = parse_accept_language(request.headers.get("accept-language"))
    service.reaplicar_controlo(db, controlo_empresa_id, empresa, utilizador, request)
    return service.get_controlo_lista_item(
        db, empresa, utilizador, controlo_empresa_id, locale=locale
    )


# ---------------------------------------------------------------------------
# POST /controlos/{controlo_empresa_id}/checks/{check_id}/concluir
# ---------------------------------------------------------------------------

@router.post(
    "/controlos/{controlo_empresa_id}/checks/{check_id}/concluir",
    status_code=status.HTTP_200_OK,
    summary="Marcar check como concluído",
    dependencies=[OperarDep],
)
def concluir_check(
    controlo_empresa_id: uuid.UUID,
    check_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
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
    dependencies=[OperarDep],
)
def reverter_check(
    controlo_empresa_id: uuid.UUID,
    check_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
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
    dependencies=[AprovarDep],
)
def aprovar(
    controlo_empresa_id: uuid.UUID,
    dados: AprovarControloSchema,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Aprova um controlo marcado como 'implementado'. Apenas auditores."""
    empresa = get_empresa_ativa(db, utilizador)
    ce = service.aprovar_controlo(
        db, controlo_empresa_id, empresa, utilizador, dados.texto_relatorio, request
    )
    resposta: dict = {"mensagem": "Controlo aprovado com sucesso."}
    # Aprovado abaixo do nível que o perfil exige: a aprovação vale, mas quem
    # aprovou fica a sabê-lo (e a auditoria também).
    nivel_minimo = service.nivel_minimo_do_controlo(db, ce, empresa)
    if ce.nivel_maturidade_atual < nivel_minimo:
        resposta["aviso"] = {
            "codigo": "nivel_abaixo_do_minimo",
            "nivel_atual": ce.nivel_maturidade_atual,
            "nivel_minimo": nivel_minimo,
        }
    return resposta


# ---------------------------------------------------------------------------
# POST /controlos/{controlo_empresa_id}/reprovar (auditor only)
# ---------------------------------------------------------------------------

@router.post(
    "/controlos/{controlo_empresa_id}/reprovar",
    status_code=status.HTTP_200_OK,
    summary="Reprovar controlo",
    dependencies=[AprovarDep],
)
def reprovar(
    controlo_empresa_id: uuid.UUID,
    dados: ReprovarControloSchema,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
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
    dependencies=[VerDep],
)
def historico_relatorios(
    controlo_empresa_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
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
    dependencies=[DelegarDep],
)
def delegar_lote(
    dados: DelegarControlosLoteSchema,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
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
    dependencies=[DelegarDep],
)
def delegar(
    controlo_empresa_id: uuid.UUID,
    dados: DelegarControloSchema,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
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
