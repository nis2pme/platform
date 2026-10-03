"""Encadeamento por hash do registo de auditoria.

Até aqui a tabela era imutável **por convenção**: nada impedia um `UPDATE` de
reescrever uma linha sem deixar rasto. Numa aplicação cujo produto é prova, isso
é uma lacuna de posicionamento, não só técnica.

Cada linha passa a levar o SHA-256 do seu próprio conteúdo (`hash_registo`)
calculado **sobre o hash da linha anterior da mesma empresa** (`hash_anterior`).
Alterar a linha N obriga a recalcular de N até ao fim: a adulteração deixa de ser
silenciosa e passa a ser detetável por quem tenha o *head* da cadeia.

O que isto **não** prova sozinho: quem manda na base pode reescrever a cadeia
inteira. A cadeia só ganha força quando o ***head* sai da máquina** — no dossiê
assinado e no backup diário. Aí, reescrever a história obriga a falsificar todas
os *head*s já exportados.

Não é blockchain, e a diferença é deliberada: a primitiva é a mesma, mas o
consenso distribuído existe para quando não há ninguém em quem confiar. Aqui há o
servidor; consenso seria complexidade e lentidão a troco de nada.

## Uma cadeia por empresa

Nunca uma cadeia global: encadear todos os tenants na mesma sequência obrigaria
cada escrita de qualquer empresa a esperar pela de todas as outras, e daria a uma
empresa a capacidade de atrasar as restantes. As ações sem empresa (plataforma,
autenticação antes de se saber o tenant) têm cadeia própria, sob um identificador
reservado — ver `CADEIA_PLATAFORMA`.

## Ordem, e quando se encadeia

A cadeia só é verificável se as escritas de uma empresa forem serializadas: duas
a lerem o mesmo *head* ao mesmo tempo produziriam duas linhas a apontar para o
mesmo anterior, e uma delas ficaria fora da cadeia. A serialização é feita com um
**bloqueio da linha de *head*** (`SELECT ... FOR UPDATE`), que o Postgres segura
até ao fim da transação. Em SQLite — onde correm os testes — a escrita já é
serializada pela própria base, e o `FOR UPDATE` é ignorado sem prejuízo.

Porque o bloqueio dura até ao commit, **o momento em que se encadeia decide
quanto tempo ele é retido**. Encadear onde o código de negócio regista a ação
deixaria a linha trancada durante todo o resto do pedido — I/O de disco, gRPC,
geração de documentos —, e o débito de escritas auditadas de uma empresa passaria
a ser uma por duração de pedido. Por isso o caminho normal **acumula** as
entradas e encadeia-as todas em `encadear_lote`, imediatamente antes do commit.
"""
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Field, Session, SQLModel

logger = logging.getLogger(__name__)

# Identificador reservado para a cadeia das ações sem empresa. Não é uma empresa
# real e nunca colide com uma: o UUID nulo não é gerado por `uuid4()`.
CADEIA_PLATAFORMA = uuid.UUID("00000000-0000-0000-0000-000000000000")

# Quanto tempo uma escrita em sessão própria (`force_commit`) espera pelo *head*
# antes de desistir de encadear. Curto de propósito: quem chega aqui está quase
# sempre dentro de um pedido que já tem o *head* bloqueado, e nesse caso esperar
# mais não resolve — o detentor só larga quando o pedido acabar, e o pedido está
# à espera desta escrita.
ESPERA_SESSAO_PROPRIA_MS = 2000

# E na transação normal do pedido. Sem limite, quem espera pelo *head* espera o
# que o detentor demorar — **e o detentor pode nunca acabar**: medido numa onda
# de testes, um pedido cujo cliente desistiu deixou a transação aberta a segurar
# o *head*, e o pedido seguinte da mesma empresa esperou 7 minutos até o stack
# deixar de responder. A cadeia é um ponto de serialização por empresa: sem teto,
# um pedido preso congela todas as escritas auditadas do tenant.
#
# 5 s é muito acima de qualquer disputa legítima (o *head* segura-se durante uma
# transação de pedido, não durante trabalho longo) e muito abaixo do tempo em que
# alguém repara que a aplicação parou.
ESPERA_PEDIDO_MS = 5000

