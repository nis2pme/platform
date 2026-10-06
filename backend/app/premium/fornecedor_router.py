"""
Router da Cadeia de Abastecimento / Fornecedores (premium) — superfície FINA, com
três gates que se acumulam:
  - require_feature("supply_chain")     → o tenant tem o módulo? (402)
  - require_capability("fornecedores", …) → o papel pode esta classe de ação? (403)
  - Ator (âmbito) no sidecar            → o implementador só escreve os seus fornecedores

Passthrough para o sidecar (a autoridade — deriva score/classe, valida, faz o
tenant-scoping e é dono da premium-data-db). O core resolve identidades
(responsável validado contra o tenant; nome vem sempre da BD) e regista a
auditoria no core-db após sucesso.
Rotas `def`: a base e o gRPC são síncronos e correm no threadpool, fora do
event loop.
"""
from __future__ import annotations


from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.premium.atores import ator_de, negar_capacidade, resolver_pessoa
from app.premium.dependencies import require_feature
from app.premium.erros import executar_grpc
from app.premium.fornecedor_client import FornecedorClient, get_fornecedor_client
from app.premium.pedido import (cliente_ou_503, fundir_alteracoes, locale_do_pedido,
                                todos_opcionais, uuid_ou_none)
from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability, tem_capacidade
from app.shared.dependencies import CurrentUserDep, SessionDep

router = APIRouter(
    prefix="/fornecedores",
    tags=["Fornecedores"],
    dependencies=[
        Depends(require_capability("fornecedores", ClasseAcao.VER)),
        Depends(require_feature("supply_chain")),
    ],
)

FornecedorDep = Depends(get_fornecedor_client)
OperarDep = Depends(require_capability("fornecedores", ClasseAcao.OPERAR))
EliminarDep = Depends(require_capability("fornecedores", ClasseAcao.ELIMINAR))


# ── Schemas de entrada ────────────────────────────────────────────────────────

# Os tetos seguem o uso (nomes e contactos curtos, notas de uma ou duas páginas) e
# são os mesmos que o sidecar impõe, que é quem decide. Sem eles, um fornecedor com
# campos de 1 MiB fazia a listagem (até 500 linhas numa só mensagem gRPC) deixar de
# abrir para toda a empresa.
_CHAVE = Annotated[str, Field(max_length=64)]


class FornecedorIn(BaseModel):
    ativo_id: str = Field("", max_length=64)
    nome: str = Field(max_length=200)
    servico: str = Field("", max_length=500)
    contacto: str = Field("", max_length=300)
    criticidade: str = Field("", max_length=32)
    estado: str = Field("ativo", max_length=32)
    due_diligence: bool = False
    due_diligence_nota: str = Field("", max_length=4000)
    due_diligence_data: str = Field("", max_length=32)
    # Cláusula -> «true»/«false». Que cláusulas existem é o sidecar que sabe.
    requisitos: dict[_CHAVE, Annotated[str, Field(max_length=8)]] = Field(
        default_factory=dict, max_length=50
    )
    pessoal_chave: str = Field("", max_length=4000)
    termino_nota: str = Field("", max_length=4000)
    acessos_revogados: bool = False
    dados_destino: str = Field("", max_length=32)
    encerrado_em: str = Field("", max_length=32)
    responsavel_id: str = Field("", max_length=64)
    responsavel_nome: str = Field("", max_length=200)


# Os textos opcionais, que um PATCH limpa mandando `null` (é o mesmo que mandar
# vazio). Deduz-se do modelo, para um campo novo não ficar de fora: o que é texto
# e tem o vazio por omissão limpa-se. O que é obrigatório (`nome`), tem outro valor
# por omissão (`estado`), é sim/não ou é um mapa (`requisitos`) leva 422 com `null`:
# apagar o estado ou as cláusulas de um contrato por engano não é «limpar».
_TEXTOS_LIMPAVEIS = frozenset(
    n for n, f in FornecedorIn.model_fields.items() if f.annotation is str and f.default == ""
)

# O PATCH aceita qualquer subconjunto destes campos (o mesmo modelo, tudo opcional).
FornecedorPatch = todos_opcionais(FornecedorIn, "FornecedorPatch", anulaveis=_TEXTOS_LIMPAVEIS)

# A pessoa responsável é um par: o identificador da conta e o nome (de uma pessoa
# externa, sem conta). Alterar um invalida o outro.
_PAR_RESPONSAVEL = (("responsavel_id", "responsavel_nome"),)


