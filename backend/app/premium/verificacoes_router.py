"""
Router das VERIFICAÇÕES TÉCNICAS (premium): o que as ligações e os relatórios
importados provam. Superfície fina, dois gates que se acumulam:
  - require_alguma_feature(...)           → o tenant tem alguma fonte? (402); o
                                             sidecar filtra o que cada uma dá
  - require_capability("verificacoes", …) → ver os resultados é de quem corrige
                                             (o implementador também); marcar como
                                             visto e decidir um alerta é operar;
                                             as metas da empresa são governação

Passthrough para o sidecar, que é a autoridade. As ligações (credenciais,
servidores, coletor) estão noutro router, com outra capacidade: ver o que falha
não é ver os segredos com que se lá chegou.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlmodel import select

from app.frameworks.runtime import nivel_qnrcs_efetivo
from app.premium.atores import ator_de
from app.premium.conetor_client import FEATURES_CONETORES, ConetorClient, get_conetor_client
from app.premium import contexto_nucleo
from app.premium.dependencies import recusar_escrita_em_so_leitura, require_alguma_feature
from app.premium.erros_conetor import executar_conetor
from app.premium.pedido import cliente_ou_503
from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, SessionDep

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/verificacoes",
    tags=["Verificações técnicas"],
    dependencies=[
        Depends(require_capability("verificacoes", ClasseAcao.VER)),
        Depends(require_alguma_feature(*FEATURES_CONETORES)),
    ],
)

_Operar = Depends(require_capability("verificacoes", ClasseAcao.OPERAR))
_Governar = Depends(require_capability("verificacoes", ClasseAcao.GOVERNAR))
# As escritas param com a licença em só-leitura, como nos outros módulos.
_Escrita = Depends(recusar_escrita_em_so_leitura(*FEATURES_CONETORES))
ConetorDep = Depends(get_conetor_client)

# Teto dos códigos pedidos de uma vez (um controlo tem um código; um risco, meia
# dúzia de tratamentos). Acima disto não é um ecrã a pedir.
_MAX_CODIGOS = 200


class VistoIn(BaseModel):
    nota: str = Field("", max_length=4000)


class MetasIn(BaseModel):
    # O sidecar valida a forma e os valores contra o catálogo (só aperta o mínimo).
    sinais_config_json: str = Field("", max_length=64_000)


class DecisaoAlertaIn(BaseModel):
    decisao: str = Field(..., pattern="^(incidente|descartado)$")
    motivo: str = Field("", max_length=4000)
    incidente_id: str = Field("", max_length=64)


def _tenant(utilizador) -> str:
    return str(utilizador.empresa_id)


def _declaracoes_de_agora(db, utilizador) -> dict[str, str]:
    """O estado declarado dos controlos de agora, que é do núcleo: com ele o
    sidecar apura as contradições (dado como feito, e a verificação diz que falha)
    na leitura. Fail-soft: uma instalação por semear não tem referencial, e ler as
    verificações não pode depender disso; sem declarações, o sidecar mostra o que
    ficou guardado."""
    from app.shared.dependencies import get_empresa_ativa

    try:
        return contexto_nucleo.declaracoes_dos_controlos(db, get_empresa_ativa(db, utilizador))
    except Exception:  # noqa: BLE001 — ver docstring
        logger.warning("Verificações: sem o estado dos controlos da empresa %s.", utilizador.empresa_id, exc_info=True)
        return {}


# ── Leituras ─────────────────────────────────────────────────────────────────

@router.get("/catalogo", summary="As fontes, o tema de cada uma e os campos das metas")
def catalogo(utilizador: CurrentUserDep, cli: ConetorClient | None = ConetorDep):
    return executar_conetor(cliente_ou_503(cli).catalogo, _tenant(utilizador))


@router.get("/fontes", summary="De onde vêm os dados de cada fonte e de quando são")
def fontes(utilizador: CurrentUserDep, cli: ConetorClient | None = ConetorDep):
    return executar_conetor(cliente_ou_503(cli).estado_fontes, _tenant(utilizador))


@router.get("/constatacoes", summary="As verificações de um tema (vazio = todas)")
def constatacoes(
    utilizador: CurrentUserDep,
    db: SessionDep,
    tema: str = Query("", max_length=32),
    locale: str = Query("", max_length=16),
    cli: ConetorClient | None = ConetorDep,
):
    return executar_conetor(
        cliente_ou_503(cli).constatacoes,
        _tenant(utilizador), tema, locale, _declaracoes_de_agora(db, utilizador),
    )


@router.get("/controlos", summary="As verificações ligadas a uns controlos")
def por_controlos(
    utilizador: CurrentUserDep,
    db: SessionDep,
    codigos: str = Query(..., max_length=4000, description="Códigos separados por vírgula"),
    locale: str = Query("", max_length=16),
    cli: ConetorClient | None = ConetorDep,
):
    lista = [c.strip() for c in codigos.split(",") if c.strip()][:_MAX_CODIGOS]
    if not lista:
        return {"sinais": []}
    return executar_conetor(
        cliente_ou_503(cli).constatacoes_dos_controlos,
        _tenant(utilizador), lista, locale, _declaracoes_de_agora(db, utilizador),
    )


@router.get("/sinais/{fonte}/{sinal}", summary="Uma verificação por inteiro: passos e afetados")
def detalhe_sinal(
    fonte: str,
    sinal: str,
    utilizador: CurrentUserDep,
    locale: str = Query("", max_length=16),
    cli: ConetorClient | None = ConetorDep,
):
    return executar_conetor(
        cliente_ou_503(cli).detalhe_sinal, _tenant(utilizador), fonte, sinal, ator_de(utilizador, "verificacoes"), locale
    )


@router.get("/eventos", summary="O que piorou, melhorou ou passou a contradizer o declarado")
def eventos(
    utilizador: CurrentUserDep,
    antes_de: int = Query(0, ge=0),
    limite: int = Query(0, ge=0, le=200),
    so_por_resolver: bool = False,
    cli: ConetorClient | None = ConetorDep,
):
    # O histórico do ecrã lê-se do mais recente para trás, página a página.
    return executar_conetor(
        cliente_ou_503(cli).listar_eventos,
        _tenant(utilizador),
        0,
        limite,
        so_por_resolver,
        True,
        antes_de,
    )


@router.get("/factos/ativo/{ativo_id}", summary="O que as verificações dizem de uma máquina")
def factos_do_ativo(ativo_id: uuid.UUID, utilizador: CurrentUserDep, cli: ConetorClient | None = ConetorDep):
    return executar_conetor(cliente_ou_503(cli).factos_do_ativo, _tenant(utilizador), str(ativo_id))


@router.get("/factos/{dominio}", summary="Os factos de um domínio, máquina a máquina")
def factos_por_dominio(dominio: str, utilizador: CurrentUserDep, cli: ConetorClient | None = ConetorDep):
    return executar_conetor(cliente_ou_503(cli).factos_por_dominio, _tenant(utilizador), dominio)


@router.get("/alertas", summary="Alertas graves da monitorização, por decidir ou decididos")
def alertas(
    utilizador: CurrentUserDep,
    estado: str = Query("", max_length=16),
    limite: int = Query(0, ge=0, le=200),
    cli: ConetorClient | None = ConetorDep,
):
    return executar_conetor(cliente_ou_503(cli).listar_alertas, _tenant(utilizador), estado, limite)


@router.get("/avisos-risco", summary="O que as verificações dizem dos controlos e do ativo de um risco")
def avisos_risco(
    utilizador: CurrentUserDep,
    db: SessionDep,
    controlos: str = Query("", max_length=4000, description="Ids dos controlos da empresa, por vírgula"),
    ativo_id: uuid.UUID | None = None,
    locale: str = Query("", max_length=16),
    cli: ConetorClient | None = ConetorDep,
):
    """Um tratamento de risco assenta num controlo; se a verificação técnica diz
    que esse controlo falha, o risco residual está subestimado. E um ativo com
    vulnerabilidades críticas por corrigir pesa no risco que o tem por alvo.
    Sugere — nunca mexe na probabilidade nem no impacto."""
    from app.frameworks.models import Control, ControloEmpresaV2

    c = cliente_ou_503(cli)
    ids: list[uuid.UUID] = []
    for parte in controlos.split(","):
        try:
            ids.append(uuid.UUID(parte.strip()))
        except ValueError:
            continue
    codigo_de: dict[str, str] = {}
    if ids:
        ids = ids[:_MAX_CODIGOS]
        # O tratamento guarda o id do controlo que o ecrã lhe deu, que é o do
        # quadro (o de `/controlos`); dados antigos podem ter o da empresa. Os
        # dois resolvem-se, sempre pelos controlos desta empresa, e a resposta
        # devolve o id tal como veio (a ficha do controlo aceita os dois).
        pedidos = set(ids)
        linhas = db.exec(
            select(ControloEmpresaV2.id, ControloEmpresaV2.control_id, Control.code)
            .join(Control, Control.id == ControloEmpresaV2.control_id)
            .where(
                ControloEmpresaV2.empresa_id == utilizador.empresa_id,
                or_(ControloEmpresaV2.id.in_(ids), ControloEmpresaV2.control_id.in_(ids)),
            )
        ).all()
        for ce_id, control_id, code in linhas:
            enviado = ce_id if ce_id in pedidos else control_id
            codigo_de.setdefault(code, str(enviado))
    if not codigo_de and ativo_id is None:
        return {"controlos_a_falhar": [], "ativo": None}
    # Quem diz o que falha e o que pesa no risco é o sidecar; ao núcleo cabe só
    # traduzir os ids dos controlos (que são dele) em códigos, e de volta.
    avisos = executar_conetor(
        c.avisos_do_risco,
        _tenant(utilizador),
        list(codigo_de),
        str(ativo_id) if ativo_id is not None else "",
        locale,
        _declaracoes_de_agora(db, utilizador),
    )
    return {
        "controlos_a_falhar": [
            {"controlo_id": codigo_de[a["codigo"]], "codigo": a["codigo"], "sinais": a["sinais"]}
            for a in avisos["controlos_a_falhar"]
            if a["codigo"] in codigo_de
        ],
        "ativo": avisos["ativo"],
    }


# ── Escrita ──────────────────────────────────────────────────────────────────

@router.post("/eventos/{evento_id}/visto", summary="Marcar um desvio como visto", dependencies=[_Operar, _Escrita])
def marcar_visto(
    evento_id: int,
    dados: VistoIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: ConetorClient | None = ConetorDep,
):
    resultado = executar_conetor(
        cliente_ou_503(cli).resolver_evento, _tenant(utilizador), evento_id, dados.nota, ator_de(utilizador, "verificacoes")
    )
    registar_acao(
        db, acao=Acao.CONETOR_EVENTO_RESOLVIDO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Conetor", entidade_id=None,
        dados_novos={"evento_id": evento_id}, request=request,
    )
    db.commit()
    return resultado


@router.post("/alertas/{alerta_id}/decidir", summary="Decidir um alerta grave", dependencies=[_Operar, _Escrita])
def decidir_alerta(
    alerta_id: int,
    dados: DecisaoAlertaIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: ConetorClient | None = ConetorDep,
):
    """Passou a incidente (o incidente já criado, pelo id) ou foi descartado (com
    o motivo, que fica cifrado). Um alerta decidido não se decide outra vez."""
    if dados.decisao == "incidente":
        from app.incidentes.models import Incidente

        try:
            inc_id = uuid.UUID(dados.incidente_id)
        except ValueError:
            raise HTTPException(status_code=400, detail={"codigo": "incidente_em_falta"})
        incidente = db.get(Incidente, inc_id)
        if incidente is None or incidente.empresa_id != utilizador.empresa_id:
            raise HTTPException(status_code=404, detail={"codigo": "nao_encontrado"})
    resultado = executar_conetor(
        cliente_ou_503(cli).decidir_alerta,
        _tenant(utilizador),
        alerta_id,
        dados.decisao,
        dados.motivo,
        dados.incidente_id,
        ator_de(utilizador, "verificacoes"),
    )
    # Sem o motivo nem o título (vivem cifrados no sidecar): só a decisão.
    registar_acao(
        db, acao=Acao.CONETOR_ALERTA_DECIDIDO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Conetor", entidade_id=None,
        dados_novos={
            "alerta_id": alerta_id,
            "decisao": dados.decisao,
            "incidente_id": dados.incidente_id or None,
            "nivel": resultado.get("nivel"),
        },
        request=request,
    )
    db.commit()
    return resultado


# ── Metas da empresa ─────────────────────────────────────────────────────────

@router.get("/metas/{fonte}", summary="As metas da empresa para os sinais de uma fonte")
def obter_metas(fonte: str, utilizador: CurrentUserDep, cli: ConetorClient | None = ConetorDep):
    est = executar_conetor(cliente_ou_503(cli).estado, _tenant(utilizador), fonte)
    return {"fonte": fonte, "sinais_config_json": est.get("sinais_config_json") or "{}"}


@router.put("/metas/{fonte}", summary="Definir as metas da empresa para os sinais de uma fonte", dependencies=[_Governar, _Escrita])
def definir_metas(
    fonte: str,
    dados: MetasIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: ConetorClient | None = ConetorDep,
):
    """As metas só apertam o mínimo do nível (o sidecar garante-o). O que depende
    do relógio (a idade de uma leitura ou de um relatório) reavalia-se já; os
    outros limites contam a partir da próxima leitura."""
    from app.shared.dependencies import get_empresa_ativa

    c = cliente_ou_503(cli)
    executar_conetor(
        c.configurar_politica, _tenant(utilizador), fonte, dados.sinais_config_json, True,
        ator_de(utilizador, "verificacoes"),
    )
    registar_acao(
        db, acao=Acao.CONETOR_CONFIGURADO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Conetor", entidade_id=None,
        dados_novos={"tipo": fonte, "metas": True}, request=request,
    )
    db.commit()
    # Reavaliar já o que depende do tempo, com as metas novas. Fail-soft: as
    # metas ficaram gravadas, e o tick volta a reavaliar no ciclo seguinte.
    try:
        empresa = get_empresa_ativa(db, utilizador)
        executar_conetor(c.reavaliar_observacoes, _tenant(utilizador), nivel_qnrcs_efetivo(empresa), contexto_nucleo.declaracoes_dos_controlos(db, empresa))
    except HTTPException:
        pass
    return {"fonte": fonte, "sinais_config_json": dados.sinais_config_json or "{}"}
