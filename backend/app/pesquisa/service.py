"""
Pesquisa global — parte do core.

Procura nas entidades que o core possui (controlos, incidentes, tarefas,
evidências, formação). Os dados do sidecar (ativos, riscos, fornecedores) são
pedidos à parte, em `app.premium.pesquisa_client`, e juntos no router.

A pesquisa é um SEGUNDO caminho de leitura sobre tabelas que os módulos já
servem pelos seus próprios ecrãs. Por isso tudo o que ela pode devolver está
declarado numa tabela única (`PESQUISAVEIS`), e um só ciclo decide quem vê o
quê: cada entrada nomeia a célula da matriz que a autoriza, e é o ciclo — não o
SQL de cada entidade — que pergunta a capacidade e aplica o âmbito. Acrescentar
uma entidade pesquisável é acrescentar uma linha; não há forma de a declarar sem
dizer que módulo a autoriza, e a autorização passa a vir de graça.

Regras:
  - SEMPRE isolado por empresa (o `empresa_id` entra em todas as queries).
  - NUNCA pesquisa campos PII cifrados (nomes de utilizador): o Fernet usa IV
    aleatório, o texto cifrado não é comparável — e não é preciso.
  - Correspondência insensível a maiúsculas e, quando a base o permite, a
    acentos ("seguranca" encontra "Segurança").
  - A expressão é partida em TERMOS e todos têm de casar, em qualquer ordem e
    em qualquer dos campos de texto da entidade: "ciber testes" e "testes
    ciber" encontram ambos "testes e exercícios de cibersegurança".
  - Quem só alcança o que lhe está atribuído vê na pesquisa o mesmo que vê na
    listagem do módulo — o critério é lido da matriz, nunca do papel.
"""
import logging
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import and_, case, func
from sqlmodel import Session, select

from app.evidencias.models import Evidencia, EvidenciaRequisito
from app.formacao.models import AcaoFormacao
from app.frameworks.models import Control, ControlLocale, ControloEmpresaV2
from app.incidentes.models import Incidente
from app.pesquisa.schemas import ResultadoPesquisaSchema
from app.shared.capacidades import ClasseAcao, so_atribuidos, tem_capacidade
from app.tarefas.models import Tarefa

logger = logging.getLogger(__name__)

# Abaixo disto a pesquisa é ruído (e custo).
Q_MIN = 2
# Termos considerados numa expressão; os seguintes são ignorados. Cada termo
# acrescenta uma condição por linha, por isso o teto é também anti-abuso.
Q_MAX_TERMOS = 6
# Resultados por tipo (a paleta mostra poucos; "ver todos" leva à lista do módulo).
LIMITE_DEFAULT = 5
LIMITE_MAX = 25

# Resultado da deteção de `unaccent`, por engine. None = ainda não testado.
_unaccent_disponivel: dict[str, bool] = {}


def limite_efetivo(pedido: int) -> int:
    """Limite pedido normalizado para o intervalo aceite."""
    if pedido <= 0:
        return LIMITE_DEFAULT
    return min(pedido, LIMITE_MAX)


def query_curta(q: str) -> bool:
    """True se a expressão é curta demais para valer a pena pesquisar."""
    return len(q.strip()) < Q_MIN


def termos(q: str) -> tuple[str, ...]:
    """
    Expressão do utilizador → os termos que têm de casar TODOS.

    A ordem por que foram escritos não conta: quem escreve "ciber testes" e quem
    escreve "testes ciber" procura a mesma coisa. Termos de um só caractere são
    ruído e ficam de fora; se não sobrar nenhum, vale a expressão inteira, para
    que uma pesquisa como "a b" continue a procurar literalmente "a b" em vez de
    ficar sem condição nenhuma — e devolver tudo.
    """
    escolhidos: list[str] = []
    for bruto in q.split():
        termo = bruto.lower()
        if len(termo) >= Q_MIN and termo not in escolhidos:
            escolhidos.append(termo)
    if not escolhidos:
        return (q.strip().lower(),)
    return tuple(escolhidos[:Q_MAX_TERMOS])


