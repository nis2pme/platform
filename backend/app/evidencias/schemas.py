"""
Schemas Pydantic para o módulo de evidências.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.evidencias.models import TipoEvidencia


# ---------------------------------------------------------------------------
# Evidência (resposta)
# ---------------------------------------------------------------------------

class EvidenciaSchema(BaseModel):
    id: uuid.UUID
    controlo_empresa_id: uuid.UUID
    empresa_id: uuid.UUID
    tipo: TipoEvidencia

    titulo: str | None = None        # título descritivo (opcional)
    conteudo_texto: str | None = None
    conteudo_resumo: str | None = None

    # Ficheiro — não expõe o path interno do servidor
    ficheiro_nome: str | None = None
    ficheiro_tipo: str | None = None
    ficheiro_tamanho: int | None = None

    uploaded_by_id: uuid.UUID
    uploaded_by_nome: str | None = None  # injectado em service
    created_at: datetime
    deleted_at: datetime | None = None

    # Onde olhar no documento, **neste** controlo. Vem da ligação e não da
    # evidência: a mesma prova serve vários controlos por sítios diferentes do
    # texto. Só é preenchido quando se sabe em que controlo se está a olhar.
    nota_ambito: str | None = None
    ambito_por_confirmar: bool = False
    # Quantos controlos esta prova sustenta hoje. É o que permite ao ecrã
    # distinguir «retirar daqui» de «eliminar», que passaram a ser coisas
    # diferentes e com consequências diferentes.
    total_ligacoes: int = 1
    # Porque é que esta prova se tem de guardar (`historico`, `dossie`,
    # `aprovado`). Vazio = nunca foi prova: um engano retira-se e vai para a
    # reciclagem, em vez de ficar guardado para sempre como versão anterior.
    motivos_retencao: list[str] = []

    # Deduplicação (preenchidos só na criação; não persistidos):
    #  - criado=False → já existia uma evidência idêntica e não se criou nova
    #  - duplicado=True → o conteúdo é igual a outra evidência ativa deste controlo
    criado: bool = True
    duplicado: bool = False

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------

class ListaEvidenciasSchema(BaseModel):
    total: int
    evidencias: list[EvidenciaSchema]


# ---------------------------------------------------------------------------
# Listagem global (enriquecida com dados do controlo/domínio)
# ---------------------------------------------------------------------------

class EvidenciaComControloSchema(EvidenciaSchema):
    """Evidencia com contexto do controlo e objetivo — para listagem global."""
    controlo_codigo: str | None = None
    controlo_titulo: str | None = None
    controlo_estado: str | None = None   # estado de ControloEmpresa
    dominio_codigo: str | None = None
    dominio_nome: str | None = None


class ListaTodasEvidenciasSchema(BaseModel):
    total: int
    evidencias: list[EvidenciaComControloSchema]


# ---------------------------------------------------------------------------
# Ligações evidência ↔ controlo
# ---------------------------------------------------------------------------

class LigacaoSchema(BaseModel):
    """Uma ligação ativa, do ponto de vista da evidência.

    Leva o código e o título do controlo porque a pergunta que este objeto serve
    é *"que controlos é que esta prova sustenta?"* — e um UUID não responde a
    isso a quem está a olhar para o ecrã.
    """
    requisito_id: uuid.UUID
    controlo_codigo: str | None = None
    controlo_titulo: str | None = None
    controlo_estado: str | None = None

    nota_ambito: str | None = None
    ambito_por_confirmar: bool = False

    ligado_em: datetime
    ligado_por_nome: str | None = None

    model_config = {"from_attributes": True}


class ListaLigacoesSchema(BaseModel):
    total: int
    ligacoes: list[LigacaoSchema]


class OrigemSchema(BaseModel):
    """Um controlo de onde a evidência saiu — para onde "restaurar" a devolve."""
    requisito_id: uuid.UUID
    controlo_codigo: str | None = None
    controlo_titulo: str | None = None


class OrfaSchema(BaseModel):
    """Evidência sem controlo associado — na reciclagem, não no caixote."""
    id: uuid.UUID
    titulo: str | None = None
    tipo: str
    ficheiro_tamanho: int | None = None
    ficheiro_nome: str | None = None
    ficheiro_tipo: str | None = None
    uploaded_by_id: uuid.UUID | None = None
    carregada_por_nome: str | None = None
    created_at: datetime | None = None
    # De onde saiu (o último gesto que a deixou órfã) e por mão de quem.
    saiu_de: list[OrigemSchema] = []
    retirada_por_nome: str | None = None
    # Preenchido quando saiu por ter sido substituída: restaurá-la duplicaria a
    # prova, porque a versão nova já sustenta esses controlos.
    substituida_por_id: uuid.UUID | None = None

    orfa_desde: datetime
    dias_orfa: int
    # Quantos dias faltam até ser apagada. `None` quando é retida: já foi prova
    # de um controlo ou pode ter saído num dossiê, e por isso não se recicla.
    dias_ate_reciclar: int | None = None
    retida: bool = False
    motivos_retencao: list[str] = []
    # Se QUEM PEDE a pode apagar de vez agora. Decidido aqui e não no ecrã, para
    # a regra (não retida; administração, ou quem a carregou) viver num sítio só.
    pode_apagar: bool = False


class ListaOrfasSchema(BaseModel):
    total: int
    dias_reciclagem: int
    # Prazo de conservação das provas retidas, em anos. Zero = por definir.
    retencao_anos: int = 0
    orfas: list[OrfaSchema]


class NaoRestauradoSchema(BaseModel):
    requisito_id: uuid.UUID
    controlo_codigo: str | None = None
    # `sem_permissao` (não opera esse controlo) ou `controlo_inexistente`.
    motivo: str


class RestauroSchema(BaseModel):
    """O que voltou e o que ficou de fora — a resposta diz os dois."""
    restaurados: list[LigacaoSchema]
    nao_restaurados: list[NaoRestauradoSchema] = []


class LigarPedido(BaseModel):
    """Ligar uma evidência já existente a mais um controlo."""
    requisito_id: uuid.UUID
    nota_ambito: str | None = None


class MetadadosPedido(BaseModel):
    """Corrigir o que descreve a evidência — **sem** criar versão.

    Separado de substituir conteúdo de propósito: se fosse o mesmo gesto, uma
    gralha no título deixaria sete versões da mesma política para trás.
    """
    titulo: str | None = None
    valido_ate: datetime | None = None


class ApagamentoRgpdPedido(BaseModel):
    """Forma antiga do apagamento com lápide (só «pedido do titular»).

    Mantida durante uma versão para quem ainda a chame; a nova é
    `ApagamentoComLapidePedido`.
    """
    motivo: str = Field(max_length=1000)
    confirmado: bool = False


class ApagarDefinitivamentePedido(BaseModel):
    """Apagar de vez uma órfã que nunca foi prova.

    `razao` é um código fechado e vai para a trilha de auditoria. `texto` é
    livre, vai cifrado para a linha da evidência e nunca para a trilha — a trilha
    não se reescreve, e o texto pode conter os dados que se estão a apagar.
    Obrigatório quando a razão é `outro`.
    """
    razao: str
    texto: str | None = Field(default=None, max_length=1000)


class ApagarDefinitivamenteLotePedido(ApagarDefinitivamentePedido):
    """O mesmo, para várias órfãs com a mesma razão. Cada uma é verificada."""
    evidencia_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


class RecusaApagamentoSchema(BaseModel):
    id: uuid.UUID
    # `nao_encontrada`, `sem_permissao`, `evidencia_ligada` ou `evidencia_retida`.
    codigo: str
    motivos: list[str] = []


class ResultadoApagamentoSchema(BaseModel):
    apagadas: list[uuid.UUID]
    recusadas: list[RecusaApagamentoSchema]


class ApagamentoComLapidePedido(BaseModel):
    """Apagar prova — retida ou ainda ligada — deixando lápide.

    `fundamento` é um código fechado e vai para a trilha. `texto` é obrigatório,
    fica cifrado na linha da evidência e nunca vai para a trilha.
    `sem_obrigacao_conservar` é a confirmação explícita de que não se aplica uma
    obrigação legal de conservar nem é prova num litígio: sem ela, não se apaga.
    Sem `confirmado`, responde 409 com o impacto.
    """
    fundamento: str
    texto: str = Field(max_length=1000)
    sem_obrigacao_conservar: bool = False
    confirmado: bool = False


class LapideSchema(BaseModel):
    """O que resta de uma prova apagada. Sem título: o título pode ser ele
    próprio o dado pessoal. Prova-se pela impressão digital do conteúdo."""
    id: uuid.UUID
    conteudo_hash: str | None = None
    eliminada_em: datetime | None = None
    eliminada_por_nome: str | None = None
    fundamento: str | None = None
    # Só para a administração: o texto pode identificar quem pediu o apagamento.
    motivo: str | None = None


class VersaoSchema(BaseModel):
    """Um elo da cadeia de versões de uma evidência."""
    id: uuid.UUID
    titulo: str | None = None
    criado_em: datetime
    substituida_em: datetime | None = None
    # `ligada`, `orfa`, `apagada` ou `lapide`.
    estado: str
    total_ligacoes: int = 0
    motivos_retencao: list[str] = []
    uploaded_by_nome: str | None = None
    # A versão a partir da qual se pediu a cadeia.
    atual: bool = False


class ListaVersoesSchema(BaseModel):
    total: int
    versoes: list[VersaoSchema]


class NotaAmbitoPedido(BaseModel):
    """Onde olhar dentro do documento, para este controlo em concreto.

    `None` limpa a nota. Alterá-la limpa também o `ambito_por_confirmar`: quem
    escreve a nota está, por definição, a confirmá-la.
    """
    nota_ambito: str | None = None
