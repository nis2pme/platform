"""Schemas da pesquisa global."""
from pydantic import BaseModel, Field


class ResultadoPesquisaSchema(BaseModel):
    """Um resultado, já pronto a listar.

    `tipo` e `subtitulo` são CÓDIGOS (ex.: "incidente", "aberto") — quem os
    traduz é o frontend, que já tem os dicionários. `id` é string para servir
    tanto o core (UUID) como o sidecar.
    """

    tipo: str
    id: str
    titulo: str
    subtitulo: str = ""


class RespostaPesquisaSchema(BaseModel):
    resultados: list[ResultadoPesquisaSchema] = Field(default_factory=list)
    # True quando o sidecar existe mas não respondeu a tempo: os resultados do
    # core vêm na mesma (degradação silenciosa) e o UI pode avisar discretamente.
    premium_indisponivel: bool = False
