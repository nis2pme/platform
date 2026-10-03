"""
Modelos SQLModel do módulo de Formação (core).

Cobre o registo das ações de sensibilização e formação em cibersegurança:
  - PR.FC-1 — todo o pessoal é sensibilizado e formado (distinção sensibilização
    vs formação).
  - PR.FC-2 (Básico) — formação do órgão de gestão, que é a exigência legal do
    RJC, arts. 25.º, n.º 1, al. d), e 27.º, n.º 1, al. f) (a gestão assegura a formação e os seus titulares também a fazem).

Duas tabelas:
  - `formacao_acoes`        — a ação de formação/sensibilização e o seu estado.
  - `formacao_participantes` — quem participou (utilizadores internos + nomes
                               externos), com a presença registada.
"""
import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


class TipoFormacao(str, Enum):
    SENSIBILIZACAO = "sensibilizacao"  # sessão curta de sensibilização/awareness
    FORMACAO = "formacao"              # formação estruturada (PR.FC-1)


class EstadoFormacao(str, Enum):
    PLANEADA = "planeada"
    REALIZADA = "realizada"
    CANCELADA = "cancelada"


class AcaoFormacao(SQLModel, table=True):
    """Ação de sensibilização ou formação em cibersegurança."""

    __tablename__ = "formacao_acoes"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    titulo: str = Field(max_length=255)
    descricao: str = Field(default="", sa_column=Column(Text))

    tipo: TipoFormacao = Field(default=TipoFormacao.SENSIBILIZACAO)
    # "presencial" | "online" | "misto". Texto livre validado no service.
    formato: str = Field(default="presencial", max_length=40)
    estado: EstadoFormacao = Field(default=EstadoFormacao.PLANEADA, index=True)

    data: date = Field(index=True)              # data prevista ou de realização
    duracao_horas: Optional[float] = Field(default=None)
    formador: str = Field(default="", max_length=255)  # interno ou entidade externa

    # Destinou-se (também) ao órgão de gestão? Evidência do PR.FC-2 / RJC, arts. 25.º e 27.º.
    orgao_gestao: bool = Field(default=False, index=True)

    # Indicadores de eficácia (PR.FC-1 Elevado) — opcionais.
    eficacia_metodo: Optional[str] = Field(default=None, sa_column=Column(Text))
    eficacia_resultado: Optional[str] = Field(default=None, sa_column=Column(Text))

    responsavel_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="utilizadores.id", nullable=True, index=True
    )
    realizada_at: Optional[date] = Field(default=None)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deleted_at: Optional[datetime] = Field(default=None)  # soft delete


class ParticipanteFormacao(SQLModel, table=True):
    """Participante de uma ação de formação (interno ou externo)."""

    __tablename__ = "formacao_participantes"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    acao_id: uuid.UUID = Field(foreign_key="formacao_acoes.id", index=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    # Utilizador interno (se aplicável); senão, participante externo pelo nome.
    utilizador_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="utilizadores.id", nullable=True, index=True
    )
    # Nome (externo ou cache do interno) — cifrado em repouso como qualquer
    # outro nome de pessoa; decifra-se ao servir e ao comparar.
    nome: str = Field(default="", max_length=500)

    # Este participante é do órgão de gestão? (precisão adicional para o PR.FC-2).
    orgao_gestao: bool = Field(default=False)
    presente: bool = Field(default=True)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
