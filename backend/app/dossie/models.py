"""
Modelos do módulo de dossiês de auditoria.

Cada exportação de um dossiê fica registada aqui — sem conteúdo. O registo
permite conferir mais tarde a autenticidade de um ficheiro apresentado por
terceiros (mesmo sha256 = mesmo dossiê) e guarda a identidade de resposta:
a chave que permitirá abrir o parecer que um auditor vier a devolver sobre
este dossiê. A metade privada dessa chave fica cifrada em repouso.

O caminho inverso também vive aqui: quando um auditor devolve um parecer
(.nis2pme tipo=parecer), a importação regista-o em `pareceres_importados`
(anti-replay + selo no dashboard) e fixa a chave pública do auditor em
`auditores_confiaveis` (TOFU, modelo SSH: o admin confirma o fingerprint
uma vez; a partir daí a chave é reconhecida automaticamente).
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.shared.pii import TextoCifrado


class DossieGerado(SQLModel, table=True):
    """Registo imutável de um dossiê exportado (nunca guarda o conteúdo)."""

    __tablename__ = "dossies_gerados"

    # O id É o dossie_id que viaja no cabeçalho assinado do ficheiro.
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    # SHA-256 (hex) do corpo cifrado — a impressão digital do ficheiro gerado.
    sha256: str = Field(max_length=64)

    # O que o dossiê incluiu (JSON: modo de cifra, evidências, período, versão).
    ambito: Optional[str] = Field(default=None, sa_column=Column(Text))

    criado_por: Optional[uuid.UUID] = Field(
        default=None, foreign_key="utilizadores.id", nullable=True
    )
    criado_em: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), index=True
    )

    # Chave privada (age) do destinatário de resposta deste dossiê, cifrada em
    # repouso — só esta instância conseguirá decifrar um parecer devolvido.
    identidade_resposta: Optional[str] = Field(default=None, sa_column=Column(Text))


class AuditorConfiavel(SQLModel, table=True):
    """Chave pública (Ed25519) de um auditor externo fixada pela empresa.

    Trust-on-first-use: no primeiro parecer o admin confirma o fingerprint
    por outro canal e a chave fica fixada; pareceres seguintes com a mesma
    chave são reconhecidos sem nova cerimónia. Uma chave diferente volta a
    exigir confirmação explícita — nunca substitui uma fixada em silêncio.
    """

    __tablename__ = "auditores_confiaveis"
    __table_args__ = (
        UniqueConstraint("empresa_id", "pub", name="uq_auditor_confiavel_empresa_pub"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    # Chave pública Ed25519 (base64url, 43 chars) e o fingerprint legível.
    pub: str = Field(max_length=64, index=True)
    fingerprint: str = Field(max_length=19)

    # Nome do auditor tal como veio no parecer em que foi fixado (PII, cifrado).
    nome: Optional[str] = Field(default=None, max_length=500)

    criado_por: Optional[uuid.UUID] = Field(
        default=None, foreign_key="utilizadores.id", nullable=True
    )
    criado_em: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # O convite que veio no último ficheiro deste auditor, para o próximo envio
    # do dossiê sem trocar códigos à mão. É um segredo ao portador: fica cifrado
    # e nunca vai ao browser. Apaga-se quando é usado.
    proximo_convite: Optional[str] = Field(default=None, sa_column=Column(Text))
    proximo_convite_expira_em: Optional[datetime] = Field(default=None)


class ParecerImportado(SQLModel, table=True):
    """Registo imutável de um parecer de auditor importado.

    Anti-replay (o id É o identificador do cabeçalho assinado do parecer —
    importar duas vezes o mesmo parecer é recusado) e fonte do selo do
    dashboard: "dossiê de {data} revisto por {auditor} em {data}".
    """

    __tablename__ = "pareceres_importados"

    # O id vem do cabeçalho assinado do ficheiro (dossie_id do parecer).
    id: uuid.UUID = Field(primary_key=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    # O dossiê local a que o parecer responde (dossie_ref verificado).
    dossie_gerado_id: uuid.UUID = Field(foreign_key="dossies_gerados.id", index=True)

    # SHA-256 (hex) do corpo cifrado do ficheiro do parecer.
    sha256: str = Field(max_length=64)

    auditor_pub: str = Field(max_length=64)
    auditor_fingerprint: str = Field(max_length=19)
    # Nome do auditor declarado no parecer (PII, cifrado).
    auditor_nome: Optional[str] = Field(default=None, max_length=500)

    # Parecer global (nível validado, texto executivo, âmbito, limitações) —
    # JSON cifrado em repouso, como o texto dos relatórios de auditoria.
    global_json: Optional[str] = Field(default=None, sa_column=Column(Text))

    n_controlos: int = Field(default=0)
    n_achados: int = Field(default=0)
    n_pedidos: int = Field(default=0)

    # "auditoria" (parecer de auditor) ou "consultoria" (trabalho de consultor:
    # não conta como auditoria externa nem dá selo).
    natureza: str = Field(default="auditoria", max_length=20)
    # O parecer.json normalizado (global, controlos, achados, pedidos,
    # evidências analisadas), cifrado em repouso: é o que o ecrã mostra.
    conteudo: Optional[str] = Field(default=None, sa_column=Column(Text))

    # Quando o auditor emitiu o parecer (do cabeçalho assinado).
    parecer_criado_em: Optional[datetime] = Field(default=None)

    importado_por: Optional[uuid.UUID] = Field(
        default=None, foreign_key="utilizadores.id", nullable=True
    )
    importado_em: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), index=True
    )


class DossieCanalRelay(SQLModel, table=True):
    """Canal de resposta de um dossiê enviado pelo relay.

    Ao enviar o dossiê pelo relay, esta instância manda o hash de um token
    aleatório que só ela guarda. Com esse token procura e levanta o parecer que
    o auditor deixar no relay sobre o dossiê. Fica à parte de `dossies_gerados`
    (que é imutável) porque o canal tem estado que muda.

    Estados: `aguardando` (sem parecer), `disponivel` (há um parecer no relay
    por importar), `importado` (o último que chegou entrou; o auditor ainda o
    pode corrigir enquanto o canal estiver aberto), `expirado` (o canal fechou
    sem parecer importado) e `sem_canal` (o relay já não o conhece).
    """

    __tablename__ = "dossie_canais_relay"

    # O canal é 1:1 com o dossiê enviado.
    dossie_gerado_id: uuid.UUID = Field(foreign_key="dossies_gerados.id", primary_key=True)
    empresa_id: uuid.UUID = Field(foreign_key="empresas.id", index=True)

    # Destino, tal como veio no convite (validado contra RELAY_HOSTS_PERMITIDOS).
    relay_url: str = Field(max_length=500)
    relay_convite_id: str = Field(max_length=128)
    # Token de resposta (256 bits), cifrado em repouso: quem o tiver levanta o
    # parecer cifrado (não o lê, mas apaga-o do relay).
    token_resposta: str = Field(sa_column=Column(TextoCifrado, nullable=False))

    estado: str = Field(default="aguardando", max_length=20, index=True)
    canal_expira_em: datetime = Field()
    # O parecer que está no relay (sha256 do ficheiro inteiro) e até quando lá fica.
    parecer_sha256: Optional[str] = Field(default=None, max_length=64)
    parecer_expira_em: Optional[datetime] = Field(default=None)
    detetado_em: Optional[datetime] = Field(default=None)
    # O que entrou por último, e a confirmação ao relay que ficou por fazer (o
    # relay estava em baixo quando o parecer foi importado).
    importado_sha256: Optional[str] = Field(default=None, max_length=64)
    confirmar_sha256: Optional[str] = Field(default=None, max_length=64)
    lembrete_enviado: bool = Field(default=False)
    ultima_procura: Optional[datetime] = Field(default=None)

    criado_em: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
