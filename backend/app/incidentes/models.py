"""
Modelos SQLModel do módulo de Incidentes (core).

Cobre o ciclo de vida da resposta a incidentes de cibersegurança (RS.*/RC.*) e,
sobretudo, as notificações do Regime Jurídico da Cibersegurança (RJC,
arts. 41.º a 44.º): notificação inicial, atualização, fim de impacto
significativo, relatório final e relatórios intercalares — e a notificação à
CNPD de uma violação de dados pessoais (RGPD, art. 33.º). Os prazos calculam-se
em `app/incidentes/prazos.py`.

Três tabelas:
  - `incidentes`              — o incidente, o estado da resposta, os factos de
                                que os prazos dependem e os marcos cumpridos.
  - `incidente_eventos`       — linha temporal APPEND-ONLY (quem fez o quê,
                                quando): notas, ações, decisões, mudanças de
                                estado, comunicações e marcos.
  - `incidente_notificacoes`  — APPEND-ONLY: cópia cifrada de cada notificação
                                marcada como enviada, com o canal, a referência
                                e o hash do conteúdo. É a prova do que se entregou.
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel

from app.shared.pii import TextoCifrado


class EstadoIncidente(str, Enum):
    ABERTO = "aberto"          # registado, ainda por triar/analisar
    EM_ANALISE = "em_analise"  # a investigar (RS.GI/AI)
    CONTIDO = "contido"        # contenção aplicada (RS.MI-1)
    RESOLVIDO = "resolvido"    # resolvido/recuperado (RS.MI-2, RC.PR)
    FECHADO = "fechado"        # encerrado com critérios cumpridos (RC.PR-6)


class SeveridadeIncidente(str, Enum):
    BAIXA = "baixa"
    MEDIA = "media"
    ALTA = "alta"
    CRITICA = "critica"


class TipoEventoIncidente(str, Enum):
    NOTA = "nota"              # observação livre
    ACAO = "acao"             # ação tomada (RS.MI)
    DECISAO = "decisao"       # decisão registada (ex.: não conter)
    ESTADO = "estado"         # mudança de estado
    COMUNICACAO = "comunicacao"  # parte interessada informada (RS.NC-1, RC.CO-1)
    MARCO = "marco"           # marco legal cumprido (alerta/notificação/relatório)


class Incidente(SQLModel, table=True):
    """Incidente de cibersegurança e o estado da resposta."""

    __tablename__ = "incidentes"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    titulo: str = Field(max_length=255)
    descricao: str = Field(default="", sa_column=Column(Text))

    # Código do tipo na taxonomia de incidentes (`classe.tipo`, ver
    # dados/taxonomia_incidentes.json). Validado no service.
    categoria: str = Field(default="outro.indeterminado", max_length=80)
    severidade: SeveridadeIncidente = Field(default=SeveridadeIncidente.MEDIA)
    estado: EstadoIncidente = Field(default=EstadoIncidente.ABERTO, index=True)

    # Significativo para efeitos de notificação. None = ainda por avaliar.
    significativo: Optional[bool] = Field(default=None)
    # Quando se concluiu que era significativo: é daqui que contam a notificação
    # inicial e a atualização. Sem ela, conta o conhecimento.
    significativo_em: Optional[datetime] = Field(default=None)

    # Responsável pelo tratamento (RS.GI-1). Delegação transversal (matriz de capacidades).
    responsavel_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="utilizadores.id", nullable=True, index=True
    )

    # Datas-chave. `conhecido_at` é a deteção.
    conhecido_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ocorrido_at: Optional[datetime] = Field(default=None)
    # Quando o incidente assumiu e perdeu o impacto significativo.
    impacto_inicio_em: Optional[datetime] = Field(default=None)
    fim_impacto_em: Optional[datetime] = Field(default=None)

    # Factos de que os prazos dependem.
    # Resolvido nas 2 h após a deteção: confirmação expressa, nunca inferida.
    resolvido_2h: bool = Field(default=False)
    atualizacao_necessaria: bool = Field(default=False)
    # Justificação de a notificação em 24 h ser incompatível com a mitigação.
    excecao_24h: Optional[str] = Field(default=None, sa_column=Column(TextoCifrado))
    # Pedido de relatórios intercalares pela autoridade.
    intercalar_pedido_em: Optional[datetime] = Field(default=None)
    # Houve violação de dados pessoais: notificação à CNPD.
    cnpd_aplicavel: bool = Field(default=False)

    # Marcos cumpridos (quando preenchidos; senão ainda em falta). Os intercalares
    # não têm coluna: vivem só em `incidente_notificacoes`.
    notificacao_inicial_at: Optional[datetime] = Field(default=None)
    atualizacao_at: Optional[datetime] = Field(default=None)
    fim_impacto_notificado_at: Optional[datetime] = Field(default=None)
    relatorio_final_at: Optional[datetime] = Field(default=None)
    cnpd_notificado_at: Optional[datetime] = Field(default=None)

    # Representante para contacto da autoridade, quando não é o ponto de
    # contacto permanente. Dados pessoais: cifrados em repouso.
    representante_nome: Optional[str] = Field(default=None, sa_column=Column(TextoCifrado))
    representante_telefone: Optional[str] = Field(default=None, sa_column=Column(TextoCifrado))
    representante_email: Optional[str] = Field(default=None, sa_column=Column(TextoCifrado))

    # Impacto.
    utilizadores_afetados: Optional[int] = Field(default=None)
    utilizadores_total: Optional[int] = Field(default=None)
    zona_geografica: Optional[str] = Field(default=None, max_length=255)
    transfronteirico: Optional[bool] = Field(default=None)
    paises_afetados: Optional[str] = Field(default=None, max_length=255)
    tempo_recuperacao: Optional[str] = Field(default=None, max_length=120)

    # Texto livre com nomes e pormenores de pessoas: cifrado em repouso.
    causa: Optional[str] = Field(default=None, sa_column=Column(TextoCifrado))
    efeitos: Optional[str] = Field(default=None, sa_column=Column(TextoCifrado))
    medidas: Optional[str] = Field(default=None, sa_column=Column(TextoCifrado))
    situacao_residual: Optional[str] = Field(default=None, sa_column=Column(TextoCifrado))
    licoes_aprendidas: Optional[str] = Field(default=None, sa_column=Column(TextoCifrado))  # ID.MC-3
    criterios_fecho: Optional[str] = Field(default=None, sa_column=Column(TextoCifrado))     # RC.PR-6

    fechado_at: Optional[datetime] = Field(default=None)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deleted_at: Optional[datetime] = Field(default=None)  # soft delete


class IncidenteEvento(SQLModel, table=True):
    """Entrada da linha temporal de um incidente — APPEND-ONLY (prova)."""

    __tablename__ = "incidente_eventos"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    incidente_id: uuid.UUID = Field(foreign_key="incidentes.id", index=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    tipo: TipoEventoIncidente = Field()
    # Cifrado em repouso: é aqui que fica quem fez o quê durante o incidente.
    # Nas linhas que o sistema escreve (`codigo` preenchido) é só a nota que o
    # utilizador juntou, se juntou; a frase compõe-se na leitura. Numa linha sem
    # código — escrita por um utilizador, ou anterior ao código — é o texto todo.
    texto: str = Field(default="", sa_column=Column(TextoCifrado))
    # O que o sistema registou, em código (ver `texto_evento` no service): quem
    # lê escolhe a língua da frase.
    codigo: Optional[str] = Field(default=None, max_length=40)
    # Parâmetros desse código, em JSON, cifrados: a referência atribuída pela
    # autoridade pode identificar o processo.
    params: Optional[str] = Field(default=None, sa_column=Column(TextoCifrado))
    # Para comunicações: a parte interessada informada (ver `PARTES` no service).
    parte: Optional[str] = Field(default=None, max_length=80)

    autor_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="utilizadores.id", index=True
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), index=True
    )


class IncidenteNotificacao(SQLModel, table=True):
    """Notificação marcada como enviada — APPEND-ONLY (prova do que se entregou).

    Guarda o documento desse tipo tal como estava no momento do registo, cifrado,
    com o hash do JSON canónico: o que se mostra depois é o que foi entregue,
    mesmo que o incidente mude a seguir. Não há caminho de atualização nem de
    eliminação."""

    __tablename__ = "incidente_notificacoes"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    incidente_id: uuid.UUID = Field(foreign_key="incidentes.id", index=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    # Código do marco (ver `MARCOS` em prazos.py). Texto validado no service.
    tipo: str = Field(max_length=40)
    enviada_em: datetime = Field()
    # myciber | email | telefone | outro. Texto validado no service.
    canal: str = Field(max_length=40)
    # Referência atribuída pela plataforma da autoridade (quando há).
    referencia: Optional[str] = Field(default=None, max_length=255)
    # JSON do documento congelado, cifrado em repouso.
    conteudo: str = Field(default="", sa_column=Column(TextoCifrado))
    # SHA-256 (hex) do JSON canónico do documento.
    hash_conteudo: str = Field(max_length=64)

    autor_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="utilizadores.id", index=True
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
