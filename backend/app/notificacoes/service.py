"""
Lógica de negócio do módulo de notificações.

Quem produz avisos chama `criar_notificacao` com um código do catálogo e os
valores que entram na frase. A deduplicação e a substituição de avisos obsoletos
são decididas pelo catálogo, não por cada produtor: antes desta versão, cada um
repetia a sua própria consulta de "já existe uma igual por ler?", e um esqueceu-se
de incluir o estado do prazo na chave — o que fazia um aviso de "prazo a expirar"
sobreviver intacto depois de o prazo já ter passado.

Os filtros da listagem, das contagens e da marcação em lote saem todos da mesma
função. É isso que faz um contador significar exatamente o conjunto que a tabela
mostra.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Sequence

from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.auth.models import Utilizador
from app.notificacoes.catalogo import DefinicaoNotificacao, chave, definicao
from app.notificacoes.models import Notificacao
from app.shared.pii import cifrar_pii

logger = logging.getLogger(__name__)

Estado = Literal["nao_lidas", "lidas", "todas"]


def _agora() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Produção
# ---------------------------------------------------------------------------


def _cifrar_params(
    d: DefinicaoNotificacao, params: dict[str, Any] | None
) -> dict[str, Any] | None:
    """
    Cifra as chaves de `params` que o catálogo marca como dados pessoais.

    O nome de quem auditou e o que ele escreveu já são guardados cifrados no
    relatório de auditoria; uma notificação sobre o mesmo facto não pode ser a
    cópia em claro dos mesmos dados. O que fica de fora — o código do controlo,
    o título da tarefa — é o que a pesquisa precisa de ver, e não identifica
    ninguém.
    """
    if not params or not d.params_pii:
        return params

    seguros = dict(params)
    for nome in d.params_pii:
        valor = seguros.get(nome)
        if not valor:
            continue
        seguros[nome] = cifrar_pii(str(valor))
    return seguros


def criar_notificacao(
    db: Session,
    *,
    empresa_id: uuid.UUID,
    utilizador_id: uuid.UUID,
    codigo: str,
    params: dict[str, Any] | None = None,
    entidade_id: uuid.UUID | None = None,
    controlo_empresa_id: uuid.UUID | None = None,
    dedup_partes: Sequence[object] = (),
) -> Notificacao | None:
    """
    Cria uma notificação para o utilizador indicado.

    Devolve `None` quando o catálogo diz que este código deduplica e já existe
    uma por ler com a mesma chave — não é um erro, é o caso normal de um tick
    que volta a correr sobre um facto que não mudou.

    Não faz commit: quem chama decide quando é que a transação fecha.
    """
    d = definicao(codigo)
    chave_dedup = chave(codigo, *dedup_partes)

    if d.dedup:
        ja_existe = db.exec(
            select(Notificacao.id).where(
                Notificacao.utilizador_id == utilizador_id,
                Notificacao.chave_dedup == chave_dedup,
                Notificacao.lida.is_(False),  # type: ignore[union-attr]
            )
        ).first()
        if ja_existe:
            return None

    params_gravar = _cifrar_params(d, params)
    notif = Notificacao(
        empresa_id=empresa_id,
        utilizador_id=utilizador_id,
        codigo=codigo,
        categoria=d.categoria,
        severidade=d.severidade,
        chave_dedup=chave_dedup,
        params=json.dumps(params_gravar, ensure_ascii=False) if params_gravar else None,
        entidade_tipo=d.entidade_tipo,
        entidade_id=entidade_id,
        controlo_empresa_id=controlo_empresa_id,
        acionavel=d.acionavel,
    )
    db.add(notif)

    # Avisos que este torna obsoletos: a chave é a mesma, muda só o código, por
    # isso a correspondência é exata e não apanha outro marco do mesmo incidente.
    for codigo_antigo in d.substitui:
        _marcar_lidas_por_chave(
            db,
            utilizador_id=utilizador_id,
            chave_dedup=chave(codigo_antigo, *dedup_partes),
        )

    return notif


def _marcar_lidas_por_chave(
    db: Session, *, utilizador_id: uuid.UUID, chave_dedup: str
) -> int:
    notifs = db.exec(
        select(Notificacao).where(
            Notificacao.utilizador_id == utilizador_id,
            Notificacao.chave_dedup == chave_dedup,
            _por_resolver(),
        )
    ).all()
    return dar_por_resolvidas(db, notifs)


# ---------------------------------------------------------------------------
# Filtros — os mesmos para listar, contar e marcar em lote
# ---------------------------------------------------------------------------


def _criterios(
    utilizador: Utilizador,
    *,
    estado: Estado = "nao_lidas",
    categoria: str | None = None,
    severidade: str | None = None,
    q: str | None = None,
    data_inicio: datetime | None = None,
    data_fim: datetime | None = None,
) -> list:
    """
    Constrói a cláusula de filtro.

    O `empresa_id` é redundante com o `utilizador_id` enquanto um utilizador
    pertencer a uma só empresa. Está cá na mesma: é o que garante que, se algum
    dia uma conta mudar de empresa, ela não passa a ver o que lhe foi dito na
    anterior.
    """
    criterios: list = [
        Notificacao.utilizador_id == utilizador.id,
        Notificacao.empresa_id == utilizador.empresa_id,
    ]

    if estado == "nao_lidas":
        criterios.append(Notificacao.lida.is_(False))  # type: ignore[union-attr]
    elif estado == "lidas":
        criterios.append(Notificacao.lida.is_(True))  # type: ignore[union-attr]

    if categoria:
        criterios.append(Notificacao.categoria == categoria)
    if severidade:
        criterios.append(Notificacao.severidade == severidade)
    if data_inicio is not None:
        criterios.append(Notificacao.created_at >= data_inicio)
    if data_fim is not None:
        criterios.append(Notificacao.created_at <= data_fim)
    if q:
        # A pesquisa cobre os valores que entram na frase (o título do incidente,
        # o código do controlo) e o texto congelado das linhas antigas. O código
        # do evento entra também: quem escreve "prazo" encontra os prazos.
        # Os valores cifrados ficam de fora na prática — o criptograma não
        # corresponde ao que se escreveu. É o preço de não guardar o nome de uma
        # pessoa em claro, e a pesquisa continua a chegar lá pelo controlo.
        termo = f"%{q.strip()}%"
        criterios.append(
            func.coalesce(Notificacao.params, "").ilike(termo)
            | func.coalesce(Notificacao.mensagem, "").ilike(termo)
            | func.coalesce(Notificacao.titulo, "").ilike(termo)
            | func.coalesce(Notificacao.codigo, "").ilike(termo)
        )

    return criterios


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------


def listar(
    db: Session,
    utilizador: Utilizador,
    *,
    estado: Estado = "nao_lidas",
    categoria: str | None = None,
    severidade: str | None = None,
    q: str | None = None,
    data_inicio: datetime | None = None,
    data_fim: datetime | None = None,
    limite: int = 20,
    offset: int = 0,
) -> tuple[int, list[Notificacao]]:
    """Página de notificações e o total do conjunto filtrado."""
    criterios = _criterios(
        utilizador,
        estado=estado,
        categoria=categoria,
        severidade=severidade,
        q=q,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )

    total: int = db.exec(
        select(func.count()).select_from(Notificacao).where(*criterios)
    ).one()

    # Desempate por id: um tick escreve várias linhas no mesmo instante, e sem
    # segunda coluna a ordenação não é determinística — a paginação repetiria
    # umas e saltaria outras.
    notifs = list(
        db.exec(
            select(Notificacao)
            .where(*criterios)
            .order_by(
                Notificacao.created_at.desc(),  # type: ignore[union-attr]
                Notificacao.id.desc(),  # type: ignore[union-attr]
            )
            .offset(offset)
            .limit(limite)
        ).all()
    )
    return total, notifs


def contar_nao_lidas(db: Session, utilizador: Utilizador) -> int:
    """Contagem do sininho. É o pedido mais frequente da aplicação inteira."""
    return db.exec(
        select(func.count())
        .select_from(Notificacao)
        .where(*_criterios(utilizador, estado="nao_lidas"))
    ).one()


def resumo(
    db: Session,
    utilizador: Utilizador,
    *,
    estado: Estado = "nao_lidas",
    categoria: str | None = None,
    severidade: str | None = None,
    q: str | None = None,
    data_inicio: datetime | None = None,
    data_fim: datetime | None = None,
) -> dict[str, Any]:
    """
    Contagens sobre EXATAMENTE o mesmo conjunto que a listagem devolve com os
    mesmos parâmetros.

    A repartição por categoria e por severidade sai de uma única agregação: o
    número de combinações é pequeno e limitado pelo catálogo, por isso somar em
    Python fica mais barato do que percorrer a tabela duas vezes.
    """
    criterios = _criterios(
        utilizador,
        estado=estado,
        categoria=categoria,
        severidade=severidade,
        q=q,
        data_inicio=data_inicio,
        data_fim=data_fim,
    )

    linhas = db.exec(
        select(Notificacao.categoria, Notificacao.severidade, func.count())
        .where(*criterios)
        .group_by(Notificacao.categoria, Notificacao.severidade)  # type: ignore[arg-type]
    ).all()

    total = 0
    por_categoria: dict[str, int] = {}
    por_severidade: dict[str, int] = {}
    for cat, sev, n in linhas:
        n = int(n)
        total += n
        por_categoria[cat] = por_categoria.get(cat, 0) + n
        por_severidade[sev] = por_severidade.get(sev, 0) + n

    return {
        "total": total,
        # Independente dos filtros: é o número do sininho, e tem de ser o mesmo
        # esteja o utilizador a ver que separador estiver.
        "nao_lidas": contar_nao_lidas(db, utilizador),
        "por_categoria": por_categoria,
        "por_severidade": por_severidade,
    }


def controlos_com_notificacoes(
    db: Session, utilizador: Utilizador
) -> list[uuid.UUID]:
    """Controlos com avisos por ler — alimenta os marcadores na lista."""
    return [
        ce_id
        for ce_id in db.exec(
            select(Notificacao.controlo_empresa_id)
            .where(
                *_criterios(utilizador, estado="nao_lidas"),
                Notificacao.controlo_empresa_id.is_not(None),  # type: ignore[union-attr]
            )
            .distinct()
        ).all()
        if ce_id is not None
    ]


# ---------------------------------------------------------------------------
# Marcação
# ---------------------------------------------------------------------------


def marcar_lida(
    db: Session, notificacao_id: uuid.UUID, utilizador: Utilizador, *, lida: bool = True
) -> bool:
    """Marca (ou desmarca) uma notificação. Devolve True se encontrada."""
    notif = db.exec(
        select(Notificacao).where(
            Notificacao.id == notificacao_id,
            Notificacao.utilizador_id == utilizador.id,
            Notificacao.empresa_id == utilizador.empresa_id,
        )
    ).first()
    if not notif:
        return False
    notif.lida = lida
    notif.lida_at = _agora() if lida else None
    db.add(notif)
    return True


def marcar_lidas_por_entidade(
    db: Session,
    *,
    entidade_tipo: str,
    entidade_id: uuid.UUID,
    utilizador: Utilizador,
) -> int:
    """
    Dispensa os avisos INFORMATIVOS desta entidade ao visitá-la.

    Os acionáveis ficam. Visitar o ecrã não é tratar do assunto: um prazo legal
    ultrapassado tem de continuar a aparecer enquanto estiver ultrapassado, e
    quem o quiser tirar da lista tem de o dizer de propósito.
    """
    notifs = db.exec(
        select(Notificacao).where(
            *_criterios(utilizador, estado="nao_lidas"),
            Notificacao.entidade_tipo == entidade_tipo,
            Notificacao.entidade_id == entidade_id,
            Notificacao.acionavel.is_(False),  # type: ignore[union-attr]
        )
    ).all()
    agora = _agora()
    for n in notifs:
        n.lida = True
        n.lida_at = agora
        db.add(n)
    return len(notifs)


def marcar_lidas_em_lote(
    db: Session,
    utilizador: Utilizador,
    *,
    categoria: str | None = None,
    severidade: str | None = None,
    q: str | None = None,
    data_inicio: datetime | None = None,
    data_fim: datetime | None = None,
) -> int:
    """
    Marca como lidas as não lidas que correspondem aos filtros.

    Os filtros são os mesmos da listagem de propósito: quem carrega no botão
    enquanto vê um separador está a dispensar o que está a ver, não a esvaziar
    a caixa inteira sem reparar.
    """
    notifs = db.exec(
        select(Notificacao).where(
            *_criterios(
                utilizador,
                estado="nao_lidas",
                categoria=categoria,
                severidade=severidade,
                q=q,
                data_inicio=data_inicio,
                data_fim=data_fim,
            )
        )
    ).all()
    agora = _agora()
    for n in notifs:
        n.lida = True
        n.lida_at = agora
        db.add(n)
    return len(notifs)


def marcar_lidas_por_chave_prefixo(
    db: Session, *, prefixo: str, empresa_id: uuid.UUID
) -> int:
    """
    Dispensa os avisos de um facto que deixou de ser verdade.

    Usada quando o objeto de origem muda de estado — uma tarefa concluída muda
    de prazo, e os avisos do prazo anterior deixam de descrever a realidade.
    Restringida à empresa: é a única forma de garantir que o âmbito não depende
    de o identificador na chave ser irrepetível.
    """
    notifs = db.exec(
        select(Notificacao).where(
            Notificacao.empresa_id == empresa_id,
            Notificacao.chave_dedup.like(f"{prefixo}%"),  # type: ignore[union-attr]
            _por_resolver(),
        )
    ).all()
    return dar_por_resolvidas(db, notifs)


def pendentes_da_entidade(
    db: Session, *, empresa_id: uuid.UUID, entidade_id: uuid.UUID, codigos: Sequence[str]
) -> list[Notificacao]:
    """
    Os avisos ainda por resolver de uma entidade, nos códigos indicados.

    Para quem precisa de decidir aviso a aviso se o facto ainda é verdade — um
    prazo que mudou de data deixa de o ser, mesmo com o mesmo marco.
    """
    return list(db.exec(
        select(Notificacao).where(
            Notificacao.empresa_id == empresa_id,
            Notificacao.entidade_id == entidade_id,
            Notificacao.codigo.in_(list(codigos)),  # type: ignore[union-attr]
            _por_resolver(),
        )
    ).all())


def _por_resolver():
    """Por ler, ou lida à mão mas ainda a descrever trabalho por fazer."""
    return or_(
        Notificacao.lida.is_(False),  # type: ignore[union-attr]
        Notificacao.acionavel.is_(True),  # type: ignore[union-attr]
    )


def dar_por_resolvidas(db: Session, notifs) -> int:
    """
    Fecha avisos cujo facto deixou de ser verdade (tratado ou substituído).

    Deixam também de ser acionáveis: o ecrã marca como "por tratar" o acionável
    que já foi lido, e dizer isso de um prazo cumprido ou de uma tarefa concluída
    seria mentir. Quem os leu à mão antes mantém a data dessa leitura.
    """
    agora = _agora()
    for n in notifs:
        n.lida = True
        n.lida_at = n.lida_at or agora
        n.acionavel = False
        db.add(n)
    return len(notifs)


# ---------------------------------------------------------------------------
# Retenção
# ---------------------------------------------------------------------------


def purgar_lidas_antigas(db: Session, *, dias: int, lote: int = 5000) -> int:
    """
    Apaga notificações JÁ LIDAS mais antigas que a janela de retenção.

    Três condições cumulativas, todas necessárias:

    - `lida` — uma notificação por ler é trabalho por fazer e nunca se apaga,
      tenha a idade que tiver;
    - `lida_at` preenchido — as que foram lidas antes de essa data passar a ser
      registada não têm como ser datadas, e apagá-las seria adivinhar;
    - fora da janela.

    O apagamento é definitivo e é seguro que o seja: uma notificação é um
    lembrete, não um registo probatório. O facto que lhe deu origem continua na
    trilha de auditoria e no módulo de onde veio, cada um com a sua retenção.

    Corre por lotes: numa instalação com anos de histórico, uma só instrução
    seguraria um lock longo sobre a tabela que o sininho consulta a toda a hora.
    """
    from app.shared.audit import Acao, registar_acao

    corte = _agora() - timedelta(days=dias)
    notifs = list(
        db.exec(
            select(Notificacao)
            .where(
                Notificacao.lida.is_(True),  # type: ignore[union-attr]
                Notificacao.lida_at.is_not(None),  # type: ignore[union-attr]
                Notificacao.lida_at < corte,
            )
            .limit(lote)
        ).all()
    )
    if not notifs:
        return 0

    por_empresa: dict[uuid.UUID, int] = {}
    for n in notifs:
        por_empresa[n.empresa_id] = por_empresa.get(n.empresa_id, 0) + 1
        db.delete(n)

    # Um apagamento silencioso é exatamente o que um auditor não quer encontrar,
    # mesmo tratando-se de lembretes.
    for empresa_id, quantas in por_empresa.items():
        registar_acao(
            db,
            acao=Acao.NOTIFICACOES_PURGADAS,
            empresa_id=empresa_id,
            entidade_tipo="Notificacao",
            dados_novos={"linhas": quantas, "retencao_dias": dias},
        )

    return len(notifs)
