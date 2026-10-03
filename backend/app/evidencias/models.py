"""
Modelos SQLModel do módulo de evidências.
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import Column
from sqlmodel import Field, SQLModel


class TipoEvidencia(str, Enum):
    TEXTO = "texto"
    FICHEIRO = "ficheiro"
    AMBOS = "ambos"   # tem texto E ficheiro em simultâneo


class Evidencia(SQLModel, table=True):
    """
    Prova de implementação de um controlo.
    Pode ser texto livre ou ficheiro (PDF, imagem, documento, etc.).
    Soft delete — evidências não são eliminadas fisicamente.
    """

    __tablename__ = "evidencias"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)

    # Ligação ao controlo implementado (V2)
    controlo_empresa_v2_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.UUID(as_uuid=True), nullable=True, index=True),
    )
    # Desnormalizado para queries rápidas sem join
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    titulo: Optional[str] = Field(default=None, max_length=255)  # título opcional da evidência
    tipo: TipoEvidencia = Field()

    # Conteúdo textual (tipo=texto ou ambos)
    conteudo_texto: Optional[str] = Field(default=None)

    # Ficheiro (tipo=ficheiro)
    ficheiro_path: Optional[str] = Field(default=None, max_length=500)
    ficheiro_nome: Optional[str] = Field(default=None, max_length=500)  # cifrado em repouso
    ficheiro_tipo: Optional[str] = Field(default=None, max_length=100)  # MIME type
    ficheiro_tamanho: Optional[int] = Field(default=None)               # bytes

    # Cifra Fernet — True se o conteúdo do ficheiro foi cifrado no upload
    ficheiro_cifrado: bool = Field(default=False)

    # Cifra Fernet — True se conteudo_texto foi cifrado em repouso
    conteudo_texto_cifrado: bool = Field(default=False)

    # Impressão digital do CONTEÚDO em claro (SHA-256 hex), calculada ANTES da cifra
    # Fernet (o texto cifrado tem IV aleatório e nunca seria comparável). Serve para
    # detetar evidências repetidas — o mesmo ficheiro/texto anexado outra vez sem
    # alterações — e evitar processamento e disco desnecessários. Nullable: evidências
    # antigas ficam sem hash (nunca recalculado retroativamente).
    conteudo_hash: Optional[str] = Field(default=None, max_length=64, index=True)

    # Quem fez upload
    uploaded_by_id: uuid.UUID = Field(foreign_key="utilizadores.id", index=True)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deleted_at: Optional[datetime] = Field(default=None)  # soft delete

    # --- Versões ------------------------------------------------------------
    # A evidência é IMUTÁVEL: substituir conteúdo cria uma linha nova que aponta
    # para a anterior por `substitui_id`. É o que permite dizer que a 2027-03-01 o
    # controlo estava evidenciado pela v1, mesmo depois de a v2 existir.
    #
    # Regra de ouro: **a versão é do documento; a adequação é da ligação.** Por
    # isso a ligação aponta para uma VERSÃO concreta e não para a mais recente —
    # senão uma revisão obrigaria todos os controlos ligados a acompanhá-la, e
    # seria impossível uma revisão servir dois controlos e não servir o terceiro.
    substitui_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.UUID(as_uuid=True), nullable=True, index=True),
    )
    substituida_em: Optional[datetime] = Field(default=None)

    # Data da próxima revisão. Alimenta o aviso "esta evidência tem três anos" e
    # liga ao módulo de tarefas recorrentes que já existe.
    valido_ate: Optional[datetime] = Field(default=None)

    # --- Lápide do apagamento a pedido do titular ---------------------------
    # O RGPD puxa ao contrário da retenção: o produto quer guardar a prova, mas
    # se ela tiver dados pessoais e houver pedido de apagamento tem de sair. O
    # conteúdo desaparece; fica o registo de que existiu, quando saiu, por ordem
    # de quem e porquê. A impressão digital é o `conteudo_hash` acima.
    #
    # Distinguir do soft delete normal não é detalhe: com as duas a marcar só o
    # `deleted_at`, a retenção não saberia qual delas pode ignorar.
    eliminacao_rgpd: bool = Field(default=False)
    # Cifrado em repouso: o motivo de um pedido de apagamento identifica com
    # frequência quem o fez.
    eliminacao_motivo: Optional[str] = Field(default=None, sa_column=Column(sa.Text))
    # Sem FK: a conta pode ser anonimizada depois, e a lápide tem de sobreviver.
    eliminacao_por_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.UUID(as_uuid=True), nullable=True),
    )

    # Relationships (sem FK gerida pelo ORM — join feito manualmente no service)
    # controlo_empresa gerido via UUID raw — ver evidencias/service.py


class EvidenciaRequisito(SQLModel, table=True):
    """Ligação N:N entre uma evidência e um requisito (controlo do tenant).

    Antes disto, `Evidencia.controlo_empresa_v2_id` era uma FK única: uma
    evidência pertencia a **um** controlo. Doía já — a política de segurança da
    informação evidencia vários controlos do próprio QNRCS e obrigava a carregar o
    mesmo ficheiro uma vez por controlo — e doía muito mais no multi-framework,
    onde cada norma nova duplicaria todas as evidências do cliente.

    **Append-only, como o resto do modelo.** Desligar preenche `desligado_em`;
    religar cria linha nova. Daí a chave primária ser sintética: com
    `PRIMARY KEY (evidencia_id, requisito_id)` seria impossível registar que uma
    evidência foi desligada e mais tarde religada — o segundo `ligar` colidiria
    com a linha antiga. O índice único parcial garante que só existe uma ligação
    **ativa** por par, e a história fica toda lá para responder a "que provas
    sustentavam este controlo em março".
    """

    __tablename__ = "evidencia_requisito"

    # A garantia descrita acima tem de nascer com a tabela. Enquanto viveu só na
    # migração que a criou, as instalações NOVAS ficavam sem ela: nessas, a
    # tabela é criada a partir destes modelos e a migração salta o bloco inteiro
    # por já a encontrar feita. `ligar()` faz consultar-depois-inserir e conta
    # com este índice para recusar a segunda linha — sem ele, dois pedidos
    # simultâneos deixavam duas ligações ativas para o mesmo par, em silêncio.
    __table_args__ = (
        sa.Index(
            "uq_evidencia_requisito_ativa",
            "evidencia_id",
            "requisito_id",
            unique=True,
            postgresql_where=sa.text("desligado_em IS NULL"),
            sqlite_where=sa.text("desligado_em IS NULL"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    evidencia_id: uuid.UUID = Field(foreign_key="evidencias.id", index=True)
    # Controlo do tenant (ControloEmpresaV2). Sem FK gerida pelo ORM, como o
    # resto do módulo — o join é feito no service.
    requisito_id: uuid.UUID = Field(
        sa_column=Column(sa.UUID(as_uuid=True), nullable=False, index=True),
    )
    # Desnormalizado, como na evidência: evita um join em todas as listagens e
    # mantém o isolamento por empresa verificável numa condição só.
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    # Onde olhar dentro de um documento que serve vários controlos: "secção 4.2",
    # "páginas 3-5", "anexo B". Sem isto o auditor abre o mesmo PDF em seis
    # controlos sem saber onde olhar, e uma evidência mista deixa de ser prova
    # para passar a ruído.
    nota_ambito: Optional[str] = Field(default=None, max_length=500)
    ambito_por_confirmar: bool = Field(default=False)

    ligado_por_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(sa.UUID(as_uuid=True), nullable=True),
    )
    ligado_em: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    desligado_em: Optional[datetime] = Field(default=None)
