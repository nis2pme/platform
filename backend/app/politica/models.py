"""
Desvio de uma empresa ao defeito da matriz de capacidades.

A tabela é ESPARSA: só existe linha onde a empresa decidiu algo diferente do
defeito do código. Consequências que se aproveitam de graça:

  - um módulo novo numa versão futura herda o defeito sem seed nenhum;
  - um defeito apertado numa versão futura chega a todas as instalações que não
    tocaram nessa célula, e respeita a escolha das que tocaram;
  - o estado de uma empresa lê-se em meia dúzia de linhas, não em centenas.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class PoliticaCapacidade(SQLModel, table=True):
    __tablename__ = "politica_capacidades"
    # Uma célula tem no máximo uma opinião por empresa. Declarada aqui e na
    # migração: sem isto, a base dos testes não tinha a mesma forma da real.
    __table_args__ = (
        UniqueConstraint(
            "empresa_id", "modulo", "classe", "papel", name="uq_politica_celula"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    # A célula: qual módulo, qual classe de ação, qual papel.
    modulo: str = Field(max_length=40)
    classe: str = Field(max_length=20)
    papel: str = Field(max_length=20)

    # "total" | "atribuido" | "nenhum" — o último retira a capacidade ao papel.
    ambito: str = Field(max_length=20)

    # Por que caminho foi decidido: "interruptor" (uma das opções nomeadas que a
    # plataforma oferece) ou "grelha" (edição célula a célula). Não muda o efeito
    # — muda o que o documento de funções e responsabilidades pode afirmar sobre
    # a organização, e é isso que um auditor foi ali procurar.
    origem: str = Field(default="interruptor", max_length=20)

    alterado_por: uuid.UUID | None = Field(default=None)
    alterado_em: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
