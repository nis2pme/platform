"""
Schemas Pydantic do módulo de Formação.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

from app.formacao.models import EstadoFormacao, TipoFormacao


# ── Entrada ──────────────────────────────────────────────────────────────────

class AcaoCriarIn(BaseModel):
    titulo: str
    descricao: str = ""
    tipo: TipoFormacao = TipoFormacao.SENSIBILIZACAO
    formato: str = "presencial"
    data: date | None = None          # default = hoje (no service)
    duracao_horas: float | None = None
    formador: str = ""
    orgao_gestao: bool = False
    responsavel_id: str = ""


class AcaoAtualizarIn(BaseModel):
    titulo: str | None = None
    descricao: str | None = None
    tipo: TipoFormacao | None = None
    formato: str | None = None
    data: date | None = None
    duracao_horas: float | None = None
    formador: str | None = None
    orgao_gestao: bool | None = None
    eficacia_metodo: str | None = None
    eficacia_resultado: str | None = None
    responsavel_id: str | None = None


class EstadoIn(BaseModel):
    estado: EstadoFormacao


class ParticipanteIn(BaseModel):
    utilizador_id: str = ""     # interno (opcional)
    nome: str = ""              # externo (ou cache do interno)
    orgao_gestao: bool = False
    presente: bool = True


class ParticipantesLoteIn(BaseModel):
    """Um grupo inteiro num só pedido: uma sessão tem N pessoas, não N pedidos.

    Com teto: cada nome é cifrado e vira um objeto antes de tocar na base, e um
    único pedido sem limite podia trazer centenas de milhares deles."""

    participantes: list[ParticipanteIn] = Field(max_length=500)


class PresencaIn(BaseModel):
    """
    Quem esteve mesmo presente só se sabe depois da sessão. Sem isto, a presença
    ficava congelada no momento em que o participante era inscrito.
    """

    presente: bool


# ── Saída ────────────────────────────────────────────────────────────────────

class ParticipanteSchema(BaseModel):
    id: uuid.UUID
    utilizador_id: uuid.UUID | None = None
    nome: str
    orgao_gestao: bool
    presente: bool

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def decifrar_pii(cls, data):
        """Decifra o nome num dict — nunca na entidade ORM, que o commit regravaria em claro."""
        from app.shared.pii import decifrar_pii

        if not isinstance(data, dict):
            data = {k: getattr(data, k) for k in cls.model_fields if hasattr(data, k)}
        if data.get("nome"):
            data["nome"] = decifrar_pii(data["nome"]) or ""
        return data


class AcaoSchema(BaseModel):
    id: uuid.UUID
    titulo: str
    descricao: str
    tipo: TipoFormacao
    formato: str
    estado: EstadoFormacao
    data: date
    duracao_horas: float | None
    formador: str
    orgao_gestao: bool
    eficacia_metodo: str | None
    eficacia_resultado: str | None
    responsavel_id: uuid.UUID | None
    responsavel_nome: str | None = None
    realizada_at: date | None
    created_at: datetime
    updated_at: datetime
    # Derivado (calculado no service):
    total_participantes: int = 0

    model_config = {"from_attributes": True}


class AcaoDetalheSchema(AcaoSchema):
    participantes: list[ParticipanteSchema] = []


class ListaAcoesSchema(BaseModel):
    total: int
    acoes: list[AcaoSchema]


class LoteParticipantesSchema(BaseModel):
    """Resultado da adição em lote: quantos entraram e quantos já lá estavam."""

    adicionados: int
    ignorados: int
    participantes: list[ParticipanteSchema]


class PainelFormacaoSchema(BaseModel):
    total: int
    realizadas_ano: int          # ações realizadas nos últimos 12 meses
    planeadas: int
    participantes_ano: int       # participações PRESENTES nos últimos 12 meses
    orgao_gestao_ok: bool        # órgão de gestão formado nos últimos 12 meses (RJC, arts. 25.º e 27.º)
    # Datas da última formação do órgão de gestão: sem elas o indicador passa de
    # verde a vermelho de um dia para o outro, sem ninguém poder antecipar.
    orgao_gestao_ultima: date | None = None
    orgao_gestao_valido_ate: date | None = None
