"""
Canal de resposta pelo relay: o parecer do auditor volta a esta instância.

Ao enviar um dossiê pelo relay, a instância abre lá um canal: manda o SHA-256
de um token aleatório que só ela guarda (cifrado em `dossie_canais_relay`). O
auditor dono do convite deixa no canal o parecer, já assinado por ele e
cifrado para a chave de resposta deste dossiê; com o token, a instância
procura-o, descarrega-o e, depois de o importar, confirma-o (o relay apaga-o).

O relay continua a ser um correio cego e não é de confiança: o que chega passa
pela mesma verificação da importação por ficheiro (`parecer.py`), mais uma: o
parecer tem de ser sobre o dossiê deste canal.

A procura periódica só deteta e avisa. Importar exige uma pessoa: a
confirmação da impressão digital de um auditor novo (TOFU) não se faz sozinha.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.dossie.models import DossieCanalRelay, DossieGerado

logger = logging.getLogger(__name__)

# Enquanto o canal espera um parecer, procura-se de hora a hora; depois de um
# chegar (por importar ou já importado), uma vez por dia — só para dar por um
# parecer corrigido ou pelo fim do canal. Cada procura deixa o IP desta
# instância no registo da borda do relay: procurar menos é reter menos.
INTERVALO_AGUARDANDO = timedelta(hours=1)
INTERVALO_DEPOIS = timedelta(hours=24)
# Folga para o tick horário não saltar uma procura por uns segundos de atraso.
_FOLGA = timedelta(minutes=5)
# Lembrete quando o parecer está por importar e o relay o apaga dentro disto.
LEMBRETE_ANTES = timedelta(days=3)
# Prazo do canal quando o relay não o diz (o de omissão do relay).
_CANAL_OMISSAO = timedelta(days=90)
_TIMEOUT_S = 30

_HEX64 = re.compile(r"[0-9a-f]{64}")

# Estados em que o canal ainda pode trazer alguma coisa.
_VIVOS = ("aguardando", "disponivel", "importado")


class RelayIndisponivel(Exception):
    """O relay não respondeu (rede, TLS, 5xx): tenta-se na procura seguinte."""


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _utc(valor: datetime | None) -> datetime | None:
    """As colunas guardam UTC sem fuso; o que se lê volta a ter fuso."""
    if valor is None or valor.tzinfo is not None:
        return valor
    return valor.replace(tzinfo=timezone.utc)


def _instante(valor) -> datetime | None:
    """Um instante em segundos (cabeçalho ou JSON do relay) → datetime UTC."""
    try:
        return datetime.fromtimestamp(int(valor), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def novo_token() -> tuple[str, str]:
    """Token de resposta (256 bits) e o SHA-256 dele, que é o que vai ao relay."""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest()


def registar_canal(
    db: Session, dossie_id: uuid.UUID, empresa_id: uuid.UUID, relay: dict, token: str, resposta: dict,
) -> DossieCanalRelay | None:
    """Guarda o canal se o relay o abriu. Um relay de versão anterior ignora o
    hash e não devolve `canal_resposta`: sem canal, nada a procurar. Não faz
    commit."""
    if not isinstance(resposta, dict) or resposta.get("canal_resposta") is not True:
        return None
    canal = DossieCanalRelay(
        dossie_gerado_id=dossie_id,
        empresa_id=empresa_id,
        relay_url=relay["url"],
        relay_convite_id=relay["convite_id"],
        token_resposta=token,
        canal_expira_em=_instante(resposta.get("canal_expira_em")) or _agora() + _CANAL_OMISSAO,
    )
    db.add(canal)
    return canal


# ---------------------------------------------------------------------------
# Pedidos ao relay
# ---------------------------------------------------------------------------

class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
    """Um redirecionamento levaria o token de resposta (Authorization) para
    onde o relay mandasse. O relay nunca redireciona: um 3xx é um erro."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _abrir(pedido: urllib.request.Request):
    from app.shared.tls_fornecedor import contexto_fornecedor

    abridor = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=contexto_fornecedor()), _SemRedirecionar,
    )
    return abridor.open(pedido, timeout=_TIMEOUT_S)


