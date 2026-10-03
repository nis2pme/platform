"""Schemas da política de capacidades."""
from __future__ import annotations

from pydantic import BaseModel, Field


class InterruptorSchema(BaseModel):
    """Um interruptor e o estado em que está nesta empresa.

    O texto visível não vem daqui: a chave é estável e o frontend traduz, como
    em todos os códigos que a API devolve.
    """

    chave: str
    # "ligado" | "desligado" | "personalizado"
    estado: str
    # Valor de origem da plataforma, para o ecrã poder assinalar o que foi mudado.
    defeito: bool


class PoliticaSchema(BaseModel):
    interruptores: list[InterruptorSchema]


class AlterarInterruptorSchema(BaseModel):
    ligar: bool = Field(description="True liga o interruptor, False desliga-o.")


# ── Edição célula a célula ──────────────────────────────────────────────────────


class CelulaSchema(BaseModel):
    """Um papel numa célula: o que vale, o que a plataforma trazia, e se se mexe."""

    papel: str
    # "total" | "atribuido" | "nenhum"
    valor: str
    origem: str
    editavel: bool
    # Porque é que está fechada, quando está. "Configura-se noutro sítio" e "não
    # se configura de todo" não são a mesma coisa, e um ecrã que lhes chamasse o
    # mesmo mandava o utilizador procurar uma opção que não existe.
    motivo: str | None = None


class ClasseSchema(BaseModel):
    classe: str
    # Valores que ESTA célula sabe honrar. Onde o âmbito não é consultado por
    # código nenhum, só há dois — oferecer o terceiro seria oferecer nada.
    ambitos: list[str]
    celulas: list[CelulaSchema]


class ModuloSchema(BaseModel):
    modulo: str
    # Se alguma célula do módulo se mexe aqui. Os que gerem a instalação vão
    # inteiros a falso — dois deles têm interruptor próprio, os outros nove não
    # se configuram de maneira nenhuma, e o `motivo` distingue os dois casos.
    editavel: bool
    motivo: str | None = None
    classes: list[ClasseSchema]


class MatrizSchema(BaseModel):
    """A matriz inteira, na ordem em que se mostra."""

    papeis: list[str]
    modulos: list[ModuloSchema]


class AlterarCelulaSchema(BaseModel):
    modulo: str = Field(max_length=40)
    classe: str = Field(max_length=20)
    papel: str = Field(max_length=20)
    valor: str = Field(max_length=20)


class AlterarCelulasSchema(BaseModel):
    """Um lote de alterações — os invariantes são propriedades da matriz inteira."""

    alteracoes: list[AlterarCelulaSchema] = Field(min_length=1, max_length=500)
