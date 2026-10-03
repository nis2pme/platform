"""
Router da Análise de Risco (premium) — superfície FINA, com três gates que
se acumulam:
  - require_feature("risk_analysis")  → o tenant tem o módulo? (402)
  - require_capability("risco", …)    → o papel pode esta classe de ação? (403)
  - Ator (âmbito) no sidecar          → o implementador só escreve os seus riscos

Governação (classe `governar`, inclui o CEO): definir o apetite ao risco e
aceitar riscos (tratamento tipo="aceitar") são decisões do órgão de gestão —
não de quem opera o dia-a-dia.

Passthrough para o sidecar (a autoridade — deriva nível/classe, valida, faz o
tenant-scoping e é dono da premium-data-db). O core resolve identidades (dono
validado contra o tenant; nome vem sempre da BD) e regista a auditoria no
core-db após sucesso.
Rotas `def`: a base e o gRPC são síncronos e correm no threadpool, fora do
event loop.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import select

from app.controlos.service import ControloCitado, controlos_citados
from app.frameworks.models import ControloEmpresaV2
from app.premium.client import (PremiumIndisponivelError,
                                e_indisponibilidade)
from app.premium.client import e_valor_fora_do_contrato
from app.premium.recusas import recusa_de_licenca
from app.premium.atores import ator_de, negar_capacidade, registar_recusa_de_recurso, resolver_pessoa
from app.premium.dependencies import require_feature
from app.premium.risco_client import RiscoClient, get_risco_client
from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability, tem_capacidade
from app.shared.dependencies import CurrentUserDep, SessionDep, get_empresa_ativa
from app.shared.enums import EstadoControlo
from app.shared.i18n import MsgsI18n, locale_de_request, traduzir
from app.shared.utils import parse_accept_language

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/risco",
    tags=["Análise de Risco"],
    dependencies=[
        Depends(require_capability("risco", ClasseAcao.VER)),
        Depends(require_feature("risk_analysis")),
    ],
)

RiscoDep = Depends(get_risco_client)
OperarDep = Depends(require_capability("risco", ClasseAcao.OPERAR))
EliminarDep = Depends(require_capability("risco", ClasseAcao.ELIMINAR))
GovernarDep = Depends(require_capability("risco", ClasseAcao.GOVERNAR))


# ── Schemas de entrada ────────────────────────────────────────────────────────

class RiscoIn(BaseModel):
    titulo: str
    descricao: str = ""
    ativo_id: str = ""
    ameaca: str = ""
    vulnerabilidade: str = ""
    probabilidade: int = 1
    impacto: int = 1
    estado: str = "aberto"
    dono_id: str = ""
    dono_nome: str = ""
    justificacao: str = ""
    cenario_chave: str = ""


class ReavaliarIn(BaseModel):
    probabilidade: int
    impacto: int
    justificacao: str = ""


class DefinicoesIn(BaseModel):
    limiar_tratar: int = 10
    limiar_urgente: int = 15
    aprovador: str = ""
    data_aprovacao: str = ""
    periodicidade_altos: int = 3
    periodicidade_moderados: int = 6
    periodicidade_baixos: int = 12


# Comprimento mínimo da justificação ao ACEITAR um risco. O mesmo critério que
# a exclusão de um controlo do âmbito já usa: são as duas decisões que tiram algo
# das contas de conformidade sem que nada seja feito, e a única coisa que as
# torna contestáveis por um auditor é o motivo ficar escrito.
JUSTIFICACAO_ACEITACAO_MIN = 10


class TratamentoIn(BaseModel):
    tipo: str = "mitigar"
    controlo_id: str = ""
    descricao: str = ""
    estado: str = "planeado"
    prioridade: int = 0
    data_alvo: str = ""
    responsavel_id: str = ""
    responsavel_nome: str = ""


def _exigir_justificacao_de_aceitacao(tipo: str, descricao: str) -> None:
    """Aceitar um risco tem de dizer porquê; os outros tratamentos não.

    Mitigar, transferir ou evitar são operação — alguém vai fazer alguma coisa, e
    a descrição é conveniência. **Aceitar é a única forma de um risco sair das
    contas sem que nada seja feito**, e por isso é a única que a matriz classifica
    como governação. Um registo de aceitação sem motivo diz ao auditor que a
    decisão foi tomada e não diz porquê — que é o mesmo que não a registar.
    """
    if tipo != "aceitar":
        return
    if len((descricao or "").strip()) < JUSTIFICACAO_ACEITACAO_MIN:
        raise HTTPException(
            status_code=422,
            detail={
                "codigo": "justificacao_aceitacao_obrigatoria",
                "minimo": JUSTIFICACAO_ACEITACAO_MIN,
            },
        )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cliente(cli: RiscoClient | None) -> RiscoClient:
    if cli is None:
        raise HTTPException(status_code=503, detail={"codigo": "premium_indisponivel"})
    return cli


def _titulo_do_risco(cli, tenant: str, risco_id: str) -> str | None:
    """
    Título do risco a que um tratamento pertence, só para o registo.

    Um tratamento não tem nome próprio, e sem isto a trilha guardava apenas o
    identificador do risco: quem a lesse teria de cruzar UUIDs à mão — e, se o
    risco entretanto desaparecesse, não teria como.

    À prova de falha: o título é um extra e nunca pode fazer cair a operação.
    """
    try:
        risco = _executar(_cliente(cli).obter, tenant, risco_id)
        return risco.get("titulo") or None
    except Exception:  # noqa: BLE001 — o título é um extra, nunca um requisito
        return None


def _locale(request: Request) -> str:
    lang = (request.headers.get("Accept-Language") or "").lower()
    return "en" if lang.startswith("en") else "pt-PT"


def _uuid_ou_none(valor: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(valor)
    except (ValueError, TypeError):
        return None


def _exigir_delegar(utilizador, request: Request | None = None) -> None:
    """Atribuir um risco a outra pessoa é delegação — reservada à gestão."""
    if not tem_capacidade(utilizador, "risco", ClasseAcao.DELEGAR):
        raise negar_capacidade(utilizador, "risco", ClasseAcao.DELEGAR, request)


def _exigir_capacidade(utilizador, classe: ClasseAcao, request: Request | None = None) -> None:
    """Verificação inline (para regras que dependem do payload)."""
    if not tem_capacidade(utilizador, "risco", classe):
        raise negar_capacidade(utilizador, "risco", classe, request)


def _classe_para_tratamento(*tipos: str) -> ClasseAcao:
    """Aceitar um risco é decisão de gestão (`governar`, inclui o CEO); os
    restantes tipos de tratamento são operação do dia-a-dia (`operar`)."""
    return (
        ClasseAcao.GOVERNAR if "aceitar" in tipos else ClasseAcao.OPERAR
    )


def _maturidades(db, empresa_id) -> dict[str, int]:
    """Mapa {controlo_id → nível de maturidade} do tenant, para o sidecar calcular o
    risco residual (a maturidade é do core; o sidecar nunca lê o core-db). Com a
    resolução dos controlos citados pelo risco (abaixo), é a única lógica de
    domínio do core nos módulos premium: ambas leem controlos, que são do core."""
    rows = db.exec(
        select(
            ControloEmpresaV2.id,
            ControloEmpresaV2.control_id,
            ControloEmpresaV2.nivel_maturidade_atual,
            ControloEmpresaV2.estado,
        ).where(ControloEmpresaV2.empresa_id == empresa_id)
    ).all()
    # O tratamento guarda o id do controlo que o ecrã lhe deu, que é o do quadro
    # (o de `/controlos`); dados antigos podem ter o da empresa. Os dois servem.
    mapa: dict[str, int] = {}
    for ce_id, control_id, nivel, estado in rows:
        # Um controlo «não aplicável» saiu do âmbito: a maturidade que tinha não
        # se apaga ao marcá-lo, mas já não protege nada, por isso não reduz o
        # residual. Fora do mapa, o sidecar trata-o como um controlo sem maturidade.
        if estado == EstadoControlo.NAO_APLICAVEL:
            continue
        mapa[str(ce_id)] = int(nivel or 0)
        if control_id is not None:
            mapa.setdefault(str(control_id), int(nivel or 0))
    return mapa


def _controlos_citados(
    db,
    utilizador,
    request: Request,
    *,
    codigos: set[str] | None = None,
    ids: set[uuid.UUID] | None = None,
) -> list[ControloCitado]:
    """Os controlos que o risco cita, com a visibilidade do módulo de controlos.

    O título vem na língua do pedido, como na listagem de controlos. À prova de
    falha: sem referencial ativo (instalação por semear) o risco continua a
    responder, só sem os controlos; numa validação, isso conta como não
    encontrado.
    """
    if not codigos and not ids:
        return []
    try:
        empresa = get_empresa_ativa(db, utilizador)
        return controlos_citados(
            db,
            empresa,
            utilizador,
            codigos=codigos,
            ids=ids,
            locale=parse_accept_language(request.headers.get("accept-language")),
        )
    except HTTPException as exc:
        logger.warning(
            "Controlos do risco sem referencial da empresa %s: %s",
            utilizador.empresa_id, exc.detail,
        )
        return []


def _controlo_sugerido(c: ControloCitado) -> dict:
    """Um controlo sugerido por um cenário. Estado, maturidade e perfil vêm a
    None para quem não vê o controlo (já assim desde `controlos_citados`)."""
    return {
        "codigo": c.codigo,
        "controlo_id": str(c.control_id),
        "titulo": c.titulo,
        "visivel": c.visivel,
        "estado": c.estado,
        "nivel_maturidade": c.nivel_maturidade,
        "obrigatorio_perfil": c.obrigatorio_perfil,
    }


def _controlo_validado(db, utilizador, request: Request, controlo_id: str) -> str:
    """O controlo de um tratamento: vazio, ou um controlo do referencial da empresa.

    Aceita os dois ids que existem, como o cálculo do residual: o do quadro (o
    que o ecrã guarda) e, para dados antigos, o da empresa. Qualquer outro valor
    (de outra empresa, de outro referencial, ou texto solto) é recusado: ficaria
    guardado sem nunca reduzir o residual nem aparecer na ficha. Não depende da
    visibilidade: quem liga um controlo sugerido não tem de o ter delegado.
    Devolve o id na forma canónica, a mesma com que o mapa de maturidades o
    procura.
    """
    if not controlo_id:
        return ""
    uid = _uuid_ou_none(controlo_id)
    if uid is not None and _controlos_citados(db, utilizador, request, ids={uid}):
        return str(uid)
    raise HTTPException(
        status_code=400,
        detail={
            "codigo": "controlo_invalido",
            "mensagem": traduzir(
                MsgsI18n.RISCO_CONTROLO_INVALIDO, locale_de_request(request)
            ),
        },
    )


def _executar(fn, *args, utilizador=None):
    """Faz a chamada gRPC e traduz os erros em HTTP.

    `utilizador` só é passado nas escritas: é quando o sidecar pode recusar por
    âmbito (o registo não está atribuído ao ator), e é essa recusa que fica na
    trilha. Nas leituras não há âmbito a violar, por isso fica de fora."""
    try:
        return fn(*args)
    except HTTPException:
        raise
    except PremiumIndisponivelError:
        # O sidecar não está utilizável: canal por montar, material de TLS em
        # falta, transporte ausente. É indisponibilidade do módulo, não avaria da
        # plataforma — e a diferença é a que o cliente precisa de ver para saber
        # se age (renovar a licença, verificar a rede) ou se reporta um defeito.
        # Antes escapava daqui e saía 500 em todas as rotas premium.
        raise HTTPException(status_code=503, detail={"codigo": "premium_indisponivel"})
    except Exception as exc:  # noqa: BLE001 — traduzido abaixo
        if e_valor_fora_do_contrato(exc):
            raise HTTPException(status_code=400, detail={"codigo": "valor_fora_de_intervalo"}) from exc
        recusa = recusa_de_licenca(exc)
        if recusa is not None:
            raise recusa from exc
        try:
            import grpc  # type: ignore
        except ImportError:
            raise
        if isinstance(exc, grpc.RpcError):
            code = exc.code()
            if code == grpc.StatusCode.NOT_FOUND:
                raise HTTPException(status_code=404, detail={"codigo": "nao_encontrado"})
            if code == grpc.StatusCode.PERMISSION_DENIED:
                # Âmbito do ator: o registo não lhe está atribuído.
                if utilizador is not None:
                    registar_recusa_de_recurso(utilizador, "risco")
                raise HTTPException(
                    status_code=403, detail={"codigo": "sem_permissao_recurso"}
                )
            if code == grpc.StatusCode.INVALID_ARGUMENT:
                raise HTTPException(
                    status_code=400, detail={"codigo": "risco_invalido", "msg": exc.details()}
                )
            if code == grpc.StatusCode.FAILED_PRECONDITION:
                # Estado que impede a ação, corrigível por quem pediu → 409.
                # Um 5xx aqui diria que a plataforma avariou, e não avariou.
                raise HTTPException(
                    status_code=409,
                    detail={"codigo": "estado_invalido", "msg": exc.details()},
                )
            if e_indisponibilidade(exc):
                # Sidecar em baixo ou pendurado. O 502 dizia «o upstream
                # respondeu mal»; aqui não respondeu de todo. Sem isto, a mesma
                # avaria saía 502 ou 503 conforme a cache de entitlements
                # estivesse quente — e um alerta não se constrói sobre isso.
                raise HTTPException(
                    status_code=503, detail={"codigo": "premium_indisponivel"}
                )
            raise HTTPException(status_code=502, detail={"codigo": "risco_erro"})
        raise


# ── Catálogo de cenários ─────────────────────────────────────────────────────

@router.get("/cenarios", summary="Cenários de risco por tipo de ativo (catálogo)")
def listar_cenarios(
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    tipo: str = "",
    cli: RiscoClient | None = RiscoDep,
):
    tenant = str(utilizador.empresa_id)
    cenarios = _executar(_cliente(cli).listar_cenarios, tenant, tipo, _locale(request))
    # Os códigos sugeridos pelo catálogo, resolvidos no quadro da empresa: código
    # e título para todos; estado e maturidade só para quem vê o controlo.
    codigos = {
        codigo for c in cenarios for codigo in (c.get("controlos_sugeridos") or [])
    }
    por_codigo = {
        citado.codigo: citado
        for citado in _controlos_citados(db, utilizador, request, codigos=codigos)
    }
    fora_do_quadro: set[str] = set()
    for cenario in cenarios:
        sugeridos = cenario.get("controlos_sugeridos") or []
        cenario["controlos"] = [
            _controlo_sugerido(por_codigo[codigo])
            for codigo in sugeridos
            if codigo in por_codigo
        ]
        fora_do_quadro.update(codigo for codigo in sugeridos if codigo not in por_codigo)
    if fora_do_quadro:
        # O catálogo e o referencial desencontraram-se: a sugestão fica de fora
        # em vez de apontar para um controlo que a empresa não tem.
        logger.warning(
            "Cenários de risco sugerem controlos fora do referencial da empresa %s: %s",
            utilizador.empresa_id, ", ".join(sorted(fora_do_quadro)),
        )
    return cenarios


# ── Riscos (leitura) ─────────────────────────────────────────────────────────

@router.get("/riscos", summary="Listar riscos (ordenados por nível; filtros opcionais)")
def listar_riscos(
    utilizador: CurrentUserDep,
    estado: str = "",
    ativo_id: str = "",
    limite: int = 200,
    offset: int = 0,
    cli: RiscoClient | None = RiscoDep,
):
    tenant = str(utilizador.empresa_id)
    return _executar(_cliente(cli).listar, tenant, estado, ativo_id, limite, offset)


@router.get("/painel", summary="Indicadores + matriz de risco")
def obter_painel(utilizador: CurrentUserDep, cli: RiscoClient | None = RiscoDep):
    tenant = str(utilizador.empresa_id)
    return _executar(_cliente(cli).obter_painel, tenant)


@router.get("/atencao", summary="Painel 'A precisar de atenção' (alertas agregados)")
def obter_atencao(
    request: Request, utilizador: CurrentUserDep, cli: RiscoClient | None = RiscoDep
):
    tenant = str(utilizador.empresa_id)
    return _executar(_cliente(cli).obter_atencao, tenant, _locale(request))


@router.get("/documentos/{tipo}", summary="Gerar documento-evidência (payload localizado)")
def gerar_documento(
    tipo: str,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: RiscoClient | None = RiscoDep,
):
    from app.premium.anexar_evidencia import enriquecer_documento
    from app.shared.dependencies import get_empresa_ativa

    tenant = str(utilizador.empresa_id)
    doc = _executar(_cliente(cli).gerar_documento, tenant, tipo, _locale(request))
    # Enriquecer com hash estável + controlo-alvo para o "anexar como evidência".
    return enriquecer_documento(db, get_empresa_ativa(db, utilizador), doc)


@router.get("/definicoes", summary="Apetite ao risco + periodicidades")
def obter_definicoes(utilizador: CurrentUserDep, cli: RiscoClient | None = RiscoDep):
    tenant = str(utilizador.empresa_id)
    return _executar(_cliente(cli).obter_definicoes, tenant)


@router.get("/riscos/{risco_id}", summary="Ficha de um risco (com tratamentos + residual)")
def obter_risco(
    risco_id: str,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: RiscoClient | None = RiscoDep,
):
    tenant = str(utilizador.empresa_id)
    maturidades = _maturidades(db, utilizador.empresa_id)
    risco = _executar(_cliente(cli).obter, tenant, risco_id, maturidades)
    # Cada tratamento diz a que controlo está ligado (código e título, para
    # todos) e se quem pede o vê no módulo de controlos. Os dois ids servem,
    # como no mapa de maturidades; vazio ou desconhecido fica sem código.
    tratamentos = risco.get("tratamentos") or []
    ids = {
        uid
        for t in tratamentos
        if (uid := _uuid_ou_none(t.get("controlo_id") or "")) is not None
    }
    por_id: dict[uuid.UUID, ControloCitado] = {}
    for citado in _controlos_citados(db, utilizador, request, ids=ids):
        por_id[citado.control_id] = citado
        por_id[citado.controlo_empresa_id] = citado
    for t in tratamentos:
        uid = _uuid_ou_none(t.get("controlo_id") or "")
        citado = por_id.get(uid) if uid is not None else None
        t["controlo_codigo"] = citado.codigo if citado else None
        t["controlo_titulo"] = citado.titulo if citado else None
        t["controlo_visivel"] = citado.visivel if citado else False
    return risco


@router.get("/riscos/{risco_id}/avaliacoes", summary="Histórico de avaliações")
def listar_avaliacoes(risco_id: str, utilizador: CurrentUserDep, cli: RiscoClient | None = RiscoDep):
    tenant = str(utilizador.empresa_id)
    return _executar(_cliente(cli).listar_avaliacoes, tenant, risco_id)


@router.get("/riscos/{risco_id}/procedencia", summary="O que as importações mudaram neste risco")
def procedencia_risco(risco_id: str, utilizador: CurrentUserDep):
    """Mesma razão do lado do inventário: o que se mostra são valores DO RISCO,
    por isso o portão é o do módulo de risco e não o da importação."""
    from app.premium.importacao_client import get_importacao_client

    cli = get_importacao_client()
    if cli is None:
        return {"alteracoes": [], "transferencias": []}
    return _executar(
        cli.historico_registo, str(utilizador.empresa_id), risco_id, "risco"
    )


# ── Definições (governação: apetite ao risco) ────────────────────────────────

@router.put(
    "/definicoes",
    summary="Guardar apetite ao risco + periodicidades",
    dependencies=[GovernarDep],
)
def guardar_definicoes(
    dados: DefinicoesIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: RiscoClient | None = RiscoDep,
):
    tenant = str(utilizador.empresa_id)
    resultado = _executar(
        _cliente(cli).guardar_definicoes, tenant, dados.model_dump(),
        # Definir o apetite ao risco é governação, e é do órgão de gestão. O ator
        # tem de ir descrito por essa classe: pela de operação, o CEO — que
        # governa mas não opera — chegaria ao sidecar como "atribuido" e seria
        # recusado na única decisão que lhe compete.
        ator_de(utilizador, "risco", ClasseAcao.GOVERNAR),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.RISCO_APETITE_DEFINIDO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="DefinicoesRisco",
        dados_novos=dados.model_dump(),
        request=request,
    )
    return resultado


# ── Riscos (escrita) ─────────────────────────────────────────────────────────

@router.post(
    "/riscos",
    status_code=status.HTTP_201_CREATED,
    summary="Criar risco",
    dependencies=[OperarDep],
)
def criar_risco(
    dados: RiscoIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: RiscoClient | None = RiscoDep,
):
    tenant = str(utilizador.empresa_id)
    # Dono do risco validado contra o tenant; sem indicação → o próprio.
    dono_id, dono_nome, delegou = resolver_pessoa(
        db, utilizador, dados.dono_id, dados.dono_nome
    )
    if delegou:
        _exigir_delegar(utilizador, request)
    payload = dados.model_dump()
    payload["dono_id"] = dono_id
    payload["dono_nome"] = dono_nome

    resultado = _executar(
        _cliente(cli).guardar, tenant, payload, "", ator_de(utilizador, "risco"),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.RISCO_CRIADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Risco",
        entidade_id=_uuid_ou_none(resultado.get("id", "")),
        dados_novos=payload,
        request=request,
    )
    if delegou:
        registar_acao(
            db,
            acao=Acao.RISCO_DONO_ATRIBUIDO,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            entidade_tipo="Risco",
            entidade_id=_uuid_ou_none(resultado.get("id", "")),
            # O título do RISCO tem de ir: sem ele o registo guardava só o nome
            # do novo dono, e lia-se como se a ação fosse sobre a pessoa.
            dados_novos={
                "titulo": payload.get("titulo"),
                "dono_id": dono_id,
                "dono_nome": dono_nome,
            },
            request=request,
        )
    return resultado


@router.put("/riscos/{risco_id}", summary="Atualizar campos de um risco", dependencies=[OperarDep])
def atualizar_risco(
    risco_id: str,
    dados: RiscoIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: RiscoClient | None = RiscoDep,
):
    tenant = str(utilizador.empresa_id)
    # Estado atual do sidecar: necessário para detetar mudança de dono
    # (delegação) sem confiar no cliente. Custo de 1 RPC extra aceite.
    atual = _executar(_cliente(cli).obter, tenant, risco_id, None)
    dono_id, dono_nome, mudou = resolver_pessoa(
        db,
        utilizador,
        dados.dono_id,
        dados.dono_nome,
        atual_id=atual.get("dono_id", ""),
        atual_nome=atual.get("dono_nome", ""),
    )
    if mudou:
        _exigir_delegar(utilizador, request)
    payload = dados.model_dump()
    payload["dono_id"] = dono_id
    payload["dono_nome"] = dono_nome

    resultado = _executar(
        _cliente(cli).guardar, tenant, payload, risco_id, ator_de(utilizador, "risco"),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.RISCO_ATUALIZADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Risco",
        entidade_id=_uuid_ou_none(risco_id),
        dados_novos=payload,
        request=request,
    )
    if mudou:
        registar_acao(
            db,
            acao=Acao.RISCO_DONO_ATRIBUIDO,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            entidade_tipo="Risco",
            entidade_id=_uuid_ou_none(risco_id),
            # O título do RISCO tem de ir: sem ele o registo guardava só o nome
            # do novo dono, e lia-se como se a ação fosse sobre a pessoa.
            dados_novos={
                "titulo": payload.get("titulo"),
                "dono_id": dono_id,
                "dono_nome": dono_nome,
            },
            request=request,
        )
    return resultado


@router.delete(
    "/riscos/{risco_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar risco",
    dependencies=[EliminarDep],
)
def eliminar_risco(
    risco_id: str,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: RiscoClient | None = RiscoDep,
):
    tenant = str(utilizador.empresa_id)
    _executar(
        _cliente(cli).eliminar, tenant, risco_id,
        ator_de(utilizador, "risco", ClasseAcao.ELIMINAR),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.RISCO_ELIMINADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Risco",
        entidade_id=_uuid_ou_none(risco_id),
        request=request,
    )


@router.post(
    "/riscos/{risco_id}/reavaliar",
    summary="Reavaliar (nova entrada no histórico)",
    dependencies=[OperarDep],
)
def reavaliar(
    risco_id: str,
    dados: ReavaliarIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: RiscoClient | None = RiscoDep,
):
    tenant = str(utilizador.empresa_id)
    ator = ator_de(utilizador, "risco")
    resultado = _executar(
        _cliente(cli).reavaliar, tenant, risco_id, dados.model_dump(), ator["id"], ator["nome"], ator,
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.RISCO_REAVALIADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Risco",
        entidade_id=_uuid_ou_none(risco_id),
        dados_novos=dados.model_dump(),
        request=request,
    )
    return resultado


# ── Tratamentos ──────────────────────────────────────────────────────────────

@router.post(
    "/riscos/{risco_id}/tratamentos",
    status_code=status.HTTP_201_CREATED,
    summary="Adicionar tratamento",
)
def criar_tratamento(
    risco_id: str,
    dados: TratamentoIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: RiscoClient | None = RiscoDep,
):
    tenant = str(utilizador.empresa_id)
    # Gate dinâmico: depende do tipo submetido (aceitar → governar; resto → operar).
    _exigir_capacidade(utilizador, _classe_para_tratamento(dados.tipo), request)
    _exigir_justificacao_de_aceitacao(dados.tipo, dados.descricao)
    controlo_id = _controlo_validado(db, utilizador, request, dados.controlo_id)
    # Responsável do tratamento validado (utilizador do tenant ou pessoa externa);
    # é um encargo de execução, não posse do recurso → não exige `delegar`.
    resp_id, resp_nome, _ = resolver_pessoa(
        db, utilizador, dados.responsavel_id, dados.responsavel_nome, atual_id=""
    )
    payload = dados.model_dump()
    payload["controlo_id"] = controlo_id
    payload["responsavel_id"] = resp_id
    payload["responsavel_nome"] = resp_nome

    resultado = _executar(
        _cliente(cli).guardar_tratamento, tenant, risco_id, payload, "",
        # A mesma classe que autorizou o pedido descreve o ator: aceitar um risco
        # é governação, os restantes tratamentos são operação.
        ator_de(utilizador, "risco", _classe_para_tratamento(dados.tipo)),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.TRATAMENTO_CRIADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Tratamento",
        entidade_id=_uuid_ou_none(resultado.get("id", "")),
        dados_novos={
            "titulo": _titulo_do_risco(cli, tenant, risco_id),
            **payload,
            "risco_id": risco_id,
        },
        request=request,
    )
    return resultado


@router.put("/riscos/{risco_id}/tratamentos/{trat_id}", summary="Atualizar tratamento")
def atualizar_tratamento(
    risco_id: str,
    trat_id: str,
    dados: TratamentoIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: RiscoClient | None = RiscoDep,
):
    tenant = str(utilizador.empresa_id)
    # Aceitação de risco é governação — tanto passar a "aceitar" como reverter
    # uma aceitação existente. Lê o estado atual para cobrir os dois sentidos.
    atual = _executar(_cliente(cli).obter, tenant, risco_id, None)
    tipo_atual = next(
        (t.get("tipo", "") for t in atual.get("tratamentos", []) if t.get("id") == trat_id),
        "",
    )
    _exigir_capacidade(utilizador, _classe_para_tratamento(dados.tipo, tipo_atual), request)
    # Sem isto havia porta lateral: criar como "mitigar" com descrição vazia e
    # depois mudar o tipo para "aceitar" — o gate de governação apanhava a
    # mudança, a justificação não.
    _exigir_justificacao_de_aceitacao(dados.tipo, dados.descricao)
    controlo_id = _controlo_validado(db, utilizador, request, dados.controlo_id)
    resp_id, resp_nome, _ = resolver_pessoa(
        db, utilizador, dados.responsavel_id, dados.responsavel_nome, atual_id=""
    )
    payload = dados.model_dump()
    payload["controlo_id"] = controlo_id
    payload["responsavel_id"] = resp_id
    payload["responsavel_nome"] = resp_nome

    resultado = _executar(
        _cliente(cli).guardar_tratamento, tenant, risco_id, payload, trat_id,
        # Cobre os dois sentidos, como o gate acima: passar a "aceitar" e
        # reverter uma aceitação são ambos decisão de gestão.
        ator_de(utilizador, "risco", _classe_para_tratamento(dados.tipo, tipo_atual)),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.TRATAMENTO_ATUALIZADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Tratamento",
        entidade_id=_uuid_ou_none(trat_id),
        # O risco já foi lido acima para a verificação de permissões — o título
        # vem daí, sem custar uma segunda ida ao sidecar.
        dados_novos={"titulo": atual.get("titulo"), **payload, "risco_id": risco_id},
        request=request,
    )
    return resultado


@router.delete(
    "/riscos/{risco_id}/tratamentos/{trat_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar tratamento",
)
def eliminar_tratamento(
    risco_id: str,
    trat_id: str,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: RiscoClient | None = RiscoDep,
):
    tenant = str(utilizador.empresa_id)
    # Eliminar uma aceitação desfaz a decisão do órgão de gestão: exige GOVERNAR,
    # como criá-la ou revertê-la. Lê-se o tipo do tratamento para saber se é o caso.
    atual = _executar(_cliente(cli).obter, tenant, risco_id, None)
    tipo_atual = next(
        (t.get("tipo", "") for t in atual.get("tratamentos", []) if t.get("id") == trat_id),
        "",
    )
    # Dois caminhos. Quem tem `risco.eliminar` apaga como sempre, e o ator vai
    # descrito pela ELIMINAÇÃO: um papel a quem a empresa deu `eliminar` sem
    # `operar` total chegava ao sidecar como "atribuido" e era recusado na ação
    # que a matriz acabou de lhe autorizar. Sem `eliminar`, quem opera o risco
    # tira do plano os tratamentos dos riscos de que é dono — o sidecar confirma
    # o dono pelo ator de operação. Uma aceitação é decisão de gestão e fica
    # sempre pela via de `eliminar` + `governar`.
    if tem_capacidade(utilizador, "risco", ClasseAcao.ELIMINAR):
        classe_ator = ClasseAcao.ELIMINAR
    elif tipo_atual == "aceitar":
        classe_ator = ClasseAcao.ELIMINAR
        _exigir_capacidade(utilizador, ClasseAcao.ELIMINAR, request)  # recusa
    else:
        _exigir_capacidade(utilizador, ClasseAcao.OPERAR, request)
        classe_ator = ClasseAcao.OPERAR
    if tipo_atual == "aceitar":
        _exigir_capacidade(utilizador, ClasseAcao.GOVERNAR, request)
    _executar(
        _cliente(cli).eliminar_tratamento, tenant, trat_id,
        ator_de(utilizador, "risco", classe_ator),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.TRATAMENTO_ELIMINADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Tratamento",
        entidade_id=_uuid_ou_none(trat_id),
        # É o registo mais importante de todos para ter nome: o tratamento
        # deixou de existir, e sem o título do risco não há como saber a que
        # se referia.
        dados_novos={
            "titulo": _titulo_do_risco(cli, tenant, risco_id),
            "risco_id": risco_id,
        },
        request=request,
    )