def _pedido(canal: DossieCanalRelay, metodo: str, cabecalhos: dict | None = None, ler: int = 0):
    """(estado HTTP, cabeçalhos, corpo até `ler` bytes). Levanta
    RelayIndisponivel se o relay não respondeu."""
    from app.dossie.service import relay_permitido

    if not canal.token_resposta:
        raise RelayIndisponivel("canal fechado: o token de resposta já foi apagado")
    # O host foi validado no envio; volta a sê-lo aqui, para uma lista que
    # entretanto encolheu valer também para os canais já abertos.
    if not relay_permitido(canal.relay_url):
        raise RelayIndisponivel("relay fora da lista RELAY_HOSTS_PERMITIDOS")
    destino = f"{canal.relay_url}/api/parecer/{urllib.parse.quote(canal.relay_convite_id, safe='')}"
    pedido = urllib.request.Request(
        destino, method=metodo,
        headers={"Authorization": f"Bearer {canal.token_resposta}", **(cabecalhos or {})},
    )
    try:
        with _abrir(pedido) as resposta:
            corpo = resposta.read(ler) if ler else b""
            return resposta.status, resposta.headers, corpo
    except urllib.error.HTTPError as erro:
        if 300 <= erro.code < 400 or erro.code >= 500 or erro.code == 429:
            raise RelayIndisponivel(f"HTTP {erro.code}") from erro
        return erro.code, erro.headers, b""
    except (urllib.error.URLError, OSError, TimeoutError) as erro:
        raise RelayIndisponivel(str(erro)) from erro


def _fechar(canal: DossieCanalRelay) -> None:
    """O canal acabou: nada mais a confirmar, e o token de resposta deixa de
    ser guardado (já não abre nada, e não se retém o que não serve)."""
    canal.confirmar_sha256 = None
    canal.token_resposta = ""


def _sha_do_cabecalho(cabecalhos) -> str | None:
    valor = (cabecalhos.get("X-Parecer-Sha256") or "").strip().lower()
    return valor if _HEX64.fullmatch(valor) else None


# ---------------------------------------------------------------------------
# Procurar, descarregar, confirmar
# ---------------------------------------------------------------------------

def procurar(canal: DossieCanalRelay, agora: datetime | None = None) -> str | None:
    """Pergunta ao relay (HEAD) se há parecer e atualiza o canal. Devolve
    `"novo"` quando chegou um parecer que ainda não se tinha visto; `None` nos
    outros casos. Não faz commit."""
    agora = agora or _agora()
    if not canal.token_resposta:
        return None
    estado_http, cabecalhos, _ = _pedido(canal, "HEAD")
    canal.ultima_procura = agora
    expira = _instante(cabecalhos.get("X-Canal-Expira-Em")) if cabecalhos else None
    if expira:
        canal.canal_expira_em = expira

    if estado_http == 200:
        sha = _sha_do_cabecalho(cabecalhos)
        if sha is None:
            raise RelayIndisponivel("resposta do relay sem X-Parecer-Sha256")
        if sha == canal.importado_sha256:
            # É o que já entrou: falta só a confirmação ao relay.
            canal.confirmar_sha256 = sha
            return None
        canal.parecer_expira_em = _instante(cabecalhos.get("X-Parecer-Expira-Em"))
        if canal.estado == "disponivel" and canal.parecer_sha256 == sha:
            return None
        canal.estado = "disponivel"
        canal.parecer_sha256 = sha
        canal.detetado_em = agora
        canal.lembrete_enviado = False
        return "novo"

    if estado_http == 204:
        # Sem parecer no relay: ainda não chegou, já foi confirmado, ou caiu por
        # TTL antes de ser importado (o auditor pode voltar a enviá-lo).
        if canal.estado == "disponivel":
            canal.estado = "aguardando"
            canal.parecer_sha256 = None
            canal.parecer_expira_em = None
        return None

    if estado_http == 410:
        canal.canal_expira_em = min(_utc(canal.canal_expira_em), agora)
        if canal.estado != "importado":
            canal.estado = "expirado"
        _fechar(canal)
        return None

    if estado_http == 404:
        # O relay não conhece o canal (ou o token): não há volta a dar-lhe.
        logger.warning("relay: o canal do dossiê %s deixou de existir", canal.dossie_gerado_id)
        if canal.estado != "importado":
            canal.estado = "sem_canal"
        _fechar(canal)
        return None

    raise RelayIndisponivel(f"HTTP {estado_http}")


def _erro(http_status: int, codigo: str) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"codigo": codigo})


