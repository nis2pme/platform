"""
Modelos SQLModel partilhados do módulo de controlos.

  - DecisaoAuditor (enum)
  - RelatorioAuditoria (relatórios imutáveis do auditor)
  - HistoricoMaturidade (snapshots de evolução)
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


# ---------------------------------------------------------------------------
# HistoricoMaturidade — snapshots de evolução (para gráficos de tendência)
# ---------------------------------------------------------------------------

class DecisaoAuditor(str, Enum):
    APROVADO = "aprovado"
    NAO_APROVADO = "nao_aprovado"


class RelatorioAuditoria(SQLModel, table=True):
    """
    Relatório imutável criado pelo auditor na decisão de aprovação/rejeição.
    Cada transição para APROVADO ou NAO_APROVADO cria uma nova entrada.
    Suporta histórico completo por controlo.

    Um relatório pode também ser EXTERNO: criado pela importação de um parecer
    de auditor (ficheiro .nis2pme tipo=parecer). Nesse caso não há utilizador
    local (auditor_id fica vazio) e o tri-estado avaliado pelo auditor externo
    fica em estado_externo (conforme | parcial | nao_conforme), com a decisão
    interna mapeada (conforme → aprovado, resto → nao_aprovado).
    """

    __tablename__ = "relatorios_auditoria"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    controlo_empresa_v2_id: uuid.UUID | None = Field(
        default=None, foreign_key="controlos_empresa_v2.id", index=True, nullable=True
    )
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)
    # Vazio quando o relatório é externo (o auditor não é utilizador da app).
    auditor_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="utilizadores.id", index=True, nullable=True
    )
    auditor_nome: str = Field(max_length=500, default="")    # cache para evitar join — cifrado em repouso
    decisao: DecisaoAuditor = Field()
    texto: str = Field(sa_column=Column(Text))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)

    # Relatório externo (importado de um parecer assinado de auditor).
    externo: bool = Field(default=False)
    estado_externo: Optional[str] = Field(default=None, max_length=20)
    parecer_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="pareceres_importados.id", index=True, nullable=True
    )


class HistoricoMaturidade(SQLModel, table=True):
    """
    Snapshot do nível de maturidade em determinado momento.
    Guardado automaticamente quando o nível de um controlo/domínio muda.
    dominio_id=None significa snapshot do score global.
    """

    __tablename__ = "historico_maturidade"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)
    dominio_id: Optional[uuid.UUID] = Field(
        default=None,
        foreign_key="domains.id",
        nullable=True,
        index=True,
    )

    nivel_maturidade: float = Field()   # pode ser decimal (média ponderada)
    percentagem_conformidade: float = Field()  # 0–100

    data_snapshot: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)


class OrigemTransicao(str, Enum):
    """Quem provocou a mudança de estado.

    Nem toda a transição vem de uma pessoa a carregar num botão — e é por isso
    que o log de auditoria não serve para responder a esta pergunta.
    """

    UTILIZADOR = "utilizador"
    TICK = "tick"
    IMPORTACAO = "importacao"
    CONETOR = "conetor"
    SISTEMA = "sistema"   # migrações de framework, cascatas internas


class ControloEstadoHistorico(SQLModel, table=True):
    """História de conformidade por controlo — o registo do que era verdade e quando.

    O `HistoricoMaturidade` acima guarda o score **agregado**: responde a "como
    evoluiu a percentagem", não a "este controlo estava implementado, desde
    quando, e com que prova". O log de auditoria também não responde — é forense
    de **acesso**, tem retenção curta por desenho (`AUDIT_RETENCAO_DIAS`) e é
    arquivado; reconstruir "estado do controlo X a 2027-03-01" obrigaria a varrer
    o log inteiro e a reproduzir transições a partir de JSON guardado como texto,
    metade dele já fora da base.

    São coisas diferentes, com tempos de vida diferentes: o log de auditoria
    guarda-se por um ano, esta história dura o que durar o tenant.

    **Append-only, uma linha por transição REAL** — nunca fotografias periódicas.
    É essa a decisão que mantém o volume irrelevante: 107 controlos e, por
    excesso, cinco mudanças por controlo por ano dão ~535 linhas/ano, menos de
    2 MB por empresa numa década. Uma única evidência em PDF ocupa mais.
    """

    __tablename__ = "controlo_estado_historico"

    # O índice que serve a pergunta a que a tabela existe para responder — "em
    # que estado estava este controlo naquela data" — procura pelo controlo e
    # ordena por instante, numa passagem só. Declarado aqui e não apenas na
    # migração: numa instalação nova a tabela nasce destes modelos, e sem isto
    # ficava só com o índice de uma coluna.
    __table_args__ = (
        sa.Index(
            "ix_controlo_estado_historico_controlo_em",
            "controlo_empresa_id",
            "ocorrido_em",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)
    # Sem `index=True`: o índice composto acima já começa por esta coluna, e um
    # segundo índice só do prefixo seria peso de escrita sem leitura que o use.
    controlo_empresa_id: uuid.UUID = Field(
        sa_column=Column(sa.UUID(as_uuid=True), nullable=False),
    )

    # NULL no estado anterior = primeira transição registada deste controlo.
    estado_anterior: Optional[str] = Field(default=None, max_length=30)
    estado_novo: str = Field(max_length=30)

    # A régua da altura. O nível determina os controlos mínimos, portanto mudá-lo
    # muda a avaliação retroativamente — sem isto, a conformidade de 2026 acaba
    # julgada com o nível de 2028.
    nivel_qnrcs_em_vigor: Optional[str] = Field(default=None, max_length=30)

    # Ids das evidências ligadas ao controlo NO MOMENTO da transição, em JSON.
    # Guarda-se a fotografia e não um join: as ligações mudam depois, e a
    # pergunta é "com que prova é que isto foi dado como implementado então".
    evidencias: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))

    # Texto na base, e não um tipo enumerado, pela mesma razão que o `papel` da
    # adesão: um tipo enumerado obrigaria a uma migração de tipo a cada origem
    # nova. Deixado ao SQLModel, o enum de Python virava um tipo do Postgres e a
    # decisão só valia para quem tivesse atualizado — nas instalações novas,
    # acrescentar uma origem passava a rebentar.
    origem: OrigemTransicao = Field(
        default=OrigemTransicao.UTILIZADOR,
        sa_column=Column(sa.String(length=20), nullable=False),
    )
    utilizador_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.UUID(as_uuid=True), nullable=True),
    )

    ocorrido_em: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), index=True
    )
