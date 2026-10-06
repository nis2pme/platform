"""
Router da Importação de dados de ferramentas externas (premium).

**O core é uma cancela, não um processador.** Faz o que já sabe fazer bem —
limitar o tamanho, recusar o que claramente não é um ficheiro de texto, calcular
a impressão digital do conteúdo e registar a auditoria — e encaminha os bytes
para o sidecar por mTLS. **Não interpreta o ficheiro**: quem sabe ler um CSV do
GLPI ou um export do Monarc é o sidecar.

Três gates que se acumulam:
  - require_feature("data_import")        → o tenant tem o módulo? (402)
  - require_capability("importacao", …)   → consultar as fontes é leitura;
                                            submeter um ficheiro é escrita (403)
  - a cancela (`cancela.py`)              → o conteúdo é aceitável? (400/413)
"""
from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel, Field

from app.frameworks.runtime import nivel_qnrcs_efetivo
from app.premium import contexto_nucleo
from app.premium.atores import ator_de
from app.premium.cancela import validar_conteudo
from app.premium.dependencies import require_feature
from app.premium.erros_importacao import executar_importacao
from app.premium.importacao_client import ImportacaoClient, get_importacao_client
from app.premium.pedido import cliente_ou_503, locale_do_pedido
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

# ── Corpos dos pedidos ───────────────────────────────────────────────────────
#
# O core valida a FORMA (é um mapeamento? cabe nos limites?) e não o SENTIDO
# (esta coluna corresponde a este campo?) — quem sabe o que as colunas querem
# dizer é o sidecar.

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


# ── Leitura ──────────────────────────────────────────────────────────────────

@router.get("/fontes", summary="Ferramentas de onde se pode importar")
def fontes(
    utilizador: CurrentUserDep,
    locale: str = "",
    destino: str = "",
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = cliente_ou_503(cli)
    return executar_importacao(c.listar_fontes, str(utilizador.empresa_id), locale, destino)


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
    c = cliente_ou_503(cli)
    conteudo = ficheiro.file.read()
    validar_conteudo(conteudo, ficheiro.content_type or "")
    sha256 = hashlib.sha256(conteudo).hexdigest()

    resultado = executar_importacao(
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
    from app.shared.dependencies import get_empresa_ativa

    c = cliente_ou_503(cli)
    conteudo = ficheiro.file.read()
    validar_conteudo(conteudo, ficheiro.content_type or "")
    sha256 = hashlib.sha256(conteudo).hexdigest()

    empresa = get_empresa_ativa(db, utilizador)
    resultado = executar_importacao(
        c.importar_observacao,
        str(utilizador.empresa_id),
        conteudo,
        {
            "fonte": fonte,
            "nome_ficheiro": ficheiro.filename or "",
            "sha256": sha256,
            "locale": locale,
        },
        nivel_qnrcs_efetivo(empresa),
        contexto_nucleo.declaracoes_dos_controlos(db, empresa),
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
    c = cliente_ou_503(cli)
    diff = executar_importacao(
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
    c = cliente_ou_503(cli)
    resumo = executar_importacao(
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
    c = cliente_ou_503(cli)
    resumo = executar_importacao(
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
    c = cliente_ou_503(cli)
    return executar_importacao(
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
    c = cliente_ou_503(cli)
    return executar_importacao(
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
    c = cliente_ou_503(cli)
    resultado = executar_importacao(
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
    c = cliente_ou_503(cli)
    return executar_importacao(c.documento, str(utilizador.empresa_id), locale_do_pedido(request))


@router.get("/qualidade", summary="Qualidade do inventário, em números")
def qualidade(
    utilizador: CurrentUserDep,
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = cliente_ou_503(cli)
    return executar_importacao(c.qualidade, str(utilizador.empresa_id))


@router.get("/{importacao_id}", summary="Detalhe de uma importação")
def detalhe(
    importacao_id: str,
    utilizador: CurrentUserDep,
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = cliente_ou_503(cli)
    return executar_importacao(c.detalhe, str(utilizador.empresa_id), importacao_id)


# ── Perfis de mapeamento do tenant ───────────────────────────────────────────

@router.get("/perfis/lista", summary="Mapeamentos guardados desta empresa")
def perfis(
    utilizador: CurrentUserDep,
    fonte: str = "",
    cli: ImportacaoClient | None = ImportacaoDep,
):
    c = cliente_ou_503(cli)
    return executar_importacao(c.listar_perfis, str(utilizador.empresa_id), fonte)


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
    c = cliente_ou_503(cli)
    return executar_importacao(
        c.guardar_perfil,
        str(utilizador.empresa_id),
        corpo.fonte,
        corpo.nome,
        _mapeamento(corpo.mapeamento),
        ator_de(utilizador, "importacao"),
    )
