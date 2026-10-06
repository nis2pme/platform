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

from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from app.premium.atores import ator_de, negar_capacidade, resolver_pessoa
from app.premium.dependencies import require_feature
from app.premium.erros import executar_grpc
from app.premium.inventario_client import InventarioClient, get_inventario_client
from app.premium.pedido import cliente_ou_503, locale_do_pedido, uuid_ou_none
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

# Os tetos seguem o uso (nomes curtos, uma localização de uma linha, descrições e
# notas de uma ou duas páginas) e são os mesmos que o sidecar impõe, que é quem
# decide. Sem eles, um ativo com campos de 1 MiB fazia a listagem (até 500 ativos
# numa só mensagem gRPC) deixar de abrir para toda a empresa.
_ID = Annotated[str, Field(max_length=64)]

# O nome de uma pessoa externa (responsável, quem executou a sanitização) não leva
# `max_length`: quem o limita é `resolver_pessoa`, que corta ao teto o nome novo e
# deixa como está o que já estava guardado. Um teto aqui recusava (422) o PUT de um
# ativo cujo responsável, importado de uma fonte ou de antes de haver tetos, passa
# dos 200: o ecrã devolve o registo inteiro.


class AtivoIn(BaseModel):
    tipo: str = Field(max_length=32)
    nome: str = Field(max_length=200)
    descricao: str = Field("", max_length=4000)
    responsavel_id: str = Field("", max_length=64)
    responsavel_nome: str = ""
    localizacao: str = Field("", max_length=500)
    estado: str = Field("em_uso", max_length=32)
    # A criticidade (C/I/D, valor, classe) é definida pela ação dedicada
    # POST /ativos/{id}/criticidade — não passa pelo guardar geral.
    # Quais as chaves é o catálogo do tipo, no sidecar: o mais largo tem 9 campos,
    # e cada valor é um dado curto ou uma lista escrita à mão.
    atributos: dict[Annotated[str, Field(max_length=64)], Annotated[str, Field(max_length=1000)]] = Field(default_factory=dict, max_length=20)


class DependenciasIn(BaseModel):
    depende_de: list[_ID] = Field(default_factory=list, max_length=200)


class ClassificarIn(BaseModel):
    # "assistente" | "detalhado" | "manual" (validado no sidecar)
    modo: str = Field("assistente", max_length=32)
    confidencialidade: int = 0
    integridade: int = 0
    disponibilidade: int = 0
    valor_negocio: int = 0
    criticidade_manual: str = Field("", max_length=32)
    justificacao: str = Field("", max_length=4000)


class RevisaoIn(BaseModel):
    # Todos os ativos que uma página da lista mostra.
    ativo_ids: list[_ID] = Field(default_factory=list, max_length=500)


class SanitizacaoIn(BaseModel):
    # "apagado_seguro" | "disco_destruido" | "devolvido" | "sem_dados" (validado no sidecar)
    metodo: str = Field(max_length=32)
    responsavel_id: str = Field("", max_length=64)
    responsavel_nome: str = ""
    data: str = Field("", max_length=32)     # RFC3339 (vazio = agora)
    nota: str = Field("", max_length=4000)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _nome_do_ativo(inv, tenant: str, ativo_id: str) -> str | None:
    """
    Nome do ativo, só para o registo de auditoria.

    Custa uma leitura ao sidecar em caminhos onde o nome não vem na resposta. É
    deliberadamente à prova de falha: uma trilha sem o nome do ativo é pior do
    que agora, mas uma operação que falha porque a auditoria não conseguiu
    enfeitar o registo é muito pior do que as duas.
    """
    try:
        ativo = _executar(cliente_ou_503(inv).obter_ativo, tenant, ativo_id, "")
        return ativo.get("nome") or None
    except Exception:  # noqa: BLE001 — o nome é um extra, nunca um requisito
        return None


def _exigir_delegar(utilizador, request: Request | None = None) -> None:
    """Atribuir um ativo a outra pessoa é delegação — reservada à gestão."""
    if not tem_capacidade(utilizador, "inventario", ClasseAcao.DELEGAR):
        raise negar_capacidade(utilizador, "inventario", ClasseAcao.DELEGAR, request)


