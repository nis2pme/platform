"""
Router da Importação de dados de ferramentas externas (premium).

**O core é uma cancela, não um processador.** Faz o que já sabe fazer bem —
limitar o tamanho, recusar o que claramente não é um ficheiro de texto, calcular
a impressão digital do conteúdo e registar a auditoria — e encaminha os bytes
para o sidecar por mTLS. **Não interpreta o ficheiro**: quem sabe ler um CSV do
GLPI ou um export do Monarc é o sidecar, e é lá que essa metodologia vive.

Três gates que se acumulam:
  - require_feature("data_import")        → o tenant tem o módulo? (402)
  - require_capability("importacao", …)   → consultar as fontes é leitura;
                                            submeter um ficheiro é escrita (403)
  - a cancela deste ficheiro              → o conteúdo é aceitável? (400/413)

Nota sobre validação de tipo: CSV e JSON **não têm assinatura binária**. Uma
lista de assinaturas "permitidas" não existe para eles, por isso o que se faz é
o inverso — recusar as assinaturas de formatos que não aceitamos (Excel, PDF,
comprimidos, executáveis) e exigir que o resto seja texto legível. Um ZIP nunca
chega ao analisador, e é isso que interessa.
"""
from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field

from app.config import get_settings
from app.premium.client import (PremiumIndisponivelError,
                                e_indisponibilidade)
from app.premium.client import e_valor_fora_do_contrato
from app.premium.recusas import recusa_de_licenca
from app.premium.atores import ator_de
from app.premium.dependencies import require_feature
from app.premium.importacao_client import ImportacaoClient, get_importacao_client
from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, SessionDep

router = APIRouter(
    prefix="/importacao",
    tags=["Importação"],
    dependencies=[
        Depends(require_capability("importacao", ClasseAcao.VER)),
        Depends(require_feature("data_import")),
    ],
)

_OperarImportacao = Depends(require_capability("importacao", ClasseAcao.OPERAR))

ImportacaoDep = Depends(get_importacao_client)

settings = get_settings()
_MAX_BYTES = settings.IMPORT_MAX_SIZE_MB * 1024 * 1024

# Assinaturas de formatos que NÃO aceitamos nesta fase. Recusar aqui, com nome,
# é o que permite ao wizard dizer "isto é um Excel — guarde como CSV" em vez de
# deixar o utilizador a olhar para um erro de leitura.
_ASSINATURAS_RECUSADAS: list[tuple[bytes, str]] = [
    (b"PK\x03\x04", "xlsx_ou_zip"),
    (b"%PDF", "pdf"),
    (b"\xd0\xcf\x11\xe0", "xls_antigo"),
    (b"\x7fELF", "binario"),
    (b"MZ", "binario"),
    (b"\x1f\x8b", "gzip"),
    (b"\x89PNG", "imagem"),
    (b"\xff\xd8\xff", "imagem"),
]


# O nome do 413 mudou nas versões recentes do Starlette; usa-se o atual quando
# existe para não semear avisos de depreciação no arranque.
_HTTP_413 = getattr(status, "HTTP_413_CONTENT_TOO_LARGE", 413)


# ── Corpos dos pedidos ───────────────────────────────────────────────────────
#
# O core valida a FORMA (é um mapeamento? cabe nos limites?) e não o SENTIDO
# (esta coluna corresponde a este campo?) — quem sabe o que as colunas querem
# dizer é o sidecar, e é lá que essa metodologia vive.

class ColunaMapeadaIn(BaseModel):
    # Os tetos são os do leitor do sidecar: um cabeçalho tem no máximo o tamanho
    # de uma célula, e os campos do catálogo têm poucas dezenas de caracteres.
    coluna: str = Field(default="", max_length=4000)
    campo: str = Field(default="", max_length=100)
    chave: bool = False


class MapeamentoIn(BaseModel):
    """O mapeamento que o ecrã devolve. Cada parte pesa em cada linha do ficheiro
    no sidecar: sem tetos, uma lista de colunas enorme prendia-o minutos (o
    sidecar confere também, e é ele que sabe que campos existem)."""

    colunas: list[ColunaMapeadaIn] = Field(default_factory=list, max_length=200)
    formato_data: str = Field(default="", max_length=40)
    separador_decimal: str = Field(default="", max_length=4)
    equivalencias: dict[
        Annotated[str, Field(max_length=4100)], Annotated[str, Field(max_length=100)]
    ] = Field(default_factory=dict, max_length=1000)
    vazio_apaga: bool = False