def descarregar(canal: DossieCanalRelay) -> tuple[bytes, str]:
    """O parecer cifrado que está no relay, e o SHA-256 dele (conferido com o
    que o relay declara)."""
    from app.dossie.parecer import FICHEIRO_MAX

    if not canal.token_resposta:
        raise _erro(status.HTTP_410_GONE, "canal_expirado")
    try:
        estado_http, cabecalhos, dados = _pedido(canal, "GET", ler=FICHEIRO_MAX + 1)
    except RelayIndisponivel as erro:
        logger.warning("relay: parecer do dossiê %s por descarregar: %s", canal.dossie_gerado_id, erro)
        raise _erro(status.HTTP_502_BAD_GATEWAY, "relay_inacessivel") from erro
    if estado_http == 204:
        raise _erro(status.HTTP_404_NOT_FOUND, "sem_parecer_no_relay")
    if estado_http == 410:
        raise _erro(status.HTTP_410_GONE, "canal_expirado")
    if estado_http != 200:
        raise _erro(status.HTTP_404_NOT_FOUND, "canal_desconhecido")
    if len(dados) > FICHEIRO_MAX:
        raise _erro(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "ficheiro_demasiado_grande")
    sha = hashlib.sha256(dados).hexdigest()
    declarado = _sha_do_cabecalho(cabecalhos)
    if declarado is not None and declarado != sha:
        raise _erro(status.HTTP_502_BAD_GATEWAY, "relay_resposta_invalida")
    return dados, sha


def confirmar_no_relay(canal: DossieCanalRelay) -> bool:
    """Diz ao relay que o parecer `canal.confirmar_sha256` entrou (o relay
    apaga-o). True quando não há mais nada a fazer: confirmado, ou o relay já
    tem outro (um corrigido, que fica) ou já não tem canal. Não faz commit."""
    if not canal.confirmar_sha256:
        return True
    if not canal.token_resposta:
        canal.confirmar_sha256 = None
        return True
    try:
        estado_http, _, _ = _pedido(canal, "DELETE", {"X-Parecer-Sha256": canal.confirmar_sha256})
    except RelayIndisponivel as erro:
        logger.info("relay: confirmação do parecer do dossiê %s adiada: %s", canal.dossie_gerado_id, erro)
        return False
    if estado_http in (200, 404, 409, 410):
        canal.confirmar_sha256 = None
        return True
    return False


# ---------------------------------------------------------------------------
# Leitura para o ecrã
# ---------------------------------------------------------------------------

def _iso(valor: datetime | None) -> str | None:
    valor = _utc(valor)
    return valor.isoformat() if valor else None


def _fingerprint_do_convite(dossie: DossieGerado | None) -> str | None:
    try:
        ambito = json.loads(dossie.ambito or "{}") if dossie else {}
    except ValueError:
        return None
    valor = ambito.get("auditor_fingerprint") if isinstance(ambito, dict) else None
    return valor if isinstance(valor, str) else None


def descrever(canal: DossieCanalRelay, dossie: DossieGerado | None, agora: datetime | None = None) -> dict:
    agora = agora or _agora()
    return {
        "dossie_id": str(canal.dossie_gerado_id),
        "dossie_criado_em": _iso(dossie.criado_em) if dossie else None,
        "auditor_fingerprint": _fingerprint_do_convite(dossie),
        "estado": canal.estado,
        "canal_aberto": canal.estado in _VIVOS and _utc(canal.canal_expira_em) > agora,
        "canal_expira_em": _iso(canal.canal_expira_em),
        "parecer_expira_em": _iso(canal.parecer_expira_em) if canal.estado == "disponivel" else None,
        "detetado_em": _iso(canal.detetado_em),
        "ultima_procura": _iso(canal.ultima_procura),
    }


def listar(db: Session, empresa_id: uuid.UUID, limite: int = 50) -> list[dict]:
    linhas = db.exec(
        select(DossieCanalRelay, DossieGerado)
        .join(DossieGerado, DossieGerado.id == DossieCanalRelay.dossie_gerado_id)  # type: ignore[arg-type]
        .where(DossieCanalRelay.empresa_id == empresa_id)
        .order_by(DossieCanalRelay.criado_em.desc())  # type: ignore[union-attr]
        .limit(limite)
    ).all()
    agora = _agora()
    return [descrever(canal, dossie, agora) for canal, dossie in linhas]


def canal_da_empresa(db: Session, empresa_id: uuid.UUID, dossie_id: uuid.UUID) -> DossieCanalRelay:
    canal = db.exec(
        select(DossieCanalRelay).where(
            DossieCanalRelay.dossie_gerado_id == dossie_id,
            DossieCanalRelay.empresa_id == empresa_id,
        )
    ).first()
    if canal is None:
        raise _erro(status.HTTP_404_NOT_FOUND, "canal_inexistente")
    return canal


# ---------------------------------------------------------------------------
# Procura periódica
# ---------------------------------------------------------------------------

