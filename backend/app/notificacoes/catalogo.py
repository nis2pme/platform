"""
Catálogo das notificações — vocabulário do que a aplicação avisa.

Uma entrada por código. Daqui saem a categoria (por onde se filtra), a
severidade (por onde se ordena a atenção), a entidade a que o aviso pertence,
se ele descreve trabalho por fazer, e que avisos anteriores substitui.

Porque existe, em vez de cada produtor decidir por si:

1. **Uma só fonte.** Os produtores leem daqui e a API serve isto ao frontend,
   que constrói os filtros a partir do que existe mesmo — não de uma lista
   escrita à mão que envelhece em silêncio.
2. **Nunca levanta.** Um código desconhecido (linha antiga, ou instalação numa
   versão diferente) resolve para uma definição de recurso. Ler o que já está
   gravado não pode falhar.
3. **O texto não está aqui.** O catálogo diz o que as coisas SÃO; as palavras
   que o utilizador lê vivem no frontend, uma vez por idioma. É por isso que
   cada notificação guarda `params` em vez de uma frase pronta.

Os `params` esperados por cada código estão anotados ao lado da definição: são
o contrato com as chaves de tradução do frontend.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4


class Categoria:
    """Agrupadores de filtro. É esta lista que povoa o seletor de categoria."""

    INCIDENTES = "incidentes"
    TAREFAS = "tarefas"
    CONTROLOS = "controlos"
    AUDITORIA = "auditoria"
    CONETORES = "conetores"
    SISTEMA = "sistema"


class Severidade:
    """
    Urgência do que é preciso fazer.

    Usa deliberadamente as mesmas três palavras da trilha de auditoria: a
    interface já sabe desenhar esta escala, e duas escalas parecidas com nomes
    diferentes acabam sempre a divergir.
    """

    INFO = "info"
    AVISO = "aviso"
    CRITICO = "critico"


@dataclass(frozen=True)
class DefinicaoNotificacao:
    codigo: str
    categoria: str
    severidade: str
    # Tipo da entidade de origem, para o destino do link. `None` = sem alvo.
    entidade_tipo: str | None = None
    # Acionável = descreve trabalho por fazer. Não se dispensa por se visitar o
    # ecrã: só sai quando o facto deixa de ser verdade, ou por decisão explícita
    # de quem a recebeu. É o que impede um prazo legal ultrapassado de
    # desaparecer porque alguém passou pela página.
    acionavel: bool = False
    # Enquanto houver uma por ler com a mesma chave, não nasce outra igual.
    dedup: bool = True
    # Códigos que este torna obsoletos: ao nascer, as por ler desses códigos
    # para a MESMA entidade passam a lidas. É o que faz um aviso de "prazo a
    # expirar" dar lugar ao de "prazo ultrapassado" em vez de coexistirem.
    substitui: tuple[str, ...] = field(default_factory=tuple)
    # Chaves de `params` que trazem dados pessoais: são cifradas antes de ir
    # para a base e decifradas ao servir. Fica declarado aqui, e não em cada
    # produtor, porque quem acrescenta um código novo tem de tomar esta decisão
    # no mesmo sítio onde toma as outras — e porque um produtor que se esqueça
    # de cifrar não dá erro nenhum, grava em claro e ninguém repara.
    params_pii: tuple[str, ...] = field(default_factory=tuple)


class Codigo:
    """Constantes dos códigos. Os produtores referenciam estas, não literais."""

    INCIDENTE_PRAZO_RISCO = "incidente.prazo_risco"
    INCIDENTE_PRAZO_ATRASO = "incidente.prazo_atraso"
    TAREFA_PRAZO_PROXIMO = "tarefa.prazo_proximo"
    TAREFA_PRAZO_ATRASO = "tarefa.prazo_atraso"
    CONTROLO_APROVADO = "controlo.aprovado"
    CONTROLO_NAO_APROVADO = "controlo.nao_aprovado"
    PARECER_PEDIDO = "parecer.pedido_esclarecimento"
    CONETOR_DRIFT = "conetor.drift"
    CONETOR_CONTRADICAO = "conetor.contradicao"
    BACKUP_SEM_PASSPHRASE = "sistema.backup_sem_passphrase"
    BACKUP_INTERROMPIDO = "sistema.backup_interrompido"
    TRIAL_A_TERMINAR = "sistema.trial_a_terminar"
    LICENCA_A_EXPIRAR = "sistema.licenca_a_expirar"
    LICENCA_EM_TOLERANCIA = "sistema.licenca_em_tolerancia"


_C = Categoria
_S = Severidade

_DEFINICOES: tuple[DefinicaoNotificacao, ...] = (
    # --- Incidentes: prazos das notificações (RJC, arts. 42.º-44.º; RGPD, art. 33.º)
    # params: {"titulo": str, "prazo": ISO 8601,
    #          "marco": "notificacao_inicial|atualizacao|fim_impacto|relatorio_final|intercalar|cnpd"}
    # Chave: (incidente, marco, prazo) — um prazo que muda de data volta a avisar.
    DefinicaoNotificacao(
        Codigo.INCIDENTE_PRAZO_RISCO, _C.INCIDENTES, _S.AVISO,
        entidade_tipo="Incidente", acionavel=True,
    ),
    # Cruzar um prazo legal é um evento novo, não uma atualização do anterior —
    # daí nascer uma notificação própria, com peso próprio, que apaga a antiga.
    DefinicaoNotificacao(
        Codigo.INCIDENTE_PRAZO_ATRASO, _C.INCIDENTES, _S.CRITICO,
        entidade_tipo="Incidente", acionavel=True,
        substitui=(Codigo.INCIDENTE_PRAZO_RISCO,),
    ),

    # --- Tarefas recorrentes de conformidade --------------------------------
    # params: {"titulo": str, "dias": int}  (dias negativos = já passou)
    DefinicaoNotificacao(
        Codigo.TAREFA_PRAZO_PROXIMO, _C.TAREFAS, _S.AVISO,
        entidade_tipo="Tarefa", acionavel=True,
    ),
    DefinicaoNotificacao(
        Codigo.TAREFA_PRAZO_ATRASO, _C.TAREFAS, _S.CRITICO,
        entidade_tipo="Tarefa", acionavel=True,
        substitui=(Codigo.TAREFA_PRAZO_PROXIMO,),
    ),

    # --- Decisões de auditoria interna sobre um controlo --------------------
    # params: {"controlo": str, "auditor": str}   ("auditor" = nome de pessoa)
    # Sem dedup: cada decisão é um facto novo, e duas decisões seguidas sobre o
    # mesmo controlo têm ambas de chegar a quem o implementou.
    DefinicaoNotificacao(
        Codigo.CONTROLO_APROVADO, _C.CONTROLOS, _S.INFO,
        entidade_tipo="ControloEmpresaV2", dedup=False,
        params_pii=("auditor",),
    ),
    # Reprovar devolve trabalho a quem o fez: é acionável, ao contrário da
    # aprovação. Sem isso bastava abrir o controlo para ver o motivo e o aviso
    # dava-se por tratado — o retrabalho ficava sem lembrete nenhum.
    DefinicaoNotificacao(
        Codigo.CONTROLO_NAO_APROVADO, _C.CONTROLOS, _S.AVISO,
        entidade_tipo="ControloEmpresaV2", acionavel=True, dedup=False,
        params_pii=("auditor",),
    ),

    # --- Parecer do auditor externo -----------------------------------------
    # params: {"controlo": str | None, "texto": str}
    # O texto é escrito por um auditor sobre a empresa e é a mesma coisa que o
    # relatório de auditoria guarda cifrada; o código do controlo fica em claro
    # porque é por ele que se procura.
    DefinicaoNotificacao(
        Codigo.PARECER_PEDIDO, _C.AUDITORIA, _S.AVISO,
        entidade_tipo="ControloEmpresaV2", dedup=False,
        params_pii=("texto",),
    ),

    # --- Conetores (premium): verificação técnica ---------------------------
    # params: {"sinal": str, "de": str, "para": str, "controlos": str}
    DefinicaoNotificacao(
        Codigo.CONETOR_DRIFT, _C.CONETORES, _S.AVISO,
        entidade_tipo="ControloEmpresaV2", dedup=False,
    ),
    DefinicaoNotificacao(
        Codigo.CONETOR_CONTRADICAO, _C.CONETORES, _S.CRITICO,
        entidade_tipo="ControloEmpresaV2", dedup=False,
    ),

    # --- Sistema -------------------------------------------------------------
    # params: {}
    # O backup diário vem ligado de fábrica mas não arranca sem frase-secreta, e
    # o único sinal era um cartão nas Definições: uma instalação podia passar
    # meses sem uma única cópia. Sai quando a frase-secreta é definida.
    DefinicaoNotificacao(
        Codigo.BACKUP_SEM_PASSPHRASE, _C.SISTEMA, _S.CRITICO, acionavel=True,
    ),
    # params: {}
    # Um backup foi morto a meio (quase sempre falta de memória): o agendado não
    # volta a tentar nesse dia, para não entrar em ciclo. Sem este aviso a
    # instalação ficava sem cópias sem que ninguém soubesse.
    DefinicaoNotificacao(
        Codigo.BACKUP_INTERROMPIDO, _C.SISTEMA, _S.CRITICO, acionavel=True,
    ),
    # Fim do acesso: uma por marco (dias antes do fim), a do marco seguinte
    # dá a anterior por lida. params: {"data": "AAAA-MM-DD", "dias": int}
    DefinicaoNotificacao(Codigo.TRIAL_A_TERMINAR, _C.SISTEMA, _S.AVISO),
    DefinicaoNotificacao(Codigo.LICENCA_A_EXPIRAR, _C.SISTEMA, _S.AVISO),
    # A licença passou o fim e corre a tolerância; no fim dela os módulos
    # premium ficam só de leitura. params: {"data": ..., "ate": "AAAA-MM-DD"}
    DefinicaoNotificacao(Codigo.LICENCA_EM_TOLERANCIA, _C.SISTEMA, _S.CRITICO),
)

CATALOGO: dict[str, DefinicaoNotificacao] = {d.codigo: d for d in _DEFINICOES}

CATEGORIAS: tuple[str, ...] = (
    _C.INCIDENTES, _C.TAREFAS, _C.CONTROLOS,
    _C.AUDITORIA, _C.CONETORES, _C.SISTEMA,
)

SEVERIDADES: tuple[str, ...] = (_S.CRITICO, _S.AVISO, _S.INFO)

# Devolvida para códigos que não estão no catálogo. Não é um erro: uma linha
# gravada por outra versão continua a ter de ser legível.
_RECURSO = DefinicaoNotificacao(
    codigo="", categoria=_C.SISTEMA, severidade=_S.INFO,
)


def definicao(codigo: str | None) -> DefinicaoNotificacao:
    """O que um código significa. Nunca levanta."""
    if not codigo:
        return _RECURSO
    return CATALOGO.get(codigo, _RECURSO)


def chave(codigo: str, *partes: object) -> str:
    """
    Chave de deduplicação: o código mais aquilo que torna o aviso único.

    Para códigos sem dedup, junta um sufixo aleatório — a coluna é obrigatória
    e única no seu propósito, e assim dois eventos legítimos nunca se anulam um
    ao outro por acaso.
    """
    if not definicao(codigo).dedup:
        return ":".join([codigo, *(str(p) for p in partes), uuid4().hex])
    return ":".join([codigo, *(str(p) for p in partes)])