def _tem_unaccent(db: Session) -> bool:
    """
    Deteta (uma vez por engine) se a extensão `unaccent` está instalada.

    A migração tenta criá-la mas pode não ter privilégio; e em SQLite (testes)
    não existe de todo. Sem ela, cai-se para correspondência sensível a acentos
    em vez de rebentar a pesquisa inteira.
    """
    chave = str(db.get_bind().engine.url)
    if chave in _unaccent_disponivel:
        return _unaccent_disponivel[chave]
    try:
        from sqlalchemy import text

        existe = db.exec(
            text("SELECT 1 FROM pg_extension WHERE extname = 'unaccent'")
        ).first() is not None
    except Exception:  # noqa: BLE001 — SQLite ou sem permissões: degrada
        existe = False
    _unaccent_disponivel[chave] = existe
    return existe


def _cond(coluna, padrao: str, usar_unaccent: bool):
    """Condição de correspondência para uma coluna de texto."""
    if usar_unaccent:
        return func.unaccent(coluna).ilike(func.unaccent(padrao))
    return coluna.ilike(padrao)


def _texto(*colunas):
    """As colunas de texto de uma entidade numa só expressão.

    Juntá-las antes de comparar deixa os termos casar em colunas DIFERENTES da
    mesma linha — um no título, outro na descrição —, que é o que quem pesquisa
    espera. O `coalesce` é obrigatório: concatenar com NULL dá NULL.
    """
    junto = func.coalesce(colunas[0], "")
    for coluna in colunas[1:]:
        junto = junto + " " + func.coalesce(coluna, "")
    return junto


def _todos(alvo, c: "Contexto"):
    """Condição: todos os termos aparecem no alvo, em qualquer ordem."""
    return and_(*[_cond(alvo, f"%{t}%", c.unaccent) for t in c.termos])


def _relevancia(identificador, titulo, c: "Contexto"):
    """
    Ordem de relevância, em escalões.

    Pesa mais do que parece: como cada consulta trunca no limite, é isto que
    decide QUAIS resultados chegam a existir, e não apenas por que ordem
    aparecem. Sem escalões, uma expressão de vários termos deixava a ordem
    entregue ao critério de desempate e o melhor resultado podia ficar de fora.
    """
    return case(
        # Começa pela expressão inteira — para controlos é o código ("GR.FR"
        # devolve GR.FR-3 primeiro), para os restantes é o próprio título.
        (_cond(identificador, c.comeca, c.unaccent), 4),
        # A expressão inteira aparece seguida no título.
        (_cond(titulo, c.contem, c.unaccent), 3),
        # Todos os termos no título, dispersos.
        (_todos(titulo, c), 2),
        # Casou só graças aos outros campos (descrição, código).
        else_=1,
    ).desc()


# ── O que é pesquisável ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Contexto:
    """O que uma consulta precisa de saber, já normalizado uma vez por pedido."""

    empresa_id: object
    termos: tuple[str, ...]  # todos têm de casar, em qualquer ordem
    contem: str        # padrão "%q%" — a expressão INTEIRA, para a relevância
    comeca: str        # padrão "q%"  — idem
    unaccent: bool
    limite: int
    # Preenchido quando o âmbito do utilizador o limita ao que lhe está
    # atribuído; None = alcança tudo. A consulta usa-o na cláusula de dono.
    dono_id: object | None
    # Só o catálogo de controlos é traduzido em tabela própria; as restantes
    # entidades são texto da empresa e não têm versão por idioma.
    locale: str = "pt"


@dataclass(frozen=True)
class Pesquisavel:
    """Uma entidade que a pesquisa pode devolver.

    `modulo` é a célula da matriz que autoriza este tipo — é obrigatório e é o
    que faz a autorização ser automática. `consulta` a None significa que os
    resultados vêm do sidecar premium: o core não os produz, mas decide-os pelo
    mesmo critério.
    """

    tipo: str
    modulo: str
    consulta: Callable[[Session, Contexto], list[ResultadoPesquisaSchema]] | None = None

    @property
    def remoto(self) -> bool:
        return self.consulta is None


def _rotulo(valor) -> str:
    """Enum ou texto → o código que o frontend traduz."""
    return getattr(valor, "value", str(valor)) if valor is not None else ""


