"""
Router das LIGAÇÕES às fontes técnicas (premium) — superfície FINA, com dois
gates que se acumulam:
  - require_alguma_feature(...)        → o tenant tem alguma fonte? (402); cada
                                          fonte exige depois a sua, e é o sidecar
                                          que o decide (402 com o módulo)
  - require_capability("conetores", …) → as ligações guardam segredos: ver e
                                          configurar é administração (403)

O que as ligações provam (constatações, eventos, metas, factos, alertas) está no
router das verificações, com a sua própria capacidade.

Passthrough para o sidecar (a autoridade — valida, cifra as credenciais em
repouso, faz o tenant-scoping e é dono da premium-data-db). O core regista a
auditoria no core-db após sucesso.
Rotas `def`: a base e o gRPC são síncronos e correm no threadpool, fora do
event loop.

Nota de segurança: as credenciais entram na configuração de uma ligação e seguem
DIRETAS para o sidecar pelo canal mTLS. Nunca são guardadas no core, nunca
aparecem na auditoria (nem mascaradas) e nenhum endpoint as devolve.
"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.config import get_settings
from app.premium.atores import ator_de
from app.premium.client import PremiumIndisponivelError, e_indisponibilidade
from app.premium.client import e_valor_fora_do_contrato
from app.premium.recusas import recusa_de_licenca
from app.premium.conetor_client import FEATURES_CONETORES, ConetorClient, get_conetor_client
from app.premium.dependencies import require_alguma_feature, require_feature
from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, SessionDep

router = APIRouter(
    prefix="/conetores",
    tags=["Ligações às fontes técnicas"],
    dependencies=[
        Depends(require_capability("conetores", ClasseAcao.VER)),
        Depends(require_alguma_feature(*FEATURES_CONETORES)),
    ],
)

_OperarConetor = Depends(require_capability("conetores", ClasseAcao.OPERAR))
ConetorDep = Depends(get_conetor_client)

# Remover uma ligação apaga a credencial guardada no sidecar (o client secret do
# Microsoft 365, a password do AD) e os dados da fonte. Fica fora do portão do
# módulo e do só-leitura, como a purga: os dados são do cliente e têm de poder
# sair também depois de um downgrade ou com a licença sem validação. A pessoa
# continua a precisar da capacidade de operar os conetores.
router_remocao = APIRouter(
    prefix="/conetores",
    tags=["Ligações às fontes técnicas"],
    dependencies=[Depends(require_capability("conetores", ClasseAcao.VER))],
)

# Uma ligação, um conjunto de endpoints: o formulário de cada uma é diferente (o
# consentimento de uma app na cloud não é um servidor LDAPS com uma CA). Cada
# rota exige também o direito da sua ligação — o sidecar volta a verificá-lo.
_M365 = "entra"
_AD = "ad"
_M365_DIREITO = Depends(require_feature("connector_m365"))
_AD_DIREITO = Depends(require_feature("connector_ad"))


# ── Schemas de entrada ────────────────────────────────────────────────────────

class ConfigM365In(BaseModel):
    ms_tenant_id: str
    client_id: str
    # Vazia = manter a credencial já guardada (só válido em reconfiguração).
    credencial: str = ""
    credencial_tipo: str = "secret"
    intervalo_horas: int = 0
    ativo: bool = True


class AdDiretoIn(BaseModel):
    # Servidor: IP ou nome do controlador de domínio. O nome do certificado é o
    # que o TLS confirma (o servidor pode ser um IP quando o DNS do AD não
    # resolve a partir da plataforma).
    servidor: str = Field(..., min_length=1, max_length=253)
    porta: int = Field(636, ge=1, le=65535)
    nome_tls: str = Field(..., min_length=1, max_length=253)
    utilizador: str = Field(..., min_length=1, max_length=256)
    # Vazias = manter as guardadas. A senha só segue para o sidecar (mTLS), que a cifra.
    senha: str = Field("", max_length=1024)
    ca_pem: str = Field("", max_length=65536)
    intervalo_horas: int = Field(24, ge=6, le=168)
    ativo: bool = True


# ── Helpers (partilhados com o router das verificações e com a importação) ────

def _cliente(cli: ConetorClient | None) -> ConetorClient:
    if cli is None:
        raise HTTPException(status_code=503, detail={"codigo": "premium_indisponivel"})
    return cli


def _perfil_qnrcs(empresa) -> str:
    """Nível QNRCS efetivo: o escolhido pela empresa, senão o derivado do tipo
    de entidade (mesma regra do scoring dos controlos)."""
    v = empresa.nivel_qnrcs or empresa.tipo_entidade
    v = getattr(v, "value", v) or ""
    if v in ("basico", "substancial", "elevado"):
        return v
    return {"base": "basico", "importante": "substancial", "essencial": "elevado"}.get(v, "basico")


def _declaracoes(db, empresa) -> dict[str, str]:
    """Código do controlo → estado declarado, para a régua por nível e a
    deteção de contradições (declarado implementado vs. observado) no sidecar."""
    from app.frameworks.runtime import load_company_control_rows, resolver_framework_empresa

    framework = resolver_framework_empresa(db, empresa)
    rows = load_company_control_rows(db, empresa.id, framework.id)
    declaracoes: dict[str, str] = {}
    for row in rows:
        estado = getattr(row.ce, "estado", None)
        declaracoes[row.control.code] = getattr(estado, "value", "") or ""
    return declaracoes


def _executar(fn, *args, **kwargs):
    """Faz a chamada gRPC e traduz os erros em HTTP."""
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except PremiumIndisponivelError:
        # O sidecar não está utilizável: canal por montar, material de TLS em
        # falta, transporte ausente. É indisponibilidade do módulo, não avaria da
        # plataforma — e a diferença é a que o cliente precisa de ver para saber
        # se age (renovar a licença, verificar a rede) ou se reporta um defeito.
        raise HTTPException(status_code=503, detail={"codigo": "premium_indisponivel"})
    except Exception as exc:  # noqa: BLE001 — traduzido abaixo
        if e_valor_fora_do_contrato(exc):
            raise HTTPException(status_code=400, detail={"codigo": "valor_fora_de_intervalo"}) from exc
        recusa = recusa_de_licenca(exc, incluir_modulo=False)
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
                # Falta o módulo desta fonte (o sidecar decide por fonte): é o
                # plano que não dá, não a pessoa que não pode — 402 com o nome.
                from app.premium.importacao_router import _modulo_em_falta

                modulo = _modulo_em_falta(exc.details())
                if modulo:
                    raise HTTPException(status_code=402, detail={"codigo": "modulo_em_falta", "modulo": modulo})
                raise HTTPException(status_code=403, detail={"codigo": "sem_permissao_recurso"})
            if code == grpc.StatusCode.INVALID_ARGUMENT:
                raise HTTPException(status_code=400, detail={"codigo": "conetor_invalido", "msg": exc.details()})
            if code == grpc.StatusCode.FAILED_PRECONDITION:
                raise _precondicao(exc.details())
            if code == grpc.StatusCode.UNIMPLEMENTED:
                raise HTTPException(status_code=501, detail={"codigo": "por_implementar"})
            if e_indisponibilidade(exc):
                # Sidecar em baixo ou pendurado: não respondeu de todo.
                raise HTTPException(status_code=503, detail={"codigo": "premium_indisponivel"})
            raise HTTPException(status_code=502, detail={"codigo": "conetor_erro"})
        raise


# Recusas de pré-condição do sidecar → código estável que o ecrã traduz. O
# texto do sidecar é técnico e só em português; passado tal e qual, era o que
# aparecia no aviso, também a quem usa a aplicação em inglês.
_PRECONDICOES = (
    # A instalação não tem a chave que cifra as credenciais: nada se guarda, e
    # não é quem pediu que o resolve — é quem administra o servidor.
    ("CONNECTOR_SECRETS_KEY", 503, "cifra_por_configurar"),
    ("ligacao_em_falta", 409, "ligacao_em_falta"),
    ("so_onprem", 403, "so_onprem"),
    ("desativado", 409, "conetor_desativado"),
    ("não configurado", 409, "conetor_nao_configurado"),
)


def _precondicao(detalhe: str) -> HTTPException:
    for trecho, estado, codigo in _PRECONDICOES:
        if trecho in (detalhe or ""):
            return HTTPException(status_code=estado, detail={"codigo": codigo})
    # Estado do tenant que impede a ação: corrigível por quem pediu, e por isso
    # 409 — um 5xx diria que a plataforma avariou e mandava a pessoa esperar.
    return HTTPException(status_code=409, detail={"codigo": "estado_invalido", "msg": detalhe})


def _so_onprem() -> None:
    """Em SaaS o sidecar não está na rede da empresa, e abrir o AD à internet
    para o alcançar não é opção: aí o AD entra pelo ficheiro do coletor."""
    if get_settings().DEPLOYMENT_MODE != "onprem":
        raise HTTPException(status_code=403, detail={"codigo": "so_onprem"})


# ── Comum às ligações ────────────────────────────────────────────────────────

def _estado(fonte: str, utilizador, cli):
    return _executar(_cliente(cli).estado, str(utilizador.empresa_id), fonte)


def _testar(fonte: str, utilizador, cli):
    return _executar(_cliente(cli).testar, str(utilizador.empresa_id), fonte)


def _verificar(fonte: str, request: Request, utilizador, db, cli):
    from app.shared.dependencies import get_empresa_ativa

    c = _cliente(cli)
    # Contexto do core: o sidecar avalia com o nível do perfil e cruza a
    # observação técnica com o que está declarado na plataforma (contradições).
    empresa = get_empresa_ativa(db, utilizador)
    resultado = _executar(
        c.executar_verificacao,
        str(utilizador.empresa_id),
        fonte,
        _perfil_qnrcs(empresa),
        _declaracoes(db, empresa),
        ator_de(utilizador, "conetores"),
    )
    registar_acao(
        db, acao=Acao.CONETOR_VERIFICACAO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Conetor", entidade_id=None,
        dados_novos={
            "tipo": fonte,
            "origem": "manual",
            "resultado": resultado.get("resultado"),
            "erro_categoria": resultado.get("erro_categoria"),
            "sinais_avaliados": resultado.get("sinais_avaliados"),
            "nao_conformes_politica": resultado.get("nao_conformes_politica"),
            "nao_conformes_minimo": resultado.get("nao_conformes_minimo"),
            "indeterminados": resultado.get("indeterminados"),
        },
        request=request,
    )
    db.commit()
    return resultado


def _remover(fonte: str, request: Request, utilizador, db, cli):
    resultado = _executar(
        _cliente(cli).remover, str(utilizador.empresa_id), fonte, ator_de(utilizador, "conetores")
    )
    registar_acao(
        db, acao=Acao.CONETOR_REMOVIDO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Conetor", entidade_id=None,
        dados_novos={"tipo": fonte}, request=request,
    )
    db.commit()
    return resultado


# ── Microsoft 365 (Entra ID) ─────────────────────────────────────────────────

@router.get("/entra/estado", summary="Estado da ligação ao Microsoft 365 (sem credencial)", dependencies=[_M365_DIREITO])
def estado_m365(utilizador: CurrentUserDep, cli: ConetorClient | None = ConetorDep):
    return _estado(_M365, utilizador, cli)


@router.put("/entra", summary="Configurar a ligação ao Microsoft 365", dependencies=[_OperarConetor, _M365_DIREITO])
def configurar_m365(
    dados: ConfigM365In,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: ConetorClient | None = ConetorDep,
):
    resultado = _executar(
        _cliente(cli).configurar, str(utilizador.empresa_id), _M365, dados.model_dump(),
        ator_de(utilizador, "conetores"),
    )
    # Auditoria SEM a credencial — regista-se apenas que foi substituída.
    registar_acao(
        db, acao=Acao.CONETOR_CONFIGURADO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Conetor", entidade_id=None,
        dados_novos={
            "tipo": _M365,
            "ms_tenant_id": dados.ms_tenant_id,
            "intervalo_horas": resultado.get("intervalo_horas"),
            "ativo": dados.ativo,
            "credencial_substituida": bool(dados.credencial),
        },
        request=request,
    )
    db.commit()
    return resultado


@router.post("/entra/testar", summary="Testar a ligação ao Microsoft 365", dependencies=[_OperarConetor, _M365_DIREITO])
def testar_m365(utilizador: CurrentUserDep, cli: ConetorClient | None = ConetorDep):
    return _testar(_M365, utilizador, cli)


@router.post("/entra/verificar", summary="Ler o Microsoft 365 agora", dependencies=[_OperarConetor, _M365_DIREITO])
def verificar_m365(
    request: Request, utilizador: CurrentUserDep, db: SessionDep, cli: ConetorClient | None = ConetorDep
):
    return _verificar(_M365, request, utilizador, db, cli)


@router_remocao.delete(
    "/entra", summary="Remover a ligação ao Microsoft 365 e os dados dela", dependencies=[_OperarConetor]
)
def remover_m365(request: Request, utilizador: CurrentUserDep, db: SessionDep, cli: ConetorClient | None = ConetorDep):
    return _remover(_M365, request, utilizador, db, cli)


# ── Active Directory: comum aos dois modos ───────────────────────────────────

@router.get("/ad/estado", summary="Estado do Active Directory (ficheiro ou ligação direta)", dependencies=[_AD_DIREITO])
def estado_ad(utilizador: CurrentUserDep, cli: ConetorClient | None = ConetorDep):
    return _estado(_AD, utilizador, cli)


@router_remocao.delete(
    "/ad", summary="Remover os dados do Active Directory (e o cruzamento com a cloud)",
    dependencies=[_OperarConetor],
)
def remover_ad(request: Request, utilizador: CurrentUserDep, db: SessionDep, cli: ConetorClient | None = ConetorDep):
    return _remover(_AD, request, utilizador, db, cli)


# ── Active Directory: ligação direta (LDAPS, só on-prem) ──────────────────────

@router.put(
    "/ad/direto",
    summary="Configurar a ligação direta ao Active Directory (LDAPS, só on-prem)",
    dependencies=[_OperarConetor, _AD_DIREITO],
)
def configurar_ad_direto(
    dados: AdDiretoIn,
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: ConetorClient | None = ConetorDep,
):
    _so_onprem()
    parametros = {
        "servidor": dados.servidor.strip(),
        "porta": str(dados.porta),
        "nome_tls": dados.nome_tls.strip(),
        "utilizador": dados.utilizador.strip(),
    }
    resultado = _executar(
        _cliente(cli).configurar_ligacao_direta,
        str(utilizador.empresa_id),
        _AD,
        parametros,
        dados.ca_pem,
        dados.senha,
        dados.intervalo_horas,
        dados.ativo,
        ator_de(utilizador, "conetores"),
    )
    # Auditoria SEM a senha — só que foi (ou não) substituída.
    registar_acao(
        db, acao=Acao.CONETOR_CONFIGURADO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Conetor", entidade_id=None,
        dados_novos={
            "tipo": _AD,
            "modo": "direto",
            **parametros,
            "ativo": dados.ativo,
            "senha_substituida": bool(dados.senha),
            "ca_substituida": bool(dados.ca_pem.strip()),
        },
        request=request,
    )
    db.commit()
    return resultado


@router.delete(
    "/ad/direto",
    summary="Desligar a ligação direta ao AD (volta ao ficheiro do coletor)",
    dependencies=[_OperarConetor, _AD_DIREITO],
)
def desligar_ad_direto(
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: ConetorClient | None = ConetorDep,
):
    # Em SaaS não há ligação direta a desligar: sem isto ficava na trilha um
    # "desligada" de uma ligação que nunca existiu.
    _so_onprem()
    resultado = _executar(
        _cliente(cli).voltar_ao_ficheiro, str(utilizador.empresa_id), _AD, ator_de(utilizador, "conetores")
    )
    registar_acao(
        db, acao=Acao.CONETOR_CONFIGURADO, empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id, entidade_tipo="Conetor", entidade_id=None,
        dados_novos={"tipo": _AD, "modo": "ficheiro", "ligacao_direta_desligada": True},
        request=request,
    )
    db.commit()
    return resultado


@router.post("/ad/testar", summary="Testar a ligação direta ao Active Directory", dependencies=[_OperarConetor, _AD_DIREITO])
def testar_ad(utilizador: CurrentUserDep, cli: ConetorClient | None = ConetorDep):
    _so_onprem()
    return _testar(_AD, utilizador, cli)


@router.post(
    "/ad/verificar", summary="Ler o Active Directory agora pela ligação direta", dependencies=[_OperarConetor, _AD_DIREITO]
)
def verificar_ad(
    request: Request, utilizador: CurrentUserDep, db: SessionDep, cli: ConetorClient | None = ConetorDep
):
    _so_onprem()
    return _verificar(_AD, request, utilizador, db, cli)


# ── Active Directory: ficheiro do coletor ────────────────────────────────────
#
# No ecrã vive nas Importações (é um ficheiro que uma pessoa carrega). Fica neste
# router porque o direito é o do conetor AD e não o do módulo de importação: uma
# empresa com o AD e sem importação de registos carrega o coletor na mesma.

@router.get(
    "/ad/coletor", summary="Descarregar o coletor do Active Directory (script só de leitura)", dependencies=[_AD_DIREITO]
)
def coletor_ad(utilizador: CurrentUserDep, cli: ConetorClient | None = ConetorDep):
    """O script vem do sidecar (a versão que o leitor dele entende) com o
    SHA-256, para o cliente confirmar o que vai correr."""
    r = _executar(_cliente(cli).obter_coletor, str(utilizador.empresa_id), _AD)
    return {
        "nome_ficheiro": r["nome_ficheiro"],
        "sha256": r["sha256"],
        "versao": r["versao"],
        # ASCII por construção (o PowerShell 5.1 lê scripts sem BOM como ANSI).
        "script": r["script"].decode("ascii"),
    }


@router.post(
    "/ad/coletor",
    summary="Carregar o ficheiro do coletor do Active Directory",
    dependencies=[_OperarConetor, _AD_DIREITO],
)
def carregar_coletor_ad(
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    ficheiro: UploadFile = File(...),
    locale: str = Form(""),
    # Travões já vistos e confirmados (T3: ficheiro mais antigo; T5: outro
    # domínio). Por omissão nenhum — o comportamento seguro.
    travoes_confirmados: list[str] = Form(default=[]),
):
    """O ficheiro vira uma verificação da fonte AD: sinais em régua dupla,
    desvios, contradições e computadores por conhecer propostos ao inventário.
    Passa pela mesma cancela das importações (tamanho, tipo, assinatura)."""
    from app.premium.importacao_client import get_importacao_client
    from app.premium.importacao_router import _cliente as _cliente_importacao
    from app.premium.importacao_router import _executar as _executar_importacao
    from app.premium.importacao_router import _validar_conteudo
    from app.shared.dependencies import get_empresa_ativa

    c = _cliente_importacao(get_importacao_client())
    conteudo = ficheiro.file.read()
    _validar_conteudo(conteudo, ficheiro.content_type or "")
    sha256 = hashlib.sha256(conteudo).hexdigest()
    empresa = get_empresa_ativa(db, utilizador)
    resultado = _executar_importacao(
        c.importar_coletor_ad,
        str(utilizador.empresa_id),
        conteudo,
        {"nome_ficheiro": ficheiro.filename or "", "sha256": sha256, "locale": locale},
        _perfil_qnrcs(empresa),
        _declaracoes(db, empresa),
        ator_de(utilizador, "conetores"),
        travoes_confirmados,
    )
    registar_acao(
        db,
        acao=Acao.IMPORTACAO_APLICADA,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Importacao",
        entidade_id=None,
        dados_novos={
            "importacao_id": resultado.get("importacao_id"),
            "fonte": _AD,
            "destino": "observacao",
            "nome_ficheiro": ficheiro.filename or "",
            "sha256": sha256,
            "observado_em": resultado.get("observado_em"),
            "sinais_avaliados": resultado.get("sinais_avaliados"),
            "nao_conformes_minimo": resultado.get("nao_conformes_minimo"),
            "duplicado": resultado.get("duplicado"),
            "travoes_confirmados": travoes_confirmados,
        },
        request=request,
    )
    db.commit()
    # O SHA-256 volta para o ecrã: o cliente compara-o com o que o coletor
    # escreveu no fim.
    return {**resultado, "sha256": sha256}