# `utilizador` só nas escritas: a recusa de âmbito do sidecar (registo não
# atribuído ao ator) fica na trilha.
_executar = partial(executar_grpc, modulo="inventario")


# ── Endpoints de leitura ─────────────────────────────────────────────────────

@router.get("/tipos", summary="Catálogo de tipos de ativo (descritores)")
def listar_tipos(
    request: Request,
    utilizador: CurrentUserDep,
    inv: InventarioClient | None = InventarioDep,
):
    tenant = str(utilizador.empresa_id)
    return _executar(cliente_ou_503(inv).listar_tipos, tenant, locale_do_pedido(request))


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
        cliente_ou_503(inv).listar_ativos, tenant, tipo, locale_do_pedido(request), limite, offset
    )


@router.get("/painel", summary="Indicadores do módulo")
def obter_painel(
    request: Request,
    utilizador: CurrentUserDep,
    inv: InventarioClient | None = InventarioDep,
):
    tenant = str(utilizador.empresa_id)
    return _executar(cliente_ou_503(inv).obter_painel, tenant, locale_do_pedido(request))


@router.get("/ativos/{ativo_id}", summary="Ficha de um ativo")
def obter_ativo(
    ativo_id: str,
    request: Request,
    utilizador: CurrentUserDep,
    inv: InventarioClient | None = InventarioDep,
):
    tenant = str(utilizador.empresa_id)
    return _executar(cliente_ou_503(inv).obter_ativo, tenant, ativo_id, locale_do_pedido(request))


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
    doc = _executar(cliente_ou_503(inv).gerar_documento, tenant, tipo, locale_do_pedido(request))
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
        cliente_ou_503(inv).guardar_ativo, tenant, payload, "", ator_de(utilizador, "inventario"),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.ATIVO_CRIADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Ativo",
        entidade_id=uuid_ou_none(resultado.get("id", "")),
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
            entidade_id=uuid_ou_none(resultado.get("id", "")),
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
    atual = _executar(cliente_ou_503(inv).obter_ativo, tenant, ativo_id, locale_do_pedido(request))
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
        cliente_ou_503(inv).guardar_ativo, tenant, payload, ativo_id, ator_de(utilizador, "inventario"),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.ATIVO_ATUALIZADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Ativo",
        entidade_id=uuid_ou_none(ativo_id),
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
            entidade_id=uuid_ou_none(ativo_id),
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
    # O nome lê-se antes: depois de apagado, já não há onde o ir buscar, e uma
    # eliminação sem nome na trilha não diz o que desapareceu.
    nome = _nome_do_ativo(inv, tenant, ativo_id)
    _executar(
        cliente_ou_503(inv).eliminar_ativo, tenant, ativo_id,
        ator_de(utilizador, "inventario", ClasseAcao.ELIMINAR),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.ATIVO_ELIMINADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Ativo",
        entidade_id=uuid_ou_none(ativo_id),
        dados_novos={"nome": nome},
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
        cliente_ou_503(inv).definir_dependencias,
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
        entidade_id=uuid_ou_none(ativo_id),
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
        cliente_ou_503(inv).classificar_criticidade,
        tenant,
        ativo_id,
        dados.model_dump(),
        locale_do_pedido(request),
        ator_de(utilizador, "inventario"),
        utilizador=utilizador,
    )
    registar_acao(
        db,
        acao=Acao.ATIVO_CRITICIDADE_CLASSIFICADA,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Ativo",
        entidade_id=uuid_ou_none(ativo_id),
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
        cliente_ou_503(inv).registar_revisao, tenant, dados.ativo_ids, ator["id"], ator["nome"], ator,
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
        cliente_ou_503(inv).registar_sanitizacao,
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
        entidade_id=uuid_ou_none(ativo_id),
        dados_novos={
            "metodo": dados.metodo,
            "responsavel_nome": resp_nome,
            "data": dados.data or None,
            "nota": dados.nota or None,
        },
        request=request,
    )
    return {"ok": True}