# Valor de `hash_anterior` da primeira linha de uma cadeia. Uma cadeia que
# começasse com NULL seria indistinguível de uma cadeia truncada pela frente.
GENESE = "0" * 64


class AuditHashChainHead(SQLModel, table=True):
    """*Head* da cadeia de cada empresa: o último hash escrito e quantos são.

    Existe uma linha por empresa (mais a da plataforma). É esta linha que se
    bloqueia para serializar as escritas, e é o `head_hash` que se exporta no
    dossiê e no backup.

    O `sequencia` não é usado para verificar — a verificação segue os `hash_*` —
    mas torna evidente um truncamento: uma cadeia com 900 linhas e o *head* a
    dizer 1200 denuncia 300 apagadas mesmo que o resto encaixe.
    """

    __tablename__ = "audit_hash_chain_head"

    empresa_id: uuid.UUID = Field(primary_key=True)
    head_hash: str = Field(max_length=64)
    sequencia: int = Field(default=0)


# Campos cujo valor é um UUID, e campos que são instantes. A normalização é feita
# **por campo declarado** e não por adivinhação do tipo em mão: o driver não
# devolve os mesmos tipos de Python com que a linha foi escrita, e um hash que
# dependa disso é um hash que nunca mais bate depois de a linha ser relida.
_CAMPOS_UUID = frozenset({"id", "empresa_id", "utilizador_id", "entidade_id"})
_CAMPOS_INSTANTE = frozenset({"created_at"})


def _normalizar(campo: str, valor: Any) -> Any:
    """Reduz um valor à forma canónica que entra no hash.

    Os dois casos que obrigam a isto, ambos apanhados por teste:

    - **Instantes.** O SQLite devolve datas *sem* fuso; o valor escrito trazia
      UTC. `isoformat()` dá textos diferentes para o mesmo instante, e o registo
      passava a acusar adulteração só por ter sido relido. Uma data sem fuso
      lê-se como UTC — é o que a aplicação escreve em todo o lado — e sai sempre
      no mesmo formato.
    - **UUIDs.** Conforme o driver, chegam como `uuid.UUID` ou como texto. Passam
      todos pelo construtor, que normaliza maiúsculas e separadores.
    """
    if valor is None:
        return None
    if campo in _CAMPOS_UUID:
        return str(valor if isinstance(valor, uuid.UUID) else uuid.UUID(str(valor)))
    if campo in _CAMPOS_INSTANTE:
        momento = valor
        if momento.tzinfo is None:
            momento = momento.replace(tzinfo=timezone.utc)
        return momento.astimezone(timezone.utc).isoformat()
    if hasattr(valor, "value"):  # Enum
        return valor.value
    return valor


# Campos que entram no hash, por ordem fixa. A ordem faz parte do formato: mudá-la
# invalida todas as cadeias existentes. Acrescentar um campo ao fim também — por
# isso qualquer alteração aqui exige uma versão nova do formato, não um remendo.
#
# Note-se `dados_hash` no lugar de `dados_anteriores`/`dados_novos`: ver
# `impressao_dos_dados`.
CAMPOS_ASSINADOS: tuple[str, ...] = (
    "id",
    "empresa_id",
    "utilizador_id",
    "acao",
    "entidade_tipo",
    "entidade_id",
    "dados_hash",
    "ip_hash",
    "resultado",
    "created_at",
)