def _mapeamento(m: MapeamentoIn | None) -> dict | None:
    return m.model_dump() if m is not None else None


class SimulacaoIn(BaseModel):
    """Mapeamento revisto pelo utilizador. Ausente = usa-se o que foi sugerido."""

    mapeamento: MapeamentoIn | None = None


class DecisaoIn(BaseModel):
    """Decisão tomada no ecrã de reconciliação. Fica gravada: só se pergunta uma vez."""

    linha: int
    acao: str = Field(pattern="^(mesmo|novo|ignorar)$")
    alvo_id: str = ""


class AplicarIn(BaseModel):
    mapeamento: MapeamentoIn | None = None
    decisoes: list[DecisaoIn] = Field(default_factory=list, max_length=5000)
    # O carimbo da simulação que o utilizador viu. Se o universo mudou desde
    # então, o sidecar recusa e obriga a re-simular em vez de escrever um diff
    # que já não descreve a realidade.
    carimbo: str = ""
    # Um travão confirma-se um a um, com o nome à frente. Um botão "confirmar
    # tudo" seria o mesmo que não haver travão nenhum.
    travoes_confirmados: list[str] = Field(default_factory=list, max_length=20)


class PerfilIn(BaseModel):
    fonte: str
    nome: str = Field(min_length=1, max_length=80)
    mapeamento: MapeamentoIn


class DecisaoDescobertasIn(BaseModel):
    """Aceitar cria ativos; dispensar fecha a proposta. Nunca as duas coisas no
    mesmo pedido — misturar as duas tornaria o registo de auditoria ambíguo."""

    ids: list[str] = Field(min_length=1, max_length=200)
    acao: str = Field(pattern="^(aceitar|ignorar)$")


def _cliente(cli: ImportacaoClient | None) -> ImportacaoClient:
    if cli is None:
        raise HTTPException(status_code=503, detail={"codigo": "premium_indisponivel"})
    return cli


def _recusar(codigo: str, http: int = status.HTTP_400_BAD_REQUEST, **extra):
    raise HTTPException(status_code=http, detail={"codigo": codigo, **extra})