class AvaliacaoIn(BaseModel):
    # Pergunta -> resposta (0 a 3). Que perguntas existem é o sidecar que sabe.
    respostas: dict[_CHAVE, int] = Field(default_factory=dict, max_length=100)
    nota: str = Field("", max_length=4000)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _registar_responsavel(db, utilizador, request, fornecedor: dict, resp_id: str, resp_nome: str) -> None:
    """Passar um fornecedor a outra pessoa é delegação, e deixa o mesmo rasto que
    no inventário e no risco: o nome do FORNECEDOR vai (sem ele, o registo só
    guardava o nome da pessoa, e lia-se como se a ação fosse sobre ela)."""
    registar_acao(
        db, acao=Acao.FORNECEDOR_RESPONSAVEL_ATRIBUIDO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Fornecedor",
        entidade_id=uuid_ou_none(fornecedor.get("id")),
        dados_novos={
            "nome": fornecedor.get("nome"),
            "responsavel_id": resp_id,
            "responsavel_nome": resp_nome,
        },
        request=request,
    )


def _exigir_delegar(utilizador, request: Request | None = None) -> None:
    """Atribuir um fornecedor a outra pessoa é delegação — reservada à gestão."""
    if not tem_capacidade(utilizador, "fornecedores", ClasseAcao.DELEGAR):
        raise negar_capacidade(utilizador, "fornecedores", ClasseAcao.DELEGAR, request)


# `utilizador` só nas escritas: a recusa de âmbito do sidecar (registo não
# atribuído ao ator) fica na trilha. Os códigos genéricos levam «fornecedor».
_executar = partial(executar_grpc, modulo="fornecedores", prefixo="fornecedor")


# ── Leitura ──────────────────────────────────────────────────────────────────

@router.get("", summary="Listar fornecedores")
def listar(
    request: Request,
    utilizador: CurrentUserDep,
    estado: str = "",
    limite: int = 0,
    offset: int = 0,
    cli: FornecedorClient | None = FornecedorDep,
):
    c = cliente_ou_503(cli)
    return _executar(
        c.listar, str(utilizador.empresa_id), estado, locale_do_pedido(request), limite, offset
    )


@router.get("/painel", summary="Indicadores do módulo")
def painel(utilizador: CurrentUserDep, cli: FornecedorClient | None = FornecedorDep):
    c = cliente_ou_503(cli)
    return _executar(c.obter_painel, str(utilizador.empresa_id))


@router.get("/questionario", summary="Perguntas da avaliação de fornecedores")
def questionario(
    request: Request, utilizador: CurrentUserDep, cli: FornecedorClient | None = FornecedorDep
):
    c = cliente_ou_503(cli)
    return _executar(c.listar_questionario, str(utilizador.empresa_id), locale_do_pedido(request))


@router.get("/documento", summary="Registo de fornecedores (payload localizado)")
def documento(
    request: Request, utilizador: CurrentUserDep, db: SessionDep,
    cli: FornecedorClient | None = FornecedorDep,
):
    from app.premium.anexar_evidencia import enriquecer_documento
    from app.shared.dependencies import get_empresa_ativa

    c = cliente_ou_503(cli)
    doc = _executar(
        c.gerar_documento, str(utilizador.empresa_id), "registo_fornecedores", locale_do_pedido(request)
    )
    # Hash estável + controlo-alvo para o "anexar como evidência" num clique.
    doc = enriquecer_documento(db, get_empresa_ativa(db, utilizador), doc)
    # A exportação do registo fica registada (mesma prática dos relatórios do core).
    registar_acao(
        db, acao=Acao.RELATORIO_EXPORTADO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Fornecedor", entidade_id=None,
        dados_novos={"tipo": "registo_fornecedores"}, request=request,
    )
    db.commit()
    return doc


@router.get("/{fornecedor_id}", summary="Detalhe de um fornecedor")
def obter(
    fornecedor_id: str, utilizador: CurrentUserDep, cli: FornecedorClient | None = FornecedorDep
):
    c = cliente_ou_503(cli)
    return _executar(c.obter, str(utilizador.empresa_id), fornecedor_id)


@router.get("/{fornecedor_id}/avaliacoes", summary="Histórico de avaliações")
def avaliacoes(
    fornecedor_id: str, utilizador: CurrentUserDep, cli: FornecedorClient | None = FornecedorDep
):
    c = cliente_ou_503(cli)
    return _executar(c.listar_avaliacoes, str(utilizador.empresa_id), fornecedor_id)


# ── Escrita ──────────────────────────────────────────────────────────────────

@router.post("", status_code=201, summary="Registar um fornecedor", dependencies=[OperarDep])
def criar(
    dados: FornecedorIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: FornecedorClient | None = FornecedorDep,
):
    c = cliente_ou_503(cli)
    resp_id, resp_nome, mudou = resolver_pessoa(
        db, utilizador, dados.responsavel_id, dados.responsavel_nome
    )
    if mudou:
        _exigir_delegar(utilizador, request)
    payload = dados.model_dump()
    payload["responsavel_id"] = resp_id
    payload["responsavel_nome"] = resp_nome
    resultado = _executar(
        c.guardar, str(utilizador.empresa_id), payload, "", ator_de(utilizador, "fornecedores"),
        utilizador=utilizador,
    )
    registar_acao(
        db, acao=Acao.FORNECEDOR_CRIADO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Fornecedor",
        entidade_id=uuid_ou_none(resultado.get("id")),
        dados_novos={"nome": resultado.get("nome"), "criticidade": resultado.get("criticidade")},
        request=request,
    )
    if mudou:
        _registar_responsavel(db, utilizador, request, resultado, resp_id, resp_nome)
    db.commit()
    return resultado