def impressao_dos_dados(dados_anteriores: str | None, dados_novos: str | None) -> str:
    """Compromisso com o conteúdo dos dados, em vez do conteúdo em si.

    O que motiva isto é o arquivo. `linha_para_arquivo` **mascara** os dois
    campos antes de os escrever no `.jsonl.gz` — são JSON em claro e transportam
    nome e email, num ficheiro que nunca mais é reescrito e que uma anonimização
    posterior nunca alcançaria. Se o hash do registo fosse calculado sobre o
    texto dos dados, mascará-los partiria a cadeia no exato momento da purga, e
    toda a história arquivada passaria a acusar adulteração que não houve.

    Guardando o **digest** e assinando-o, as duas exigências deixam de colidir: o
    conteúdo pode ser mascarado ou anonimizado depois, e a cadeia continua a
    verificar. O que se perde é poder reconstruir os dados a partir do hash — o
    que é a intenção, não um efeito secundário. Quem tiver o conteúdo original
    pode sempre provar que corresponde.
    """
    corpo = json.dumps(
        {"anteriores": dados_anteriores, "novos": dados_novos},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(corpo).hexdigest()


def impressao_do_registo(entrada: Any, hash_anterior: str) -> str:
    """SHA-256 do conteúdo de um registo, encadeado no anterior.

    O `ip_address` e o `user_agent` **não** entram: são criptogramas Fernet, que
    não são determinísticos — o mesmo IP dá bytes diferentes em cada linha, e o
    hash deixaria de ser reproduzível a partir da linha relida. O `ip_hash`, esse,
    entra: é determinístico e cobre a mesma informação para efeitos de prova. O
    User-Agent fica **fora** da cadeia por não ter equivalente determinístico —
    uma limitação conhecida, não um esquecimento: é o campo de menor valor
    probatório dos três e não justifica uma coluna de hash só para ele.

    A serialização é JSON canónico (chaves ordenadas, sem espaços, UTF-8 tal e
    qual) para que a mesma linha dê sempre os mesmos bytes.
    """
    corpo = {campo: _normalizar(campo, getattr(entrada, campo, None)) for campo in CAMPOS_ASSINADOS}
    corpo["hash_anterior"] = hash_anterior
    bytes_canonicos = json.dumps(
        corpo, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(bytes_canonicos).hexdigest()


class CadeiaOcupada(Exception):
    """O *head* da cadeia não ficou disponível dentro do tempo permitido."""


def _trancar_head(
    db: Session, chave: uuid.UUID, *, espera_ms: int | None = None
) -> AuditHashChainHead:
    """Devolve o *head* da cadeia, com a linha bloqueada até ao fim da transação.

    O bloqueio é o que serializa as escritas da mesma empresa. Sem ele, duas
    transações leem o mesmo *head* e a segunda linha nasce fora da cadeia.

    `espera_ms` limita quanto tempo se espera pelo bloqueio. Serve o caminho do
    `force_commit`, que corre numa **ligação própria**: se a transação principal
    do mesmo pedido já tiver o *head* bloqueado, esperar por ele seria esperar
    por quem está à espera desta — um impasse do pedido consigo mesmo. Sem
    limite, o Postgres espera **indefinidamente** (não há `lock_timeout` global
    nesta aplicação), e o sintoma seria um pedido pendurado sem erro nenhum, com
    a ligação retida até esgotar a pool. Um erro é mau; um bloqueio silencioso é
    pior.
    """
    def _com_limite():
        """Tenta obter o *head* sob limite de espera, sem arriscar a transação.

        O `SET LOCAL` e o `FOR UPDATE` vão dentro de um **savepoint**: no
        Postgres, um tempo de bloqueio esgotado aborta a transação, e nos pedidos
        normais essa transação é a do trabalho do utilizador. Sem o savepoint,
        proteger a cadeia custaria a operação que o utilizador pediu — o remédio
        seria pior. Com ele, desfaz-se só a tentativa.
        """
        with db.begin_nested():
            if db.bind is not None and db.bind.dialect.name == "postgresql":
                db.execute(text(f"SET LOCAL lock_timeout = '{int(espera_ms)}ms'"))
            return _selecionar()

    def _selecionar():
        # `with_for_update` só produz SQL em bases que o suportem; o SQLite dos
        # testes ignora-o e serializa na mesma, por ser um escritor de cada vez.
        return (
            db.query(AuditHashChainHead)
            .filter(AuditHashChainHead.empresa_id == chave)
            .with_for_update()
            .one_or_none()
        )

    try:
        head = _com_limite() if espera_ms is not None else _selecionar()
        if head is None:
            # Primeira ação desta empresa: a linha de *head* ainda não existe e
            # há que a criar. Duas ligações a fazê-lo ao mesmo tempo disputam a
            # chave primária — medido contra Postgres, e não é hipótese remota:
            # é o arranque de qualquer tenant novo.
            #
            # A criação vai dentro de um **savepoint**. Sem ele, a colisão aborta
            # a transação inteira no Postgres e tudo o que viesse a seguir
            # morria com "current transaction is aborted"; com ele, desfaz-se só
            # a tentativa e volta-se a selecionar, que é quando a linha da outra
            # transação aparece. É também a forma portável: em SQLite o mesmo
            # código funciona sem depender de `ON CONFLICT`.
            try:
                with db.begin_nested():
                    db.add(
                        AuditHashChainHead(
                            empresa_id=chave, head_hash=GENESE, sequencia=0
                        )
                    )
                    db.flush()
            except (IntegrityError, OperationalError):
                pass  # outra transação criou-a (ou segura-a) — reler resolve
            head = _selecionar()
            if head is None:
                # Criada por outra transação que ainda não confirmou. Sem o valor
                # dela não há como encadear — quem chama decide o que fazer.
                raise CadeiaOcupada("head criado por outra transação, ainda não visível")
    except OperationalError as erro:
        raise CadeiaOcupada(str(erro)) from erro
    return head


def encadear_lote(
    db: Session, entradas: list[Any], *, espera_ms: int | None = None
) -> int:
    """Encadeia várias entradas de uma vez — uma tranca por cadeia, não por linha.

    Chamado imediatamente antes do commit, com tudo o que o pedido produziu.

    A razão é o tempo de retenção. O `SELECT ... FOR UPDATE` sobre a linha de
    *head* só é largado no commit, portanto quem o tranca a meio do pedido
    tranca-a também durante tudo o que venha a seguir — escrever um ficheiro no
    disco, chamar o sidecar, gerar um PDF. Medido: um pedido segurou-a 0,9 s para
    10 ms de trabalho de cadeia, e um pedido abandonado pelo cliente segurou-a até
    o serviço deixar de responder. Encadeando aqui, a secção crítica passa a ser
    só o que está nesta função.

    As entradas são agrupadas por cadeia (uma por empresa, mais a da plataforma) e
    encadeadas pela ordem em que foram criadas — a ordem do registo é a ordem dos
    acontecimentos, não a ordem em que a base decidir escrever as linhas.

    Devolve quantas ficaram encadeadas. As restantes gravam-se sem elo: perder um
    registo de auditoria para preservar o carimbo dele seria trocar a prova pela
    etiqueta da prova.
    """
    por_cadeia: dict[uuid.UUID, list[Any]] = {}
    for entrada in entradas:
        entrada.dados_hash = impressao_dos_dados(
            entrada.dados_anteriores, entrada.dados_novos
        )
        por_cadeia.setdefault(
            entrada.empresa_id or CADEIA_PLATAFORMA, []
        ).append(entrada)

    encadeadas = 0
    for chave, lote in por_cadeia.items():
        try:
            head = _trancar_head(db, chave, espera_ms=espera_ms)
        except CadeiaOcupada:
            logger.warning(
                "Cadeia de auditoria ocupada para %s: %d registo(s) gravado(s) "
                "SEM elo (%s). A linha existe e é legível; só não entra na "
                "cadeia, e o verificador salta-a em vez de a acusar.",
                chave, len(lote), ", ".join(getattr(e, "acao", "?") for e in lote),
            )
            continue
        anterior = head.head_hash
        for entrada in lote:
            entrada.hash_anterior = anterior
            entrada.hash_registo = impressao_do_registo(entrada, anterior)
            anterior = entrada.hash_registo
        head.head_hash = anterior
        head.sequencia += len(lote)
        encadeadas += len(lote)
    return encadeadas


def encadear(db: Session, entrada: Any, *, espera_ms: int | None = None) -> bool:
    """Encadeia uma entrada só, no momento da chamada.

    Serve o caminho da **sessão própria** (`force_commit`), que escreve e confirma
    de imediato numa ligação à parte e por isso não tem commit futuro onde
    encadear em lote. O caminho normal usa `encadear_lote` antes do commit.

    Chamado com a entrada já construída e ainda **não** adicionada à sessão. O
    `created_at` tem de estar definido, porque entra no hash.

    Devolve `True` se encadeou. Devolve `False` — deixando a entrada **sem
    hashes** — quando o *head* não ficou disponível dentro de `espera_ms`.

    **Quem receber `False` tem de fazer `rollback()` antes de gravar.** No
    Postgres, o tempo esgotado num bloqueio aborta a transação, e a escrita
    seguinte falharia com *"current transaction is aborted"*. Feito isso, a linha
    grava-se na mesma.
    """
    return encadear_lote(db, [entrada], espera_ms=espera_ms) == 1


def head_de(db: Session, empresa_id: uuid.UUID | None) -> dict[str, Any]:
    """*Head* atual de uma cadeia, para exportar no dossiê e no backup.

    Uma empresa sem ações ainda não tem cadeia — devolve a génese com sequência
    zero, que é a verdade, em vez de nada.
    """
    chave = empresa_id or CADEIA_PLATAFORMA
    head = db.get(AuditHashChainHead, chave)
    if head is None:
        return {"head_hash": GENESE, "sequencia": 0}
    return {"head_hash": head.head_hash, "sequencia": head.sequencia}


def pela_ordem_da_cadeia(registos: Iterable[Any]) -> list[Any]:
    """Os registos pela ordem dos elos, não pela do relógio.

    O `created_at` é o do momento em que o registo nasce e o elo é o do commit.
    Dois pedidos da mesma empresa em paralelo trocam as duas ordens — um pedido
    longo regista ao começar e confirma no fim —, e ler pela data acusava de
    partida uma cadeia íntegra.

    Segue-se o encadeamento a partir do elo que não aponta para nenhum registo
    presente (o início, ou o primeiro depois do que foi arquivado). O que não for
    alcançável por aí — um registo apagado a meio, um metido de fora, uma
    bifurcação — vai para o fim, pela data, e o `verificar` acusa-o como sempre.
    Os que não têm elo seguem à frente: o verificador salta-os.
    """
    todos = list(registos)
    sem_elo = [r for r in todos if getattr(r, "hash_registo", None) is None]
    com_elo = [r for r in todos if getattr(r, "hash_registo", None) is not None]
    presentes = {r.hash_registo for r in com_elo}
    seguintes: dict[str | None, list[Any]] = {}
    for r in com_elo:
        seguintes.setdefault(r.hash_anterior, []).append(r)

    inicios = [r for r in com_elo if r.hash_anterior not in presentes]
    ordenados: list[Any] = []
    vistos: set[int] = set()
    if inicios:
        atual = inicios[0]  # o mais antigo, pela ordem em que chegaram
        while atual is not None and id(atual) not in vistos:
            ordenados.append(atual)
            vistos.add(id(atual))
            candidatos = [c for c in seguintes.get(atual.hash_registo, []) if id(c) not in vistos]
            atual = candidatos[0] if candidatos else None
    resto = [r for r in com_elo if id(r) not in vistos]
    return sem_elo + ordenados + resto


# Quanto a data de um elo pode ficar atrás da do elo anterior. O `created_at`
# nasce quando a ação é registada e o elo quando o pedido confirma, por isso um
# pedido longo inverte legitimamente as duas ordens. Medido a 2026-09-27 nas
# cadeias do stack local (2552 elos) e do servidor dev (83): inversão máxima de
# 0 s. O teto teórico é o pedido mais longo que o nginx deixa correr (1800 s, os
# backups); 1 h cobre-o com margem e cobre acertos do relógio. Uma linha forjada
# com data de há mais de uma hora deixa de passar.
FOLGA_DATAS = timedelta(hours=1)

# Quantos ids de linhas sem elo seguem no resultado (o total vai sempre).
MAX_IDS_SEM_ELO = 20


def _instante(valor: Any) -> datetime | None:
    if not isinstance(valor, datetime):
        return None
    return valor if valor.tzinfo is not None else valor.replace(tzinfo=timezone.utc)


def verificar(
    registos: Iterable[Any], *, desde: str = GENESE, conferir_dados: bool = True
) -> dict[str, Any]:
    """Percorre uma sequência de registos e diz onde (e se) a cadeia parte.

    Os registos têm de vir por ordem de escrita. Devolve o ponto exato da
    primeira divergência, que é o que interessa a quem investiga: dizer só "a
    cadeia está partida" obriga a procurar à mão.

    Linhas sem elo (`hash_registo` a NULL) **anteriores** ao primeiro elo são a
    história de antes do encadeamento: contam como `ignorados` e não são erro —
    dizer que a história toda está partida por causa disso seria um alarme que
    ninguém voltaria a olhar. Essa história nunca foi protegida pela cadeia.

    Linhas sem elo **posteriores** ao primeiro elo vão para `sem_elo`. Uma linha
    assim ou foi escrita fora da aplicação (um script, uma escrita direta na
    base — a forma mais barata de forjar um registo) ou é uma escrita em sessão
    própria que não conseguiu o *head* a tempo. Não parte a cadeia, porque não
    faz parte dela, mas nunca pode passar como verdadeira: quem lê o resultado
    vê-a contada e identificada.

    Cada elo tem de ter data não anterior à do elo mais recente já visto, com
    `FOLGA_DATAS` de tolerância — senão a linha era acrescentada à cabeça com
    hash válido e uma data antiga, e aparecia no meio da história.

    `conferir_dados` recalcula o digest do conteúdo e compara-o com o guardado.
    Vale para linhas vivas; **não** vale para linhas vindas do arquivo, onde os
    dados foram mascarados de propósito — aí passa-se `False` e verifica-se a
    cadeia, que é o que o arquivo preserva.

    Uma cadeia lida a seguir a uma purga começa a meio: passa-se em `desde` a
    *head* do último registo arquivado, senão o primeiro elo aparece partido.
    """
    todos = list(registos)
    datas_dos_elos = [
        d for d in (_instante(getattr(r, "created_at", None)) for r in todos
                    if getattr(r, "hash_registo", None) is not None)
        if d is not None
    ]
    primeiro_elo = min(datas_dos_elos) if datas_dos_elos else None

    sem_elo: list[str] = []
    ignorados = 0
    for r in todos:
        if getattr(r, "hash_registo", None) is not None:
            continue
        quando = _instante(getattr(r, "created_at", None))
        if primeiro_elo is not None and (quando is None or quando >= primeiro_elo):
            sem_elo.append(str(r.id))
        else:
            ignorados += 1

    anterior = desde
    verificados = 0
    mais_recente: datetime | None = None

    def resultado(**campos: Any) -> dict[str, Any]:
        return {
            **campos,
            "verificados": verificados,
            "ignorados": ignorados,
            "sem_elo": len(sem_elo),
            "sem_elo_ids": sem_elo[:MAX_IDS_SEM_ELO],
        }

    for registo in todos:
        if getattr(registo, "hash_registo", None) is None:
            continue
        if conferir_dados:
            digest = impressao_dos_dados(registo.dados_anteriores, registo.dados_novos)
            if digest != getattr(registo, "dados_hash", None):
                return resultado(
                    integra=False, motivo="dados_alterados", registo_id=str(registo.id),
                    esperado=getattr(registo, "dados_hash", None), encontrado=digest,
                )
        if registo.hash_anterior != anterior:
            return resultado(
                integra=False, motivo="elo_partido", registo_id=str(registo.id),
                esperado=anterior, encontrado=registo.hash_anterior,
            )
        recalculado = impressao_do_registo(registo, registo.hash_anterior)
        if recalculado != registo.hash_registo:
            return resultado(
                integra=False, motivo="conteudo_alterado", registo_id=str(registo.id),
                esperado=registo.hash_registo, encontrado=recalculado,
            )
        quando = _instante(getattr(registo, "created_at", None))
        if quando is not None:
            if mais_recente is not None and quando < mais_recente - FOLGA_DATAS:
                return resultado(
                    integra=False, motivo="data_fora_de_ordem", registo_id=str(registo.id),
                    esperado=f">= {(mais_recente - FOLGA_DATAS).isoformat()}",
                    encontrado=quando.isoformat(),
                )
            mais_recente = quando if mais_recente is None else max(mais_recente, quando)
        anterior = registo.hash_registo
        verificados += 1
    return resultado(integra=True, head=anterior)


def revogar_escrita_direta(db: Session, papel: str) -> None:
    """Retira UPDATE e DELETE da tabela ao papel da aplicação.

    Os *grants* não provam nada a terceiros — quem for dono da base repõe-nos.
    Defendem do acidente e do abuso trivial, que é o que acontece muito mais
    vezes do que um adversário determinado. A cadeia é que prova; isto impede.

    Não corre nas migrações de propósito: o nome do papel muda por instalação, e
    uma migração que assuma um nome errado falha o arranque de quem o tenha
    diferente. É um ato de operação, documentado no runbook.
    """
    db.execute(text(f'REVOKE UPDATE, DELETE ON audit_logs FROM "{papel}"'))