def _validar_conteudo(conteudo: bytes, content_type: str) -> None:
    """Cancela do core: tamanho, tipo declarado, assinatura e legibilidade."""
    if not conteudo:
        _recusar("ficheiro_vazio")
    if len(conteudo) > _MAX_BYTES:
        _recusar(
            "ficheiro_grande",
            http=_HTTP_413,
            limite_mb=settings.IMPORT_MAX_SIZE_MB,
        )
    if (content_type or "") not in settings.IMPORT_ALLOWED_MIME_TYPES:
        _recusar("tipo_nao_permitido", tipo=content_type)

    for assinatura, nome in _ASSINATURAS_RECUSADAS:
        if conteudo.startswith(assinatura):
            _recusar("formato_nao_suportado", detetado=nome)

    # Bytes nulos no início não aparecem em texto — exceto em UTF-16, que tem
    # marca própria e é legítimo. Sem isto, qualquer binário sem assinatura
    # conhecida passava a cancela.
    cabeca = conteudo[:512]
    if b"\x00" in cabeca and not conteudo.startswith((b"\xff\xfe", b"\xfe\xff")):
        _recusar("formato_nao_suportado", detetado="binario")


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
        # Antes escapava daqui e saía 500 em todas as rotas premium.
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
                # Falta o módulo de DESTINO (o inventário, o risco, os conetores)
                # e não a permissão de quem pede: quem escreve num módulo tem de
                # ter o módulo. É 402 como qualquer outra funcionalidade em falta,
                # e leva o nome do módulo para o ecrã poder propor o upgrade em
                # vez de mostrar um erro seco.
                modulo = _modulo_em_falta(exc.details())
                if modulo:
                    raise HTTPException(
                        status_code=402,
                        detail={"codigo": "modulo_em_falta", "modulo": modulo},
                    )
                raise HTTPException(status_code=403, detail={"codigo": "sem_permissao_recurso"})
            if code == grpc.StatusCode.INVALID_ARGUMENT:
                # Lote de decisões acima do teto. Vem antes do corpo estruturado
                # porque não é um problema do ficheiro: dizer "ficheiro inválido"
                # a quem escolheu máquinas de mais manda-o procurar no sítio errado.
                maximo = _lote_grande(exc.details())
                if maximo:
                    raise HTTPException(
                        status_code=400,
                        detail={"codigo": "lote_grande", "maximo": maximo},
                    )
                # O sidecar devolve um corpo estruturado (código + linha) para o
                # frontend poder dizer "linha 42: …" no idioma do utilizador.
                raise HTTPException(
                    status_code=400, detail=_detalhe_do_sidecar(exc.details())
                )
            if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
                raise HTTPException(
                    status_code=413,
                    detail={"codigo": "ficheiro_grande", "limite_mb": settings.IMPORT_MAX_SIZE_MB},
                )
            if code == grpc.StatusCode.FAILED_PRECONDITION:
                # Teto de registos do plano. É 402 como o módulo em falta — é a
                # mesma família de recusa (o plano não dá) e o ecrã propõe o
                # upgrade em vez de dizer que a plataforma avariou.
                numeros = _limite_do_plano(exc.details())
                if numeros:
                    total, limite = numeros
                    raise HTTPException(
                        status_code=402,
                        detail={"codigo": "limite_do_plano", "total": total, "limite": limite},
                    )
                # Travão por confirmar (ex.: T3, relatório mais antigo do que o
                # que já entrou). É uma recusa com resposta possível — o corpo
                # leva o código e os números para o ecrã perguntar. Sem isto
                # caía no 503 abaixo e o utilizador lia "premium indisponível"
                # sobre uma plataforma que está perfeitamente de pé.
                travao = _corpo_estruturado(exc.details())
                if travao:
                    raise HTTPException(status_code=409, detail=travao)
                raise HTTPException(status_code=503, detail={"codigo": "premium_indisponivel"})
            if code == grpc.StatusCode.UNIMPLEMENTED:
                raise HTTPException(status_code=501, detail={"codigo": "por_implementar"})
            if e_indisponibilidade(exc):
                # Sidecar em baixo ou pendurado. O 502 dizia «o upstream
                # respondeu mal»; aqui não respondeu de todo. Sem isto, a mesma
                # avaria saía 502 ou 503 conforme a cache de entitlements
                # estivesse quente — e um alerta não se constrói sobre isso.
                raise HTTPException(
                    status_code=503, detail={"codigo": "premium_indisponivel"}
                )
            raise HTTPException(status_code=502, detail={"codigo": "importacao_erro"})
        raise


# Prefixo estável da recusa do sidecar por falta do módulo de destino. Tem de
# acompanhar a constante do outro lado do contrato.
_PREFIXO_MODULO = "sem_direito_modulo:"

# Os módulos que a importação pode alcançar. Uma lista fechada porque o que sai
# daqui vai para o cliente: sem ela, o sidecar poderia induzir o core a repetir
# texto arbitrário numa resposta HTTP.
_MODULOS_CONHECIDOS = {
    "asset_inventory", "risk_analysis",
    # Um conetor por ferramenta de observação (e o do AD, cujo ficheiro
    # também chega por upload).
    "connector_m365", "connector_ad", "connector_gvm", "connector_wazuh",
}


def _modulo_em_falta(detalhes: str | None) -> str | None:
    """Nome do módulo em falta, ou `None` se a recusa foi por outra razão."""
    if not detalhes or not detalhes.startswith(_PREFIXO_MODULO):
        return None
    modulo = detalhes[len(_PREFIXO_MODULO):].strip()
    return modulo if modulo in _MODULOS_CONHECIDOS else None


# Prefixo estável da recusa do sidecar por teto de registos do plano.
_PREFIXO_LIMITE = "limite_do_plano:"

# Prefixo estável da recusa por lote de decisões acima do teto.
_PREFIXO_LOTE = "lote_grande:"


def _lote_grande(detalhes: str | None) -> int | None:
    """Teto de decisões por pedido, ou `None` se a recusa foi por outra razão."""
    if not detalhes or not detalhes.startswith(_PREFIXO_LOTE):
        return None
    try:
        maximo = int(detalhes[len(_PREFIXO_LOTE):])
    except ValueError:
        return None
    return maximo if maximo > 0 else None


def _limite_do_plano(detalhes: str | None) -> tuple[int, int] | None:
    """`(total, limite)` da recusa por teto do plano, ou `None`.

    Só números, e só dois: o que vem do sidecar é repetido ao cliente, por isso
    nada aqui aceita texto livre.
    """
    if not detalhes or not detalhes.startswith(_PREFIXO_LIMITE):
        return None
    partes = detalhes[len(_PREFIXO_LIMITE):].split(":")
    if len(partes) != 2:
        return None
    try:
        total, limite = int(partes[0]), int(partes[1])
    except ValueError:
        return None
    if total < 0 or limite < 0:
        return None
    return total, limite


