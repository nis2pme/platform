"""
Modelos SQLModel do módulo de Tarefas recorrentes (core).

Cobre as obrigações do QNRCS cujo critério de verificação pede um registo *vivo e
recorrente* — rever acessos, testar cópias de segurança, rever logs, formação anual,
avaliar fornecedores, rever políticas e planos, testar continuidade/recuperação. A
série de conclusões de cada tarefa é a "evidência da gestão quotidiana" que vários
critérios exigem.

Duas tabelas:
  - `tarefas`            — a obrigação e a sua periodicidade + o próximo prazo.
  - `tarefa_conclusoes`  — cada vez que a tarefa foi cumprida (APPEND-ONLY):
                           data, quem, notas e, para testes/exercícios, o resultado
                           e as lições aprendidas. Nunca se altera nem apaga (prova).
"""
import uuid
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel

from app.shared.pii import TextoCifrado


class TipoTarefa(str, Enum):
    RECORRENTE = "recorrente"          # obrigação periódica (rever acessos, logs, ...)
    TESTE_EXERCICIO = "teste_exercicio"  # teste/exercício com resultado (backups, PCN, PRD)


class Periodicidade(str, Enum):
    MENSAL = "mensal"
    TRIMESTRAL = "trimestral"
    SEMESTRAL = "semestral"
    ANUAL = "anual"
    PERSONALIZADA = "personalizada"    # intervalo em dias definido pelo utilizador
    PONTUAL = "pontual"                # uma só vez (ex.: um exercício específico)


class ResultadoTeste(str, Enum):
    """Resultado de um teste/exercício (só se aplica a tipo=teste_exercicio)."""
    SUCESSO = "sucesso"
    PARCIAL = "parcial"
    FALHA = "falha"


class Tarefa(SQLModel, table=True):
    """Obrigação recorrente (ou teste/exercício) e o seu próximo prazo."""

    __tablename__ = "tarefas"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    # Origem no catálogo de obrigações pré-definidas (None = tarefa personalizada).
    chave_catalogo: Optional[str] = Field(default=None, max_length=80, index=True)

    titulo: str = Field(max_length=255)
    descricao: str = Field(default="", sa_column=Column(Text))

    # Agrupador simples para o ecrã (revisao_acessos, teste_backups, ...). Texto livre.
    categoria: str = Field(default="outro", max_length=80)
    tipo: TipoTarefa = Field(default=TipoTarefa.RECORRENTE)

    # Controlos do QNRCS a que a tarefa dá evidência (códigos separados por vírgula).
    controlos: str = Field(default="", max_length=500)

    periodicidade: Periodicidade = Field(default=Periodicidade.ANUAL)
    # Só usado quando periodicidade == personalizada.
    periodicidade_dias: Optional[int] = Field(default=None)

    # Responsável pela execução (delegação transversal via matriz de capacidades).
    responsavel_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="utilizadores.id", nullable=True, index=True
    )

    # Próximo prazo de execução. Avança a cada conclusão (ou fica no fim se pontual).
    proximo_prazo: date = Field(index=True)
    # Quando foi cumprida pela última vez (None = ainda nunca).
    ultima_conclusao_at: Optional[date] = Field(default=None)

    # Tarefa ativa no radar. Uma tarefa pontual fica inativa ao ser cumprida.
    ativa: bool = Field(default=True, index=True)

    # Preenchido quando a tarefa nasceu de um achado de um parecer de auditor
    # importado — o plano de ação automático do round-trip de auditoria.
    origem_parecer_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="pareceres_importados.id", nullable=True, index=True
    )

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deleted_at: Optional[datetime] = Field(default=None)  # soft delete


class TarefaConclusao(SQLModel, table=True):
    """Registo de uma execução da tarefa — APPEND-ONLY (a série é a evidência)."""

    __tablename__ = "tarefa_conclusoes"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    tarefa_id: uuid.UUID = Field(foreign_key="tarefas.id", index=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    concluida_em: date = Field()
    # Texto livre, cifrado em repouso (nomes e pormenores de quem executou).
    notas: str = Field(default="", sa_column=Column(TextoCifrado))

    # Só para testes/exercícios (ID.MC-2): resultado e lições aprendidas.
    resultado: Optional[ResultadoTeste] = Field(default=None)
    licoes_aprendidas: Optional[str] = Field(default=None, sa_column=Column(TextoCifrado))

    autor_id: Optional[uuid.UUID] = Field(
        default=None, foreign_key="utilizadores.id", index=True
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), index=True
    )