def _destinatarios(db: Session, empresa_id: uuid.UUID) -> list[uuid.UUID]:
    """Quem pode importar o parecer (operar o dossiê) nesta empresa. A política
    de capacidades lê-se numa sessão própria: chama-se antes de mexer no canal,
    sem nada pendente na transação."""
    from app.auth.models import Utilizador
    from app.shared.capacidades import ClasseAcao, tem_capacidade

    utilizadores = db.exec(
        select(Utilizador).where(
            Utilizador.empresa_id == empresa_id,
            Utilizador.ativo.is_(True),  # type: ignore[union-attr]
        )
    ).all()
    return [u.id for u in utilizadores if tem_capacidade(u, "dossie", ClasseAcao.OPERAR)]


def _avisar(db: Session, canal: DossieCanalRelay, codigo: str, destinatarios: list[uuid.UUID]) -> None:
    from app.notificacoes.service import criar_notificacao

    dossie = db.get(DossieGerado, canal.dossie_gerado_id)
    params = {"data": _iso(dossie.criado_em) if dossie else None}
    if canal.parecer_expira_em:
        params["ate"] = _iso(canal.parecer_expira_em)
    for utilizador_id in destinatarios:
        criar_notificacao(
            db, empresa_id=canal.empresa_id, utilizador_id=utilizador_id, codigo=codigo, params=params,
            entidade_id=canal.dossie_gerado_id, dedup_partes=(canal.dossie_gerado_id, canal.parecer_sha256),
        )


def verificar_canais(db: Session, agora: datetime | None = None) -> int:
    """Uma passagem pelos canais vivos: procura os que estão na hora, avisa de
    pareceres novos e dos que vão cair do relay, e repete as confirmações que
    ficaram por fazer. Devolve quantos pareceres novos encontrou. Faz commit
    canal a canal: um relay em baixo não desfaz o que os outros trouxeram."""
    from app.notificacoes.catalogo import Codigo

    agora = agora or _agora()
    # Canais que fecharam: os que não chegaram a importar nada ficam
    # `expirado`; um importado continua a dizer que foi importado. Em nenhum
    # dos dois há mais nada a procurar nem a confirmar (o relay larga tudo).
    fechados = db.exec(
        select(DossieCanalRelay).where(
            DossieCanalRelay.estado.in_(_VIVOS),  # type: ignore[attr-defined]
            DossieCanalRelay.canal_expira_em <= agora,
            # Já fechado numa passagem anterior (um importado fica `importado`).
            DossieCanalRelay.token_resposta != "",
        )
    ).all()
    for canal in fechados:
        if canal.estado != "importado":
            canal.estado = "expirado"
            canal.parecer_sha256 = None
            canal.parecer_expira_em = None
        _fechar(canal)
    if fechados:
        db.commit()

    canais = db.exec(
        select(DossieCanalRelay).where(
            DossieCanalRelay.estado.in_(_VIVOS),  # type: ignore[attr-defined]
            DossieCanalRelay.canal_expira_em > agora,
        )
    ).all()
    novos = 0
    destinatarios: dict[uuid.UUID, list[uuid.UUID]] = {}
    for canal in canais:
        try:
            if canal.empresa_id not in destinatarios:
                destinatarios[canal.empresa_id] = _destinatarios(db, canal.empresa_id)
            avisar = destinatarios[canal.empresa_id]
            if canal.confirmar_sha256:
                confirmar_no_relay(canal)
            intervalo = INTERVALO_AGUARDANDO if canal.estado == "aguardando" else INTERVALO_DEPOIS
            ultima = _utc(canal.ultima_procura)
            if ultima is None or agora - ultima >= intervalo - _FOLGA:
                if procurar(canal, agora) == "novo":
                    novos += 1
                    _avisar(db, canal, Codigo.PARECER_RELAY_DISPONIVEL, avisar)
            expira = _utc(canal.parecer_expira_em)
            if (canal.estado == "disponivel" and expira and not canal.lembrete_enviado
                    and expira - agora <= LEMBRETE_ANTES):
                _avisar(db, canal, Codigo.PARECER_RELAY_A_EXPIRAR, avisar)
                canal.lembrete_enviado = True
            db.commit()
        except RelayIndisponivel as erro:
            db.rollback()
            logger.info("relay: procura do parecer do dossiê %s adiada: %s", canal.dossie_gerado_id, erro)
        except Exception:  # noqa: BLE001 — um canal estragado não pára os outros
            db.rollback()
            logger.exception("relay: procura do parecer do dossiê %s falhou", canal.dossie_gerado_id)
    return novos