def _corpo_estruturado(detalhes: str | None) -> dict | None:
    """Corpo de erro do sidecar, ou `None` se não vier no formato acordado.

    Devolver `None` — em vez de um código de omissão — é o que permite a quem
    chama distinguir "o sidecar disse-me qual é o problema" de "veio texto que
    não sei ler". Sem essa distinção, uma recusa com nome era indistinguível de
    uma avaria.
    """
    import json

    try:
        corpo = json.loads(detalhes or "")
        detalhe = corpo.get("detail")
        if isinstance(detalhe, dict) and detalhe.get("codigo"):
            return detalhe
    except (ValueError, AttributeError):
        pass
    return None


def _detalhe_do_sidecar(detalhes: str | None) -> dict:
    """O mesmo corpo, com um código genérico quando não há nada a ler. Nunca
    propaga texto cru do sidecar para o cliente."""
    return _corpo_estruturado(detalhes) or {"codigo": "ficheiro_invalido"}


# ── Leitura ──────────────────────────────────────────────────────────────────

@router.get("/fontes", summary="Ferramentas de onde se pode importar")
def fontes(
    utilizador: CurrentUserDep,
    locale: str = "",
    destino: str = "",
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = _cliente(cli)
    return _executar(c.listar_fontes, str(utilizador.empresa_id), locale, destino)


# ── Análise (não escreve nada) ───────────────────────────────────────────────

@router.post(
    "/analisar",
    summary="Analisar um ficheiro antes de importar",
    dependencies=[_OperarImportacao],
)
def analisar(
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    ficheiro: UploadFile = File(...),
    fonte: str = Form(...),
    destino: str = Form(""),
    # Âmbito por omissão: "este ficheiro é uma parte". Declarar que é a lista
    # completa é um ato consciente — é o único âmbito que pode concluir que algo
    # desapareceu da origem.
    ambito: str = Form("parcial"),
    locale: str = Form(""),
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = _cliente(cli)
    conteudo = ficheiro.file.read()
    _validar_conteudo(conteudo, ficheiro.content_type or "")
    sha256 = hashlib.sha256(conteudo).hexdigest()

    resultado = _executar(
        c.analisar_ficheiro,
        str(utilizador.empresa_id),
        conteudo,
        {
            "fonte": fonte,
            "destino": destino,
            "ambito": ambito,
            "nome_ficheiro": ficheiro.filename or "",
            "sha256": sha256,
            "locale": locale,
        },
        ator_de(utilizador, "importacao"),
    )

    # Auditoria com o que identifica o ato — nunca com conteúdo do ficheiro.
    registar_acao(
        db,
        acao=Acao.IMPORTACAO_ANALISADA,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Importacao",
        entidade_id=None,
        dados_novos={
            "importacao_id": resultado.get("importacao_id"),
            "fonte": fonte,
            "destino": destino,
            "ambito": ambito,
            "nome_ficheiro": ficheiro.filename or "",
            "sha256": sha256,
            "bytes": len(conteudo),
            "linhas": resultado.get("linhas_lidas"),
        },
        request=request,
    )
    db.commit()
    return resultado


# ── Observações técnicas (classe B) ──────────────────────────────────────────

@router.post(
    "/observacao",
    summary="Carregar um relatório de análise técnica",
    dependencies=[_OperarImportacao],
)
def observacao(
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    ficheiro: UploadFile = File(...),
    fonte: str = Form(...),
    locale: str = Form(""),
    # Travões que quem carrega já viu e confirmou, um a um (hoje só o T3, do
    # relatório mais antigo). Sem passo de simulação, a confirmação vem no mesmo
    # pedido — e por omissão não vem nenhuma, que é o comportamento seguro.
    travoes_confirmados: list[str] = Form(default=[]),
    cli: ImportacaoClient | None = ImportacaoDep,
):
    """Um relatório não cria ativos nem riscos: vira uma verificação.

    Não há passo de simulação porque não há nada a escrever no registo — o que
    entra é o retrato de um instante, e a observação seguinte substitui esta.
    Substituir um retrato recente por um antigo é a única forma de isto correr
    mal, e é o que o travão T3 põe à frente de uma pessoa antes de acontecer.
    """
    from app.premium.conetor_router import _declaracoes, _perfil_qnrcs
    from app.shared.dependencies import get_empresa_ativa

    c = _cliente(cli)
    conteudo = ficheiro.file.read()
    _validar_conteudo(conteudo, ficheiro.content_type or "")
    sha256 = hashlib.sha256(conteudo).hexdigest()

    empresa = get_empresa_ativa(db, utilizador)
    resultado = _executar(
        c.importar_observacao,
        str(utilizador.empresa_id),
        conteudo,
        {
            "fonte": fonte,
            "nome_ficheiro": ficheiro.filename or "",
            "sha256": sha256,
            "locale": locale,
        },
        _perfil_qnrcs(empresa),
        _declaracoes(db, empresa),
        ator_de(utilizador, "importacao"),
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
            "fonte": fonte,
            "destino": "observacao",
            "nome_ficheiro": ficheiro.filename or "",
            "sha256": sha256,
            "observado_em": resultado.get("observado_em"),
            "sinais_avaliados": resultado.get("sinais_avaliados"),
            "nao_conformes_minimo": resultado.get("nao_conformes_minimo"),
            "duplicado": resultado.get("duplicado"),
            # Quem carregou um relatório mais antigo do que o que já lá estava
            # tomou uma decisão — e uma decisão dessas tem de ficar registada.
            "travoes_confirmados": travoes_confirmados,
        },
        request=request,
    )
    db.commit()
    return resultado


# ── Simulação (também não escreve no destino) ────────────────────────────────

@router.post(
    "/{importacao_id}/simular",
    summary="Ver o que a importação vai fazer, antes de a fazer",
    dependencies=[_OperarImportacao],
)
def simular(
    request: Request,
    importacao_id: str,
    utilizador: CurrentUserDep,
    db: SessionDep,
    corpo: SimulacaoIn,
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = _cliente(cli)
    diff = _executar(
        c.simular,
        str(utilizador.empresa_id),
        importacao_id,
        _mapeamento(corpo.mapeamento),
        ator_de(utilizador, "importacao"),
    )
    registar_acao(
        db,
        acao=Acao.IMPORTACAO_SIMULADA,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Importacao",
        entidade_id=None,
        dados_novos={
            "importacao_id": importacao_id,
            "novos": diff.get("novos"),
            "atualizados": diff.get("atualizados"),
            "ignorados": diff.get("ignorados"),
            "ausentes": diff.get("ausentes"),
            "travoes": diff.get("travoes"),
            "pode_aplicar": diff.get("pode_aplicar"),
        },
        request=request,
    )
    db.commit()
    return diff


# ── Escrita ──────────────────────────────────────────────────────────────────

@router.post(
    "/{importacao_id}/aplicar",
    summary="Aplicar a importação simulada",
    dependencies=[_OperarImportacao],
)
def aplicar(
    request: Request,
    importacao_id: str,
    utilizador: CurrentUserDep,
    db: SessionDep,
    corpo: AplicarIn,
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = _cliente(cli)
    resumo = _executar(
        c.aplicar,
        str(utilizador.empresa_id),
        importacao_id,
        _mapeamento(corpo.mapeamento),
        [d.model_dump() for d in corpo.decisoes],
        corpo.carimbo,
        corpo.travoes_confirmados,
        ator_de(utilizador, "importacao"),
    )
    registar_acao(
        db,
        acao=Acao.IMPORTACAO_APLICADA,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Importacao",
        entidade_id=None,
        dados_novos={
            "importacao_id": importacao_id,
            "criados": resumo.get("criados"),
            "atualizados": resumo.get("atualizados"),
            "ignorados": resumo.get("ignorados"),
            "marcados_ausentes": resumo.get("marcados_ausentes"),
            "travoes_confirmados": corpo.travoes_confirmados,
        },
        request=request,
    )
    db.commit()
    return resumo


@router.post(
    "/{importacao_id}/reverter",
    summary="Desfazer uma importação aplicada",
    dependencies=[_OperarImportacao],
)
def reverter(
    request: Request,
    importacao_id: str,
    utilizador: CurrentUserDep,
    db: SessionDep,
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = _cliente(cli)
    resumo = _executar(
        c.reverter,
        str(utilizador.empresa_id),
        importacao_id,
        ator_de(utilizador, "importacao"),
    )
    registar_acao(
        db,
        acao=Acao.IMPORTACAO_REVERTIDA,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Importacao",
        entidade_id=None,
        dados_novos={
            "importacao_id": importacao_id,
            "removidos": resumo.get("criados"),
            "repostos": resumo.get("atualizados"),
            "saltados": resumo.get("saltados"),
        },
        request=request,
    )
    db.commit()
    return resumo


# ── Histórico ────────────────────────────────────────────────────────────────

@router.get("/historico", summary="Importações anteriores")
def historico(
    utilizador: CurrentUserDep,
    fonte: str = "",
    destino: str = "",
    limite: int = 20,
    offset: int = 0,
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = _cliente(cli)
    return _executar(
        c.listar_importacoes,
        str(utilizador.empresa_id),
        fonte,
        destino,
        max(1, min(limite, 200)),
        max(0, offset),
    )


# ── Descobertas (máquinas fora do inventário) ────────────────────────────────
#
# Declaradas ANTES de `/{importacao_id}`: a ordem de declaração é a ordem de
# correspondência, e `/descobertas` seria engolido pelo caminho genérico.

@router.get("/descobertas", summary="Máquinas vistas que não estão no inventário")
def descobertas(
    utilizador: CurrentUserDep,
    estado: str = "",
    limite: int = 100,
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = _cliente(cli)
    return _executar(
        c.listar_descobertas, str(utilizador.empresa_id), estado, max(1, min(limite, 500))
    )


@router.post(
    "/descobertas/decidir",
    summary="Adicionar ao inventário ou dispensar",
    dependencies=[_OperarImportacao],
)
def decidir_descobertas(
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    corpo: DecisaoDescobertasIn,
    cli: ImportacaoClient | None = ImportacaoDep,
):
    """Aceitar cria os ativos; dispensar fecha a proposta sem escrever nada."""
    c = _cliente(cli)
    resultado = _executar(
        c.decidir_descobertas,
        str(utilizador.empresa_id),
        corpo.ids,
        corpo.acao,
        ator_de(utilizador, "importacao"),
    )
    registar_acao(
        db,
        acao=Acao.IMPORTACAO_DESCOBERTA_DECIDIDA,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Importacao",
        entidade_id=None,
        # Os endereços NÃO entram no registo: são máquinas de terceiros na rede
        # do cliente, e o log de auditoria tem retenção própria mais longa do
        # que a delas. Fica o quê e quantos, que é o que se audita.
        dados_novos={"acao": corpo.acao, "quantos": len(corpo.ids)},
        request=request,
    )
    db.commit()
    return resultado


@router.get("/documento", summary="Evidência: de onde vieram os dados e quando")
def documento(
    request: Request,
    utilizador: CurrentUserDep,
    cli: ImportacaoClient | None = ImportacaoDep,
):
    """Payload do documento, localizado. Quem o transforma em PDF é o cliente —
    é o mesmo caminho dos restantes documentos-evidência."""
    c = _cliente(cli)
    locale = request.headers.get("accept-language", "")[:5]
    return _executar(c.documento, str(utilizador.empresa_id), locale)


@router.get("/qualidade", summary="Qualidade do inventário, em números")
def qualidade(
    utilizador: CurrentUserDep,
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = _cliente(cli)
    return _executar(c.qualidade, str(utilizador.empresa_id))


@router.get("/{importacao_id}", summary="Detalhe de uma importação")
def detalhe(
    importacao_id: str,
    utilizador: CurrentUserDep,
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = _cliente(cli)
    return _executar(c.detalhe, str(utilizador.empresa_id), importacao_id)


# ── Perfis de mapeamento do tenant ───────────────────────────────────────────

@router.get("/perfis/lista", summary="Mapeamentos guardados desta empresa")
def perfis(
    utilizador: CurrentUserDep,
    fonte: str = "",
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = _cliente(cli)
    return _executar(c.listar_perfis, str(utilizador.empresa_id), fonte)


@router.post(
    "/perfis",
    summary="Guardar o mapeamento para a próxima vez",
    dependencies=[_OperarImportacao],
)
def guardar_perfil(
    utilizador: CurrentUserDep,
    corpo: PerfilIn,
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = _cliente(cli)
    return _executar(
        c.guardar_perfil,
        str(utilizador.empresa_id),
        corpo.fonte,
        corpo.nome,
        _mapeamento(corpo.mapeamento),
        ator_de(utilizador, "importacao"),
    )
