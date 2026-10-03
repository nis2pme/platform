"""
Schemas Pydantic do módulo de Incidentes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator

from app.shared.validacao import SemNulos

from app.incidentes.models import (
    EstadoIncidente,
    SeveridadeIncidente,
    TipoEventoIncidente,
)


# ── Entrada ──────────────────────────────────────────────────────────────────

class _CamposRJC(BaseModel):
    """Os factos de que os prazos dependem e os campos que as notificações levam.

    As datas dos marcos cumpridos não entram aqui: só se preenchem ao registar
    a notificação enviada (`POST /{id}/notificacoes`)."""

    significativo_em: datetime | None = None
    impacto_inicio_em: datetime | None = None
    fim_impacto_em: datetime | None = None
    resolvido_2h: bool | None = None
    atualizacao_necessaria: bool | None = None
    excecao_24h: str | None = Field(None, max_length=4000)
    intercalar_pedido_em: datetime | None = None
    cnpd_aplicavel: bool | None = None
    representante_nome: str | None = Field(None, max_length=255)
    representante_telefone: str | None = Field(None, max_length=60)
    representante_email: str | None = Field(None, max_length=255)
    utilizadores_afetados: int | None = Field(None, ge=0)
    utilizadores_total: int | None = Field(None, ge=0)
    zona_geografica: str | None = Field(None, max_length=255)
    transfronteirico: bool | None = None
    paises_afetados: str | None = Field(None, max_length=255)
    causa: str | None = Field(None, max_length=20000)
    efeitos: str | None = Field(None, max_length=20000)
    medidas: str | None = Field(None, max_length=20000)
    situacao_residual: str | None = Field(None, max_length=20000)
    tempo_recuperacao: str | None = Field(None, max_length=120)


class IncidenteCriarIn(_CamposRJC):
    titulo: str = Field(max_length=255)
    descricao: str = ""
    categoria: str = Field("outro.indeterminado", max_length=80)
    severidade: SeveridadeIncidente = SeveridadeIncidente.MEDIA
    significativo: bool | None = None
    responsavel_id: str = ""
    conhecido_at: datetime | None = None   # default = agora (no service)
    ocorrido_at: datetime | None = None

    @field_validator("conhecido_at")
    @classmethod
    def _conhecido_nao_futuro(cls, v: datetime | None) -> datetime | None:
        # Os prazos legais contam a partir daqui: no futuro, empurravam-nos
        # para a frente. Folga de minutos para relógios desacertados.
        if v is not None:
            instante = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
            if instante > datetime.now(timezone.utc) + timedelta(minutes=5):
                raise ValueError("a data em que o incidente foi conhecido não pode estar no futuro")
        return v


class IncidenteAtualizarIn(_CamposRJC, SemNulos):
    nao_anulaveis = frozenset({
        "titulo", "descricao", "categoria", "severidade",
        "resolvido_2h", "atualizacao_necessaria", "cnpd_aplicavel",
    })

    titulo: str | None = Field(None, max_length=255)
    descricao: str | None = None
    categoria: str | None = Field(None, max_length=80)
    severidade: SeveridadeIncidente | None = None
    significativo: bool | None = None
    responsavel_id: str | None = None
    ocorrido_at: datetime | None = None
    licoes_aprendidas: str | None = None
    criterios_fecho: str | None = None


class EstadoIn(BaseModel):
    estado: EstadoIncidente
    nota: str = ""


class NotificacaoIn(BaseModel):
    """Registo de uma notificação enviada à autoridade (ou à CNPD)."""
    tipo: str = Field(max_length=40)   # um dos MARCOS
    canal: str = Field(max_length=40)  # myciber | email | telefone | outro
    referencia: str | None = Field(None, max_length=255)
    enviada_em: datetime | None = None   # omissão = agora
    nota: str = Field("", max_length=4000)


class EventoIn(BaseModel):
    tipo: TipoEventoIncidente
    texto: str
    parte: str | None = None   # só para comunicações

    @field_validator("tipo")
    @classmethod
    def _tipo_da_cronologia(cls, v: TipoEventoIncidente) -> TipoEventoIncidente:
        # `estado` e `marco` são escritos pelo sistema (mudar o estado, registar
        # uma notificação enviada); vindos da cronologia, mostravam no relatório
        # marcos que os campos do incidente não confirmam.
        if v in (TipoEventoIncidente.ESTADO, TipoEventoIncidente.MARCO):
            raise ValueError("tipo de evento reservado ao sistema")
        return v


# ── Saída ────────────────────────────────────────────────────────────────────

class PrazoSchema(BaseModel):
    """Estado de um marco legal (os campos de `prazos.Prazo`)."""
    marco: str
    prazo: datetime | None = None     # quando expira (UTC); None = ainda sem base
    base: str                         # campo de onde conta
    unidade: str                      # "horas" | "dias_uteis"
    quantidade: int
    obrigatorio: bool                 # conta para avisos, atraso e auditor
    voluntario: bool
    provisorio: bool
    dispensado: bool
    motivo: str | None = None
    cumprido: bool
    cumprido_at: datetime | None = None
    em_atraso: bool
    horas_restantes: float | None = None  # até ao prazo (negativo = passou)


class EventoSchema(BaseModel):
    id: uuid.UUID
    tipo: TipoEventoIncidente
    texto: str
    parte: str | None = None
    autor_id: uuid.UUID | None = None
    autor_nome: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificacaoResumoSchema(BaseModel):
    id: uuid.UUID
    tipo: str
    enviada_em: datetime
    canal: str
    referencia: str | None = None
    hash_conteudo: str
    autor_nome: str | None = None
    created_at: datetime


class NotificacaoDetalheSchema(NotificacaoResumoSchema):
    conteudo: dict | None = None


class IncidenteSchema(BaseModel):
    id: uuid.UUID
    titulo: str
    descricao: str
    categoria: str
    severidade: SeveridadeIncidente
    estado: EstadoIncidente
    significativo: bool | None
    significativo_em: datetime | None = None
    responsavel_id: uuid.UUID | None
    responsavel_nome: str | None = None
    conhecido_at: datetime
    ocorrido_at: datetime | None
    impacto_inicio_em: datetime | None = None
    fim_impacto_em: datetime | None = None
    resolvido_2h: bool = False
    atualizacao_necessaria: bool = False
    excecao_24h: str | None = None
    intercalar_pedido_em: datetime | None = None
    cnpd_aplicavel: bool = False
    # Marcos cumpridos.
    notificacao_inicial_at: datetime | None = None
    atualizacao_at: datetime | None = None
    fim_impacto_notificado_at: datetime | None = None
    relatorio_final_at: datetime | None = None
    cnpd_notificado_at: datetime | None = None
    # Campos das notificações.
    representante_nome: str | None = None
    representante_telefone: str | None = None
    representante_email: str | None = None
    utilizadores_afetados: int | None = None
    utilizadores_total: int | None = None
    zona_geografica: str | None = None
    transfronteirico: bool | None = None
    paises_afetados: str | None = None
    causa: str | None = None
    efeitos: str | None = None
    medidas: str | None = None
    situacao_residual: str | None = None
    tempo_recuperacao: str | None = None
    licoes_aprendidas: str | None
    criterios_fecho: str | None
    fechado_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Derivados (calculados no service, não persistidos):
    intercalar_ultimo_at: datetime | None = None
    voluntario: bool = False          # entidade fora do âmbito do regime
    tem_notificacoes: bool = False
    prazos: list[PrazoSchema] = []

    model_config = {"from_attributes": True}


class IncidenteDetalheSchema(IncidenteSchema):
    eventos: list[EventoSchema] = []


class ListaIncidentesSchema(BaseModel):
    total: int
    incidentes: list[IncidenteSchema]


class PainelIncidentesSchema(BaseModel):
    total: int
    abertos: int              # não fechados
    significativos_abertos: int
    prazos_em_risco: int      # marcos obrigatórios por cumprir a <24h ou em atraso
