"""
Router do Inventário de Ativos (premium) — superfície FINA, com três gates que
se acumulam:
  - require_feature("asset_inventory")  → o tenant tem o módulo? (402)
  - require_capability("inventario", …) → o papel pode esta classe de ação? (403)
  - Ator (âmbito) no sidecar            → o implementador só escreve os seus ativos

O core é passthrough: valida a forma (schemas Pydantic), resolve identidades
(responsável validado contra os utilizadores do tenant; nome vem sempre da BD),
delega no sidecar (a autoridade de domínio — valida atributos contra o catálogo,
faz o tenant-scoping e é dono da premium-data-db) e regista a auditoria no
core-db após sucesso.
Rotas `def`: a base e o gRPC são síncronos e correm no threadpool, fora do
event loop.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.premium.client import (PremiumIndisponivelError,
                                e_indisponibilidade)
from app.premium.client import e_valor_fora_do_contrato
from app.premium.recusas import recusa_de_licenca
from app.premium.atores import ator_de, negar_capacidade, registar_recusa_de_recurso, resolver_pessoa
from app.premium.dependencies import require_feature
from app.premium.inventario_client import InventarioClient, get_inventario_client
from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability, tem_capacidade
from app.shared.dependencies import CurrentUserDep, SessionDep

# Gates de router: feature do tenant (402) + leitura para todos os papéis (403).
# As classes de escrita (operar/eliminar/delegar) são exigidas por endpoint.
router = APIRouter(
    prefix="/inventario",
    tags=["Inventário de Ativos"],
    dependencies=[
        Depends(require_capability("inventario", ClasseAcao.VER)),
        Depends(require_feature("asset_inventory")),
    ],
)

InventarioDep = Depends(get_inventario_client)
OperarDep = Depends(require_capability("inventario", ClasseAcao.OPERAR))
EliminarDep = Depends(require_capability("inventario", ClasseAcao.ELIMINAR))


# ── Schemas de entrada (permissivos: comuns tipados + `atributos` livre) ──────

class AtivoIn(BaseModel):
    tipo: str
    nome: str
    descricao: str = ""
    responsavel_id: str = ""
    responsavel_nome: str = ""
    localizacao: str = ""
    estado: str = "em_uso"
    # A criticidade (C/I/D, valor, classe) é definida pela ação dedicada
    # POST /ativos/{id}/criticidade — não passa pelo guardar geral.
    atributos: dict[str, str] = Field(default_factory=dict)


class DependenciasIn(BaseModel):
    depende_de: list[str] = Field(default_factory=list)


class ClassificarIn(BaseModel):
    # "assistente" | "detalhado" | "manual" (validado no sidecar)
    modo: str = "assistente"
    confidencialidade: int = 0
    integridade: int = 0
    disponibilidade: int = 0
    valor_negocio: int = 0
    criticidade_manual: str = ""
    justificacao: str = ""


class RevisaoIn(BaseModel):
    ativo_ids: list[str] = Field(default_factory=list)


class SanitizacaoIn(BaseModel):
    # "apagado_seguro" | "disco_destruido" | "devolvido" | "sem_dados" (validado no sidecar)
    metodo: str
    responsavel_id: str = ""
    responsavel_nome: str = ""
    data: str = ""     # RFC3339 (vazio = agora)
    nota: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _locale(request: Request) -> str:
    """Idioma do pedido para os textos do catálogo (o frontend envia Accept-Language)."""
    lang = (request.headers.get("Accept-Language") or "").lower()
    return "en" if lang.startswith("en") else "pt-PT"


def _cliente(inv: InventarioClient | None) -> InventarioClient:
    if inv is None:
        # Não deve acontecer (o gate exige premium on), mas fail-closed.
        raise HTTPException(status_code=503, detail={"codigo": "premium_indisponivel"})
    return inv


def _uuid_ou_none(valor: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(valor)
    except (ValueError, TypeError):
        return None


def _nome_do_ativo(inv, tenant: str, ativo_id: str) -> str | None:
    """
    Nome do ativo, só para o registo de auditoria.

    Custa uma leitura ao sidecar em caminhos onde o nome não vem na resposta. É
    deliberadamente à prova de falha: uma trilha sem o nome do ativo é pior do
    que agora, mas uma operação que falha porque a auditoria não conseguiu
    enfeitar o registo é muito pior do que as duas.
    """
    try:
        ativo = _executar(_cliente(inv).obter_ativo, tenant, ativo_id, "")
        return ativo.get("nome") or None
    except Exception:  # noqa: BLE001 — o nome é um extra, nunca um requisito
        return None


def _exigir_delegar(utilizador, request: Request | None = None) -> None:
    """Atribuir um ativo a outra pessoa é delegação — reservada à gestão."""
    if not tem_capacidade(utilizador, "inventario", ClasseAcao.DELEGAR):
        raise negar_capacidade(utilizador, "inventario", ClasseAcao.DELEGAR, request)


def _executar(fn, *args, utilizador=None):
    """Faz a chamada gRPC e traduz os erros do
    sidecar em HTTP. Códigos estáveis para o frontend traduzir.

    `utilizador` só nas escritas: a recusa de âmbito do sidecar (registo não
    atribuído ao ator) fica na trilha."""
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
                    registar_recusa_de_recurso(utilizador, "inventario")
                raise HTTPException(
                    status_code=403, detail={"codigo": "sem_permissao_recurso"}
                )
            if code == grpc.StatusCode.INVALID_ARGUMENT:
                raise HTTPException(
                    status_code=400,
                    detail={"codigo": "inventario_invalido", "msg": exc.details()},
                )
            if code == grpc.StatusCode.FAILED_PRECONDITION:
                # Estado que impede a ação, corrigível por quem pediu → 409.
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
            raise HTTPException(status_code=502, detail={"codigo": "inventario_erro"})
        raise


# ── Endpoints de leitura ─────────────────────────────────────────────────────

@router.get("/tipos", summary="Catálogo de tipos de ativo (descritores)")
def listar_tipos(
    request: Request,
    utilizador: CurrentUserDep,
    inv: InventarioClient | None = InventarioDep,
):
    tenant = str(utilizador.empresa_id)
    return _executar(_cliente(inv).listar_tipos, tenant, _locale(request))


@router.get("/ativos", summary="Listar ativos (filtro por tipo, paginado)")
def listar_ativos(
    request: Request,
    utilizador: CurrentUserDep,
    tipo: str = "",
    limite: int = 100,
    offset: int = 0,
    inv: InventarioClient | None = InventarioDep,
):
    tenant = str(utilizador.empresa_id)
    return _executar(
        _cliente(inv).listar_ativos, tenant, tipo, _locale(request), limite, offset
    )


@router.get("/painel", summary="Indicadores do módulo")
def obter_painel(
    request: Request,
    utilizador: CurrentUserDep,
    inv: InventarioClient | None = InventarioDep,
):
    tenant = str(utilizador.empresa_id)
    return _executar(_cliente(inv).obter_painel, tenant, _locale(request))


@router.get("/ativos/{ativo_id}", summary="Ficha de um ativo")
def obter_ativo(
    ativo_id: str,
    request: Request,
    utilizador: CurrentUserDep,
    inv: InventarioClient | None = InventarioDep,
):
    tenant = str(utilizador.empresa_id)
    return _executar(_cliente(inv).obter_ativo, tenant, ativo_id, _locale(request))


@router.get("/ativos/{ativo_id}/procedencia", summary="O que as importações mudaram neste ativo")
def procedencia_ativo(
    ativo_id: str,
    utilizador: CurrentUserDep,
):
    """História de proveniência: *porque é que este campo mudou?*

    Vive aqui, e não no módulo de importação, por uma razão de autorização: o
    que se mostra são valores DO ATIVO. Quem pode ver o ativo pode ver como ele
    chegou ao estado em que está; quem não pode, também não pode por esta porta.
    """
    from app.premium.importacao_client import get_importacao_client

    cli = get_importacao_client()
    if cli is None:
        # A importação é um módulo à parte e pode não estar ligada. Um ativo
        # sem história não é um erro — é um ativo feito à mão.
        return {"alteracoes": [], "transferencias": []}
    return _executar(
        cli.historico_registo, str(utilizador.empresa_id), ativo_id, "inventario"
    )


@router.get("/documentos/{tipo}", summary="Gerar documento-evidência (payload localizado)")
def gerar_documento(
    tipo: str,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    inv: InventarioClient | None = InventarioDep,
):
    from app.premium.anexar_evidencia import enriquecer_documento
    from app.shared.dependencies import get_empresa_ativa

    tenant = str(utilizador.empresa_id)
    doc = _executar(_cliente(inv).gerar_documento, tenant, tipo, _locale(request))
    # Enriquecer com hash estável + controlo-alvo para o "anexar como evidência".
    return enriquecer_documento(db, get_empresa_ativa(db, utilizador), doc)


# ── Endpoints de escrita (operar/eliminar; auditados no core-db) ─────────────

@router.post(
    "/ativos",
    status_code=status.HTTP_201_CREATED,
    summary="Criar ativo",
    dependencies=[OperarDep],
)
def criar_ativo(
    dados: AtivoIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    inv: InventarioClient | None = InventarioDep,
):
    tenant = str(utilizador.empresa_id)
    # Responsável validado contra o tenant; sem indicação → o próprio.
    resp_id, resp_nome, delegou = resolver_pessoa(
        db, utilizador, dados.responsavel_id, dados.responsavel_nome
    )
    if delegou:
        _exigir_delegar(utilizador, request)
    payload = dados.model_dump()
    payload["responsavel_id"] = resp_id
    payload["responsavel_nome"] = resp_nome

    resultado = _executar(
        _cliente(inv).guardar_ativo, tenant, payload, "", ator_de(utilizador, "inventario"),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.ATIVO_CRIADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Ativo",
        entidade_id=_uuid_ou_none(resultado.get("id", "")),
        dados_novos=payload,
        request=request,
    )
    if delegou:
        registar_acao(
            db,
            acao=Acao.ATIVO_RESPONSAVEL_ATRIBUIDO,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            entidade_tipo="Ativo",
            entidade_id=_uuid_ou_none(resultado.get("id", "")),
            # O nome do ATIVO tem de ir: sem ele, o registo só guardava o nome
            # do novo responsável e quem lia a trilha via uma pessoa onde devia
            # ver o ativo a que ela foi atribuída.
            dados_novos={
                "nome": payload.get("nome"),
                "responsavel_id": resp_id,
                "responsavel_nome": resp_nome,
            },
            request=request,
        )
    return resultado


@router.put("/ativos/{ativo_id}", summary="Atualizar ativo", dependencies=[OperarDep])
def atualizar_ativo(
    ativo_id: str,
    dados: AtivoIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    inv: InventarioClient | None = InventarioDep,
):
    tenant = str(utilizador.empresa_id)
    # Estado atual do sidecar: necessário para detetar mudança de responsável
    # (delegação) sem confiar no cliente. Custo de 1 RPC extra aceite.
    atual = _executar(_cliente(inv).obter_ativo, tenant, ativo_id, _locale(request))
    resp_id, resp_nome, mudou = resolver_pessoa(
        db,
        utilizador,
        dados.responsavel_id,
        dados.responsavel_nome,
        atual_id=atual.get("responsavel_id", ""),
        atual_nome=atual.get("responsavel_nome", ""),
    )
    if mudou:
        _exigir_delegar(utilizador, request)
    payload = dados.model_dump()
    payload["responsavel_id"] = resp_id
    payload["responsavel_nome"] = resp_nome

    resultado = _executar(
        _cliente(inv).guardar_ativo, tenant, payload, ativo_id, ator_de(utilizador, "inventario"),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.ATIVO_ATUALIZADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Ativo",
        entidade_id=_uuid_ou_none(ativo_id),
        dados_novos=payload,
        request=request,
    )
    if mudou:
        registar_acao(
            db,
            acao=Acao.ATIVO_RESPONSAVEL_ATRIBUIDO,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            entidade_tipo="Ativo",
            entidade_id=_uuid_ou_none(ativo_id),
            dados_novos={
                "nome": payload.get("nome"),
                "responsavel_id": resp_id,
                "responsavel_nome": resp_nome,
            },
            request=request,
        )
    return resultado


@router.delete(
    "/ativos/{ativo_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar ativo",
    dependencies=[EliminarDep],
)
def eliminar_ativo(
    ativo_id: str,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    inv: InventarioClient | None = InventarioDep,
):
    tenant = str(utilizador.empresa_id)
    _executar(
        _cliente(inv).eliminar_ativo, tenant, ativo_id,
        ator_de(utilizador, "inventario", ClasseAcao.ELIMINAR),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.ATIVO_ELIMINADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Ativo",
        entidade_id=_uuid_ou_none(ativo_id),
        request=request,
    )


@router.put(
    "/ativos/{ativo_id}/dependencias",
    summary="Definir dependências de um ativo",
    dependencies=[OperarDep],
)
def definir_dependencias(
    ativo_id: str,
    dados: DependenciasIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    inv: InventarioClient | None = InventarioDep,
):
    tenant = str(utilizador.empresa_id)
    _executar(
        _cliente(inv).definir_dependencias,
        tenant,
        ativo_id,
        dados.depende_de,
        ator_de(utilizador, "inventario"),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.ATIVO_DEPENDENCIAS_DEFINIDAS,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Ativo",
        entidade_id=_uuid_ou_none(ativo_id),
        dados_novos={
            "nome": _nome_do_ativo(inv, tenant, ativo_id),
            "depende_de": dados.depende_de,
        },
        request=request,
    )
    return {"ok": True}


@router.post(
    "/ativos/{ativo_id}/criticidade",
    summary="Classificar a criticidade de um ativo",
    dependencies=[OperarDep],
)
def classificar_criticidade(
    ativo_id: str,
    dados: ClassificarIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    inv: InventarioClient | None = InventarioDep,
):
    tenant = str(utilizador.empresa_id)
    resultado = _executar(
        _cliente(inv).classificar_criticidade,
        tenant,
        ativo_id,
        dados.model_dump(),
        _locale(request),
        ator_de(utilizador, "inventario"),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.ATIVO_CRITICIDADE_CLASSIFICADA,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Ativo",
        entidade_id=_uuid_ou_none(ativo_id),
        # A resposta traz o ativo inteiro — o nome vem de graça e é ele que diz
        # a quem lê a trilha qual dos ativos foi classificado.
        dados_novos={"nome": resultado.get("nome"), **dados.model_dump()},
        request=request,
    )
    return resultado


@router.post(
    "/revisoes",
    summary="Registar revisão de ativos (individual ou em lote)",
    dependencies=[OperarDep],
)
def registar_revisao(
    dados: RevisaoIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    inv: InventarioClient | None = InventarioDep,
):
    tenant = str(utilizador.empresa_id)
    ator = ator_de(utilizador, "inventario")
    _executar(
        _cliente(inv).registar_revisao, tenant, dados.ativo_ids, ator["id"], ator["nome"], ator,
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.ATIVO_REVISAO_REGISTADA,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Ativo",
        dados_novos={"ativo_ids": dados.ativo_ids},
        request=request,
    )
    return {"ok": True}


@router.post(
    "/ativos/{ativo_id}/sanitizacao",
    summary="Registar sanitização e abater um ativo (ID.GA-8)",
    dependencies=[OperarDep],
)
def registar_sanitizacao(
    ativo_id: str,
    dados: SanitizacaoIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    inv: InventarioClient | None = InventarioDep,
):
    """Regista o destino dos dados no abate (método/data/responsável) e põe o ativo
    em 'abatido'. O responsável (quem executou/confirmou) é validado contra os
    utilizadores do tenant — o nome vem da BD; se não for indicado, fica o próprio."""
    tenant = str(utilizador.empresa_id)
    # Quem executou/confirmou: validado contra o tenant; vazio → o próprio utilizador.
    resp_id, resp_nome, _ = resolver_pessoa(
        db, utilizador, dados.responsavel_id, dados.responsavel_nome
    )
    _executar(
        _cliente(inv).registar_sanitizacao,
        tenant,
        ativo_id,
        dados.metodo,
        resp_id,
        resp_nome,
        dados.data,
        dados.nota,
        ator_de(utilizador, "inventario"),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.ATIVO_SANITIZADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Ativo",
        entidade_id=_uuid_ou_none(ativo_id),
        dados_novos={
            "metodo": dados.metodo,
            "responsavel_nome": resp_nome,
            "data": dados.data or None,
            "nota": dados.nota or None,
        },
        request=request,
    )
    return {"ok": True}