@router.patch("/{fornecedor_id}", summary="Atualizar um fornecedor", dependencies=[OperarDep])
def atualizar(
    fornecedor_id: str,
    dados: FornecedorPatch,  # type: ignore[valid-type]
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: FornecedorClient | None = FornecedorDep,
):
    c = cliente_ou_503(cli)
    atual = _executar(c.obter, str(utilizador.empresa_id), fornecedor_id)
    # O sidecar grava por substituição: manda-se o registo atual com só o que o
    # corpo traz. O que não vem no corpo fica; `null` num texto limpa-o.
    atual_editavel = {
        k: v for k, v in atual.items() if k in FornecedorIn.model_fields and v is not None
    }
    alteracoes = {
        k: "" if v is None else v for k, v in dados.model_dump(exclude_unset=True).items()
    }
    # Sem revalidar o registo inteiro: o corpo já passou pelos tetos do
    # `FornecedorPatch`, e o que estava guardado antes deles não pode impedir de
    # alterar outro campo. O sidecar aplica os tetos só ao que muda.
    novo = FornecedorIn.model_construct(
        **fundir_alteracoes(atual_editavel, alteracoes, pares=_PAR_RESPONSAVEL)
    )
    resp_id, resp_nome, mudou = resolver_pessoa(
        db, utilizador, novo.responsavel_id, novo.responsavel_nome,
        atual_id=atual.get("responsavel_id") or "", atual_nome=atual.get("responsavel_nome") or "",
    )
    if mudou:
        _exigir_delegar(utilizador, request)
    payload = novo.model_dump()
    payload["responsavel_id"] = resp_id
    payload["responsavel_nome"] = resp_nome
    resultado = _executar(
        c.guardar, str(utilizador.empresa_id), payload, fornecedor_id, ator_de(utilizador, "fornecedores"),
        utilizador=utilizador,
    )
    registar_acao(
        db, acao=Acao.FORNECEDOR_ATUALIZADO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Fornecedor",
        entidade_id=uuid_ou_none(fornecedor_id),
        dados_novos={"id": fornecedor_id, "nome": resultado.get("nome")}, request=request,
    )
    if mudou:
        _registar_responsavel(db, utilizador, request, resultado, resp_id, resp_nome)
    db.commit()
    return resultado


@router.post("/{fornecedor_id}/avaliar", summary="Avaliar um fornecedor", dependencies=[OperarDep])
def avaliar(
    fornecedor_id: str,
    dados: AvaliacaoIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: FornecedorClient | None = FornecedorDep,
):
    c = cliente_ou_503(cli)
    ator = ator_de(utilizador, "fornecedores")
    resultado = _executar(
        c.avaliar, str(utilizador.empresa_id), fornecedor_id, dados.respostas, dados.nota,
        ator["id"], ator["nome"], ator,
        utilizador=utilizador,
    )
    registar_acao(
        db, acao=Acao.FORNECEDOR_AVALIADO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Fornecedor",
        entidade_id=uuid_ou_none(fornecedor_id),
        dados_novos={"id": fornecedor_id, "nome": resultado.get("nome"),
                     "classe": resultado.get("risco_classe"), "score": resultado.get("risco_score")},
        request=request,
    )
    db.commit()
    return resultado


@router.delete(
    "/{fornecedor_id}", status_code=204,
    summary="Eliminar um fornecedor", dependencies=[EliminarDep],
)
def eliminar(
    fornecedor_id: str,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: FornecedorClient | None = FornecedorDep,
):
    c = cliente_ou_503(cli)
    # O nome lê-se antes: depois de apagado, já não há onde o ir buscar.
    try:
        nome = _executar(c.obter, str(utilizador.empresa_id), fornecedor_id).get("nome") or None
    except Exception:  # noqa: BLE001 — o nome é um extra, nunca um requisito
        nome = None
    _executar(
        c.eliminar, str(utilizador.empresa_id), fornecedor_id,
        ator_de(utilizador, "fornecedores", ClasseAcao.ELIMINAR),
        utilizador=utilizador,
    )
    registar_acao(
        db, acao=Acao.FORNECEDOR_ELIMINADO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Fornecedor",
        entidade_id=uuid_ou_none(fornecedor_id),
        dados_novos={"id": fornecedor_id, "nome": nome}, request=request,
    )
    db.commit()
