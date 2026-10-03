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

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlmodel import select

from app.premium.atores import ator_de
from app.premium.conetor_client import FEATURES_CONETORES, ConetorClient, get_conetor_client
from app.premium.conetor_router import _cliente, _declaracoes, _executar, _perfil_qnrcs
from app.premium.dependencies import recusar_escrita_em_so_leitura, require_alguma_feature
from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, SessionDep

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


_DADO_COMO_FEITO = {"implementado", "aprovado"}


def _contradicoes_de_agora(resultado: dict, db, utilizador) -> dict:
    """A contradição (dado como feito, e a verificação diz que falha) foi apurada
    com o estado dos controlos da última leitura. Um controlo que entretanto
    deixou de estar dado como feito já não tem o que contradizer: filtra-se com o
    estado de agora, que é do core. Sem isto, o painel e a ficha continuavam a
    acusar até à leitura seguinte."""
    from app.shared.dependencies import get_empresa_ativa

    sinais = resultado.get("sinais") or []
    if not any("contradicoes" in (s.get("resumo_json") or "") for s in sinais):
        return resultado
    declaracoes = _declaracoes(db, get_empresa_ativa(db, utilizador))
    feitos = {codigo for codigo, estado in declaracoes.items() if estado in _DADO_COMO_FEITO}
    for s in sinais:
        try:
            resumo = json.loads(s.get("resumo_json") or "{}")
        except ValueError:
            continue
        if not isinstance(resumo, dict) or not resumo.get("contradicoes"):
            continue
        atuais = [c for c in resumo["contradicoes"] if c in feitos]
        if atuais != resumo["contradicoes"]:
            if atuais:
                resumo["contradicoes"] = atuais
            else:
                resumo.pop("contradicoes")
            s["resumo_json"] = json.dumps(resumo, ensure_ascii=False)
    return resultado


# ── Leituras ─────────────────────────────────────────────────────────────────

@router.get("/catalogo", summary="As fontes, o tema de cada uma e os campos das metas")
def catalogo(utilizador: CurrentUserDep, cli: ConetorClient | None = ConetorDep):
    return _executar(_cliente(cli).catalogo, _tenant(utilizador))


@router.get("/fontes", summary="De onde vêm os dados de cada fonte e de quando são")
def fontes(utilizador: CurrentUserDep, cli: ConetorClient | None = ConetorDep):
    return _executar(_cliente(cli).estado_fontes, _tenant(utilizador))


@router.get("/constatacoes", summary="As verificações de um tema (vazio = todas)")
def constatacoes(
    utilizador: CurrentUserDep,
    db: SessionDep,
    tema: str = Query("", max_length=32),
    locale: str = Query("", max_length=16),
    cli: ConetorClient | None = ConetorDep,
):
    resultado = _executar(_cliente(cli).constatacoes, _tenant(utilizador), tema, locale)
    return _contradicoes_de_agora(resultado, db, utilizador)


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
    resultado = _executar(_cliente(cli).constatacoes_dos_controlos, _tenant(utilizador), lista, locale)
    return _contradicoes_de_agora(resultado, db, utilizador)


@router.get("/sinais/{fonte}/{sinal}", summary="Uma verificação por inteiro: passos e afetados")
def detalhe_sinal(
    fonte: str,
    sinal: str,
    utilizador: CurrentUserDep,
    locale: str = Query("", max_length=16),
    cli: ConetorClient | None = ConetorDep,
):
    return _executar(
        _cliente(cli).detalhe_sinal, _tenant(utilizador), fonte, sinal, ator_de(utilizador, "verificacoes"), locale
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
    return _executar(
        _cliente(cli).listar_eventos,
        _tenant(utilizador),
        0,
        limite,
        so_por_resolver,
        True,
        antes_de,
    )


@router.get("/factos/ativo/{ativo_id}", summary="O que as verificações dizem de uma máquina")
def factos_do_ativo(ativo_id: uuid.UUID, utilizador: CurrentUserDep, cli: ConetorClient | None = ConetorDep):
    return _executar(_cliente(cli).factos_do_ativo, _tenant(utilizador), str(ativo_id))


@router.get("/factos/{dominio}", summary="Os factos de um domínio, máquina a máquina")
def factos_por_dominio(dominio: str, utilizador: CurrentUserDep, cli: ConetorClient | None = ConetorDep):
    return _executar(_cliente(cli).factos_por_dominio, _tenant(utilizador), dominio)


@router.get("/alertas", summary="Alertas graves da monitorização, por decidir ou decididos")
def alertas(
    utilizador: CurrentUserDep,
    estado: str = Query("", max_length=16),
    limite: int = Query(0, ge=0, le=200),
    cli: ConetorClient | None = ConetorDep,
):
    return _executar(_cliente(cli).listar_alertas, _tenant(utilizador), estado, limite)


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

    c = _cliente(cli)
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
    controlos_a_falhar = []
    if codigo_de:
        sinais = (_executar(c.constatacoes_dos_controlos, _tenant(utilizador), list(codigo_de), locale))["sinais"]
        for codigo, ce_id in codigo_de.items():
            falham = [s for s in sinais if codigo in s["controlos"] and s["veredicto_politica"] == "nao_conforme"]
            if falham:
                controlos_a_falhar.append({"controlo_id": ce_id, "codigo": codigo, "sinais": falham})
    ativo = None
    if ativo_id is not None:
        factos = (_executar(c.factos_do_ativo, _tenant(utilizador), str(ativo_id)))["factos"]
        vulns = next((f for f in factos if f["dominio"] == "vulnerabilidades"), None)
        if vulns:
            ativo = {
                "criticas": int(vulns["dados"].get("critica", 0)) + int(vulns["dados"].get("alta", 0)),
                "observado_em": vulns["observado_em"],
            }
    return {"controlos_a_falhar": controlos_a_falhar, "ativo": ativo}


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
    resultado = _executar(
        _cliente(cli).resolver_evento, _tenant(utilizador), evento_id, dados.nota, ator_de(utilizador, "verificacoes")
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
    resultado = _executar(
        _cliente(cli).decidir_alerta,
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
    est = _executar(_cliente(cli).estado, _tenant(utilizador), fonte)
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

    c = _cliente(cli)
    _executar(
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
        _executar(c.reavaliar_observacoes, _tenant(utilizador), _perfil_qnrcs(empresa), _declaracoes(db, empresa))
    except HTTPException:
        pass
    return {"fonte": fonte, "sinais_config_json": dados.sinais_config_json or "{}"}