def _q_controlos(db: Session, c: Contexto) -> list[ResultadoPesquisaSchema]:
    """Controlos do catálogo do framework, restritos aos da empresa."""
    filtros = [
        ControloEmpresaV2.empresa_id == c.empresa_id,
        ControlLocale.locale == c.locale,
        _todos(_texto(Control.code, ControlLocale.title, ControlLocale.description), c),
    ]
    if c.dono_id is not None:
        filtros.append(ControloEmpresaV2.implementador_id == c.dono_id)

    linhas = db.exec(
        select(ControloEmpresaV2.id, Control.code, ControlLocale.title)
        .join(Control, Control.id == ControloEmpresaV2.control_id)
        .join(ControlLocale, ControlLocale.control_id == Control.id)
        .where(*filtros)
        .order_by(_relevancia(Control.code, ControlLocale.title, c), Control.code)
        .limit(c.limite)
    ).all()
    return [
        ResultadoPesquisaSchema(tipo="controlo", id=str(ce_id), titulo=title, subtitulo=code)
        for ce_id, code, title in linhas
    ]


def _q_incidentes(db: Session, c: Contexto) -> list[ResultadoPesquisaSchema]:
    filtros = [
        Incidente.empresa_id == c.empresa_id,
        Incidente.deleted_at.is_(None),  # type: ignore[union-attr]
        _todos(_texto(Incidente.titulo, Incidente.descricao), c),
    ]
    if c.dono_id is not None:
        filtros.append(Incidente.responsavel_id == c.dono_id)

    return [
        ResultadoPesquisaSchema(
            tipo="incidente", id=str(i.id), titulo=i.titulo, subtitulo=_rotulo(i.estado)
        )
        for i in db.exec(
            select(Incidente)
            .where(*filtros)
            .order_by(
                _relevancia(Incidente.titulo, Incidente.titulo, c),
                Incidente.conhecido_at.desc(),
            )
            .limit(c.limite)
        ).all()
    ]


def _q_tarefas(db: Session, c: Contexto) -> list[ResultadoPesquisaSchema]:
    filtros = [
        Tarefa.empresa_id == c.empresa_id,
        Tarefa.deleted_at.is_(None),  # type: ignore[union-attr]
        _todos(_texto(Tarefa.titulo, Tarefa.descricao), c),
    ]
    if c.dono_id is not None:
        filtros.append(Tarefa.responsavel_id == c.dono_id)

    return [
        ResultadoPesquisaSchema(
            tipo="tarefa", id=str(t.id), titulo=t.titulo, subtitulo=_rotulo(t.tipo)
        )
        for t in db.exec(
            select(Tarefa)
            .where(*filtros)
            .order_by(
                _relevancia(Tarefa.titulo, Tarefa.titulo, c), Tarefa.proximo_prazo
            )
            .limit(c.limite)
        ).all()
    ]


def _q_evidencias(db: Session, c: Contexto) -> list[ResultadoPesquisaSchema]:
    """Evidências pelo título (o conteúdo pode estar cifrado, não se pesquisa).

    O âmbito segue o dono do CONTROLO a que a evidência pertence — o mesmo
    critério que o serviço de evidências aplica ao abrir uma.
    """
    consulta = select(Evidencia).where(
        Evidencia.empresa_id == c.empresa_id,
        Evidencia.deleted_at.is_(None),  # type: ignore[union-attr]
        Evidencia.titulo.is_not(None),  # type: ignore[union-attr]
        _todos(_texto(Evidencia.titulo), c),
    )
    if c.dono_id is not None:
        # O âmbito de quem só alcança o que lhe está atribuído resolve-se pelas
        # LIGAÇÕES: a prova é alcançável se QUALQUER controlo ligado for
        # dele. Pela coluna antiga, uma evidência ligada ao controlo dele mas
        # carregada noutro sítio desaparecia da pesquisa.
        #
        # `EXISTS` e não `JOIN`: uma prova ligada a dois controlos do mesmo
        # implementador apareceria duas vezes com um join, e o `DISTINCT` que
        # corrigiria isso é **incompatível com a ordenação por relevância** —
        # o Postgres exige que as expressões do `ORDER BY` estejam na lista do
        # `SELECT`, e a daqui é um `CASE` de escalões. Medido: o join com
        # `DISTINCT` devolvia 500 ao implementador. O `EXISTS` não multiplica
        # linhas, portanto não precisa de `DISTINCT` e deixa a ordenação intacta.
        consulta = consulta.where(
            select(EvidenciaRequisito.id)
            .join(
                ControloEmpresaV2,
                ControloEmpresaV2.id == EvidenciaRequisito.requisito_id,
            )
            .where(
                EvidenciaRequisito.evidencia_id == Evidencia.id,
                EvidenciaRequisito.desligado_em.is_(None),  # type: ignore[union-attr]
                ControloEmpresaV2.implementador_id == c.dono_id,
            )
            .exists()
        )

    return [
        ResultadoPesquisaSchema(
            tipo="evidencia", id=str(e.id), titulo=e.titulo or "", subtitulo=_rotulo(e.tipo)
        )
        for e in db.exec(
            consulta.order_by(
                _relevancia(Evidencia.titulo, Evidencia.titulo, c),
                Evidencia.created_at.desc(),
            ).limit(c.limite)
        ).all()
    ]


