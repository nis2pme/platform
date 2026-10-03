"""
Modelos SQLModel do módulo de notificações.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Index, Text
from sqlmodel import Field, SQLModel


class Notificacao(SQLModel, table=True):
    """
    Notificação in-app para um utilizador específico — uma linha por destinatário.

    O conteúdo é guardado ESTRUTURADO: `codigo` diz o que aconteceu e `params`
    traz os valores que entram na frase. Quem compõe o texto é o frontend, no
    idioma de quem está a ler. `titulo` e `mensagem` só têm conteúdo nas linhas
    gravadas antes desta mudança, e é por isso que são nuláveis.
    """

    __tablename__ = "notificacoes"
    __table_args__ = (
        # Padrão de acesso real: um utilizador, filtrado por estado, do mais
        # recente para o mais antigo. Serve a listagem E a contagem do sininho.
        # Sem DESC declarado de propósito — um btree percorre-se nos dois
        # sentidos e serve o ORDER BY descendente tal como está.
        Index("ix_notif_utilizador_lida_data", "utilizador_id", "lida", "created_at"),
        # Separadores e contagens por categoria.
        Index("ix_notif_utilizador_categoria", "utilizador_id", "categoria"),
        # Varrimento da retenção.
        Index("ix_notif_lida_lida_at", "lida", "lida_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)
    utilizador_id: uuid.UUID = Field(foreign_key="utilizadores.id")  # destinatário

    # O que aconteceu (entrada do catálogo). Nulo nas linhas anteriores ao
    # conteúdo estruturado: essas não têm `params` e por isso nunca poderiam ser
    # recompostas — mostram o `mensagem` que ficou gravado.
    codigo: Optional[str] = Field(default=None, max_length=100, index=True)
    categoria: str = Field(default="sistema", max_length=40)
    severidade: str = Field(default="info", max_length=20)

    # Impede que o mesmo aviso volte a nascer enquanto o anterior não for lido.
    # Inclui o estado do facto, para uma passagem de "em risco" a "em atraso"
    # contar como evento novo em vez de ser silenciada como repetição.
    chave_dedup: str = Field(max_length=255, index=True)

    # Valores que entram na frase traduzida, em JSON.
    params: Optional[str] = Field(default=None)

    # Texto congelado das linhas antigas. Não se escreve mais nada aqui.
    titulo: Optional[str] = Field(default=None, max_length=255)
    mensagem: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # Alvo do link. Sem chave estrangeira de propósito: aponta para tabelas
    # diferentes conforme o aviso, e tem de sobreviver ao desaparecimento do
    # alvo — uma notificação que ficou por ler não pode bloquear um apagamento.
    entidade_tipo: Optional[str] = Field(default=None, max_length=50)
    entidade_id: Optional[uuid.UUID] = Field(default=None)

    # Mantido à parte da entidade genérica: tem chave estrangeira real e alimenta
    # os marcadores na lista de controlos.
    controlo_empresa_id: Optional[uuid.UUID] = Field(
        default=None,
        foreign_key="controlos_empresa_v2.id",
        nullable=True,
        index=True,
    )

    # Descreve trabalho por fazer: não se dispensa por se visitar o ecrã.
    acionavel: bool = Field(default=False)

    lida: bool = Field(default=False)
    lida_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EmailEnvio(SQLModel, table=True):
    """
    Registo de emails de notificação já enviados — a chave única garante que
    cada aviso sai UMA vez, mesmo com ticks repetidos ou re-arranques.
    Exemplos de chave: "incidente:<id>:<marco>", "digest:<empresa_id>:2026-W29".
    """

    __tablename__ = "email_envios"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)
    chave: str = Field(max_length=255, unique=True, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
