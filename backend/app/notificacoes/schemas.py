"""
Schemas Pydantic para notificações.

A resposta não traz frases prontas: traz o código do que aconteceu e os valores
que entram nele. Quem compõe o texto é o frontend, no idioma de quem lê. As
linhas antigas — gravadas quando o texto vinha feito daqui — trazem `mensagem`
preenchido e `codigo` a nulo, e é assim que se distinguem.

Os valores que o catálogo declara como dados pessoais estão cifrados na base e
são decifrados aqui, no último passo antes de saírem.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.notificacoes.catalogo import definicao
from app.shared.pii import decifrar_pii


class NotificacaoSchema(BaseModel):
    id: uuid.UUID
    codigo: str | None = None
    categoria: str
    severidade: str
    params: dict[str, Any] = Field(default_factory=dict)
    # Texto congelado das linhas antigas; nulo em tudo o que é recente.
    titulo: str | None = None
    mensagem: str | None = None
    entidade_tipo: str | None = None
    entidade_id: uuid.UUID | None = None
    controlo_empresa_id: uuid.UUID | None = None
    acionavel: bool
    lida: bool
    lida_at: datetime | None = None
    created_at: datetime

    @classmethod
    def de_modelo(cls, n) -> "NotificacaoSchema":
        """
        Converte a entidade, desserializando os parâmetros.

        Um JSON ilegível (escrito por outra versão, ou corrompido) vale um
        dicionário vazio — o utilizador fica com a frase genérica do código em
        vez de a lista inteira falhar por causa de uma linha.

        O mesmo critério vale para os valores cifrados: um que não decifre entra
        vazio na frase. Mostrar o criptograma onde devia estar o nome de uma
        pessoa seria pior do que não mostrar nada.
        """
        params: dict[str, Any] = {}
        if n.params:
            try:
                lidos = json.loads(n.params)
                if isinstance(lidos, dict):
                    params = lidos
            except ValueError:
                pass
        for nome in definicao(n.codigo).params_pii:
            valor = params.get(nome)
            if isinstance(valor, str) and valor:
                params[nome] = decifrar_pii(valor) or ""
        return cls(
            id=n.id,
            codigo=n.codigo,
            categoria=n.categoria,
            severidade=n.severidade,
            params=params,
            titulo=n.titulo,
            mensagem=n.mensagem,
            entidade_tipo=n.entidade_tipo,
            entidade_id=n.entidade_id,
            controlo_empresa_id=n.controlo_empresa_id,
            acionavel=n.acionavel,
            lida=n.lida,
            lida_at=n.lida_at,
            created_at=n.created_at,
        )


class ListaNotificacoesSchema(BaseModel):
    total: int
    notificacoes: list[NotificacaoSchema] = Field(default_factory=list)


class ResumoNotificacoesSchema(BaseModel):
    # Total do conjunto FILTRADO — o mesmo que a listagem devolve com os mesmos
    # parâmetros.
    total: int
    # Número do sininho: por ler, independente dos filtros em ecrã.
    nao_lidas: int
    por_categoria: dict[str, int] = Field(default_factory=dict)
    por_severidade: dict[str, int] = Field(default_factory=dict)
    # Só preenchido a pedido: alimenta os marcadores na lista de controlos e não
    # vale a pena calculá-lo no pedido periódico que só quer a contagem.
    controlos_com_notificacoes: list[uuid.UUID] = Field(default_factory=list)


class CatalogoNotificacoesSchema(BaseModel):
    categorias: list[str]
    severidades: list[str]
    codigos: list["DefinicaoCodigoSchema"]


class DefinicaoCodigoSchema(BaseModel):
    codigo: str
    categoria: str
    severidade: str
    entidade_tipo: str | None = None
    acionavel: bool


class FiltrosMarcacaoSchema(BaseModel):
    """
    Filtros da marcação em lote — os mesmos da listagem, de propósito.

    Corpo vazio marca tudo o que está por ler. Com filtros, marca só o que
    corresponde: quem carrega no botão enquanto vê um separador está a dispensar
    aquilo que está a ver, não a esvaziar a caixa inteira sem dar por isso.
    """

    categoria: str | None = None
    severidade: str | None = None
    q: str | None = None
    data_inicio: datetime | None = None
    data_fim: datetime | None = None


class ResultadoNotificacoesMarcadasSchema(BaseModel):
    marcadas: int


CatalogoNotificacoesSchema.model_rebuild()