def _q_formacao(db: Session, c: Contexto) -> list[ResultadoPesquisaSchema]:
    filtros = [
        AcaoFormacao.empresa_id == c.empresa_id,
        AcaoFormacao.deleted_at.is_(None),  # type: ignore[union-attr]
        _todos(_texto(AcaoFormacao.titulo, AcaoFormacao.descricao), c),
    ]
    if c.dono_id is not None:
        filtros.append(AcaoFormacao.responsavel_id == c.dono_id)

    return [
        ResultadoPesquisaSchema(
            tipo="formacao", id=str(a.id), titulo=a.titulo, subtitulo=_rotulo(a.estado)
        )
        for a in db.exec(
            select(AcaoFormacao)
            .where(*filtros)
            .order_by(
                _relevancia(AcaoFormacao.titulo, AcaoFormacao.titulo, c),
                AcaoFormacao.data.desc(),
            )
            .limit(c.limite)
        ).all()
    ]


# A tabela. Uma linha por tipo que a pesquisa pode devolver — do core e do
# sidecar. A ordem é a de apresentação.
PESQUISAVEIS: tuple[Pesquisavel, ...] = (
    Pesquisavel("controlo", "controlos", _q_controlos),
    Pesquisavel("incidente", "incidentes", _q_incidentes),
    Pesquisavel("tarefa", "tarefas", _q_tarefas),
    Pesquisavel("evidencia", "evidencias", _q_evidencias),
    Pesquisavel("formacao", "formacao", _q_formacao),
    # Vêm do sidecar premium: o core não os produz, mas decide quem os vê.
    Pesquisavel("ativo", "inventario"),
    Pesquisavel("risco", "risco"),
    Pesquisavel("fornecedor", "fornecedores"),
)


def tipos_permitidos(utilizador) -> dict[str, Pesquisavel]:
    """
    Os tipos de resultado que este utilizador pode ver.

    Resposta ÚNICA à pergunta "o que é que esta pessoa alcança na pesquisa",
    usada tanto para os resultados do core como para filtrar os do sidecar. Se
    fossem duas listas, uma delas envelhecia.
    """
    return {
        p.tipo: p
        for p in PESQUISAVEIS
        if tem_capacidade(utilizador, p.modulo, ClasseAcao.VER)
    }


def pesquisar_core(
    db: Session,
    utilizador,
    q: str,
    locale: str = "pt",
    limite: int = 0,
) -> list[ResultadoPesquisaSchema]:
    """Pesquisa nas entidades do core. Devolve [] se a expressão for curta.

    Recebe o UTILIZADOR e não a empresa: dele saem a empresa, a capacidade e o
    âmbito, e não há forma de esquecer um dos três.
    """
    q = q.strip()
    if query_curta(q):
        return []

    permitidos = tipos_permitidos(utilizador)
    unaccent = _tem_unaccent(db)
    n = limite_efetivo(limite)
    # Partida uma vez por pedido, não por entidade.
    termos_q = termos(q)

    out: list[ResultadoPesquisaSchema] = []
    for p in PESQUISAVEIS:
        if p.remoto or p.tipo not in permitidos:
            continue
        contexto = Contexto(
            empresa_id=utilizador.empresa_id,
            termos=termos_q,
            contem=f"%{q}%",
            comeca=f"{q}%",
            unaccent=unaccent,
            limite=n,
            # Onde a matriz limita ao que lhe está atribuído, a consulta filtra
            # pelo dono — o mesmo critério da listagem do módulo. Sem isto, a
            # pesquisa mostrava o que o ecrã do módulo esconde.
            dono_id=(
                utilizador.id
                if so_atribuidos(utilizador, p.modulo, ClasseAcao.VER)
                else None
            ),
            locale=locale,
        )
        out.extend(p.consulta(db, contexto))
    return out
