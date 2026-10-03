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


from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.premium.client import (PremiumIndisponivelError,
                                e_indisponibilidade)
from app.premium.client import e_valor_fora_do_contrato
from app.premium.recusas import recusa_de_licenca
from app.premium.atores import ator_de, negar_capacidade, registar_recusa_de_recurso, resolver_pessoa
from app.premium.dependencies import require_feature
from app.premium.fornecedor_client import FornecedorClient, get_fornecedor_client
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

class FornecedorIn(BaseModel):
    ativo_id: str = ""
    nome: str
    servico: str = ""
    contacto: str = ""
    criticidade: str = ""
    estado: str = "ativo"
    due_diligence: bool = False
    due_diligence_nota: str = ""
    due_diligence_data: str = ""
    requisitos: dict[str, str] = {}
    pessoal_chave: str = ""
    termino_nota: str = ""
    acessos_revogados: bool = False
    dados_destino: str = ""
    encerrado_em: str = ""
    responsavel_id: str = ""
    responsavel_nome: str = ""


class AvaliacaoIn(BaseModel):
    respostas: dict[str, int] = {}
    nota: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cliente(cli: FornecedorClient | None) -> FornecedorClient:
    if cli is None:
        raise HTTPException(status_code=503, detail={"codigo": "premium_indisponivel"})
    return cli


def _locale(request: Request) -> str:
    lang = (request.headers.get("Accept-Language") or "").lower()
    return "en" if lang.startswith("en") else "pt-PT"


def _exigir_delegar(utilizador, request: Request | None = None) -> None:
    """Atribuir um fornecedor a outra pessoa é delegação — reservada à gestão."""
    if not tem_capacidade(utilizador, "fornecedores", ClasseAcao.DELEGAR):
        raise negar_capacidade(utilizador, "fornecedores", ClasseAcao.DELEGAR, request)


def _executar(fn, *args, utilizador=None):
    """Faz a chamada gRPC e traduz os erros em HTTP.

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
                if utilizador is not None:
                    registar_recusa_de_recurso(utilizador, "fornecedores")
                raise HTTPException(status_code=403, detail={"codigo": "sem_permissao_recurso"})
            if code == grpc.StatusCode.INVALID_ARGUMENT:
                raise HTTPException(
                    status_code=400, detail={"codigo": "fornecedor_invalido", "msg": exc.details()}
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
            raise HTTPException(status_code=502, detail={"codigo": "fornecedor_erro"})
        raise


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
    c = _cliente(cli)
    return _executar(
        c.listar, str(utilizador.empresa_id), estado, _locale(request), limite, offset
    )


@router.get("/painel", summary="Indicadores do módulo")
def painel(utilizador: CurrentUserDep, cli: FornecedorClient | None = FornecedorDep):
    c = _cliente(cli)
    return _executar(c.obter_painel, str(utilizador.empresa_id))


@router.get("/questionario", summary="Perguntas da avaliação de fornecedores")
def questionario(
    request: Request, utilizador: CurrentUserDep, cli: FornecedorClient | None = FornecedorDep
):
    c = _cliente(cli)
    return _executar(c.listar_questionario, str(utilizador.empresa_id), _locale(request))


@router.get("/documento", summary="Registo de fornecedores (payload localizado)")
def documento(
    request: Request, utilizador: CurrentUserDep, db: SessionDep,
    cli: FornecedorClient | None = FornecedorDep,
):
    from app.premium.anexar_evidencia import enriquecer_documento
    from app.shared.dependencies import get_empresa_ativa

    c = _cliente(cli)
    doc = _executar(
        c.gerar_documento, str(utilizador.empresa_id), "registo_fornecedores", _locale(request)
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
    c = _cliente(cli)
    return _executar(c.obter, str(utilizador.empresa_id), fornecedor_id)


@router.get("/{fornecedor_id}/avaliacoes", summary="Histórico de avaliações")
def avaliacoes(
    fornecedor_id: str, utilizador: CurrentUserDep, cli: FornecedorClient | None = FornecedorDep
):
    c = _cliente(cli)
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
    c = _cliente(cli)
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
        utilizador_id=utilizador.id, entidade_tipo="Fornecedor", entidade_id=None,
        dados_novos={"nome": resultado.get("nome"), "criticidade": resultado.get("criticidade")},
        request=request,
    )
    db.commit()
    return resultado


@router.patch("/{fornecedor_id}", summary="Atualizar um fornecedor", dependencies=[OperarDep])
def atualizar(
    fornecedor_id: str,
    dados: FornecedorIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: FornecedorClient | None = FornecedorDep,
):
    c = _cliente(cli)
    atual = _executar(c.obter, str(utilizador.empresa_id), fornecedor_id)
    resp_id, resp_nome, mudou = resolver_pessoa(
        db, utilizador, dados.responsavel_id, dados.responsavel_nome,
        atual_id=atual.get("responsavel_id") or "", atual_nome=atual.get("responsavel_nome") or "",
    )
    if mudou:
        _exigir_delegar(utilizador, request)
    payload = dados.model_dump()
    payload["responsavel_id"] = resp_id
    payload["responsavel_nome"] = resp_nome
    resultado = _executar(
        c.guardar, str(utilizador.empresa_id), payload, fornecedor_id, ator_de(utilizador, "fornecedores"),
        utilizador=utilizador,
    )
    registar_acao(
        db, acao=Acao.FORNECEDOR_ATUALIZADO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Fornecedor", entidade_id=None,
        dados_novos={"id": fornecedor_id}, request=request,
    )
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
    from app.shared.pii import decifrar_pii

    c = _cliente(cli)
    resultado = _executar(
        c.avaliar, str(utilizador.empresa_id), fornecedor_id, dados.respostas, dados.nota,
        str(utilizador.id), decifrar_pii(utilizador.nome) or "", ator_de(utilizador, "fornecedores"),
        utilizador=utilizador,
    )
    registar_acao(
        db, acao=Acao.FORNECEDOR_AVALIADO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Fornecedor", entidade_id=None,
        dados_novos={"id": fornecedor_id, "classe": resultado.get("risco_classe"),
                     "score": resultado.get("risco_score")},
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
    c = _cliente(cli)
    _executar(
        c.eliminar, str(utilizador.empresa_id), fornecedor_id,
        ator_de(utilizador, "fornecedores", ClasseAcao.ELIMINAR),
        utilizador=utilizador,
    )
    registar_acao(
        db, acao=Acao.FORNECEDOR_ELIMINADO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Fornecedor", entidade_id=None,
        dados_novos={"id": fornecedor_id}, request=request,
    )
    db.commit()
