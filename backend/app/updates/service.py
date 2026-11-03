"""
Atualizações da instalação (on-prem).

Duas metades, ligadas por um pacote assinado:

1. **Verificar.** A app pergunta periodicamente se há versão mais recente. O
   pedido leva só um identificador ANÓNIMO da instância (não derivado de dados
   da empresa), a versão e o modo de deployment — nunca dados de clientes.
   Controlado por VERIFY_UPDATES (a false, não sai qualquer pedido). A resposta
   pode trazer um manifesto assinado com a chave mestra; sem assinatura válida
   o aviso aparece na mesma, mas a atualização pelo interface não se oferece.

2. **Aplicar.** O backend NUNCA toca no Docker. Escreve um pedido numa pasta
   partilhada com o anfitrião e lê o progresso que o agente de lá deixa noutra,
   só de leitura. Tudo o que vem da pasta de estado é tratado como não fiável:
   campos com formato estrito, valores desconhecidos recusados.

Com os módulos premium instalados, há dois componentes. O núcleo novo serve sempre
o premium anterior, por isso atualizam-se um de cada vez, cada um com o seu backup
e a sua reversão: primeiro o núcleo, e quando ele arranca bem o núcleo novo pede o
premium (se o administrador o incluiu). O premium também se atualiza sozinho (as
versões de segurança).
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.config import get_settings

logger = logging.getLogger(__name__)

_INSTANCE_ID_FILE = Path("/app/data/instance_id")

# Teto da resposta do servidor de versões: o manifesto tem uns 500 bytes.
_MAX_RESPOSTA = 64 * 1024
_MAX_MANIFESTO = 4096

# Estado em memória do último check (lido pelo endpoint /updates/status).
_estado: dict = {
    "latest_version": None,
    "security_critical": False,
    "notes_url": None,
    "verificado_em": None,
    "manifesto": None,
}
# O que o sidecar respondeu da última vez sobre o premium (sem o token, que caduca:
# ao aplicar pergunta-se outra vez).
_estado_premium: dict = {"estado": None, "versao": None, "seguranca": False, "direito": None}


def obter_instance_id() -> str:
    """Lê (ou cria no 1.º arranque) o identificador anónimo da instância."""
    try:
        if _INSTANCE_ID_FILE.exists():
            valor = _INSTANCE_ID_FILE.read_text(encoding="utf-8").strip()
            if valor:
                return valor
        novo = str(uuid.uuid4())
        _INSTANCE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _INSTANCE_ID_FILE.write_text(novo + "\n", encoding="utf-8")
        try:
            os.chmod(_INSTANCE_ID_FILE, 0o600)
        except OSError:
            pass
        return novo
    except OSError:
        # Sem volume gravável: id efémero (não persiste, mas não falha).
        return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Versões
# ---------------------------------------------------------------------------

_RE_VERSAO = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:-([0-9A-Za-z.-]{1,32}))?$")
_RE_VERSAO_ESTRITA = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")


def _versao_chave(v: str | None) -> tuple | None:
    """Chave de ordenação, ou None se não for uma versão. Uma pré-versão
    (`0.4.1-rc1`) fica antes da versão final (`0.4.1`)."""
    m = _RE_VERSAO.match((v or "").strip())
    if not m:
        return None
    maior, menor, corr, pre = m.groups()
    return (int(maior), int(menor), int(corr), 0 if pre else 1, pre or "")


def _ha_versao_mais_recente(atual: str, ultima: str | None) -> bool:
    chave_ultima, chave_atual = _versao_chave(ultima), _versao_chave(atual)
    if chave_ultima is None or chave_atual is None:
        return False
    return chave_ultima > chave_atual


def _url_notas(url: object) -> str | None:
    """O endereço das notas vai para um `href`: só `https`, sem espaços."""
    if not isinstance(url, str) or len(url) > 512:
        return None
    if not url.startswith("https://") or re.search(r"[\s<>\"']", url):
        return None
    return url


# ---------------------------------------------------------------------------
# Manifesto assinado
# ---------------------------------------------------------------------------

_RE_CHAVE = re.compile(r"^[a-z0-9_]{1,32}$")
_RE_VALOR = re.compile(r"^[A-Za-z0-9._:/@+=?&%#~-]{0,512}$")
_RE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CABECALHO_MANIFESTO = "nis2pme-manifesto 1"
_CANAIS = ("stable", "dev")


def _b64url_decode(texto: str) -> bytes:
    texto = texto.strip()
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _assinatura_valida(texto: object, assinatura: object, pubkey: str) -> bool:
    """A assinatura Ed25519 sobre os bytes exatos do texto é válida para `pubkey`
    (32 bytes crus em base64url)? Nunca levanta."""
    try:
        if not (isinstance(texto, str) and isinstance(assinatura, str) and pubkey):
            return False
        if len(texto) > _MAX_MANIFESTO:
            return False
        chave = Ed25519PublicKey.from_public_bytes(_b64url_decode(pubkey))
        chave.verify(_b64url_decode(assinatura), texto.encode("utf-8"))
    except (InvalidSignature, ValueError, binascii.Error, TypeError):
        return False
    return True


def verificar_manifesto(texto: object, assinatura: object, pubkey: str) -> dict | None:
    """O manifesto, já lido, se a assinatura for válida e o conteúdo tiver a forma
    esperada. Qualquer outra coisa devolve None: nunca levanta."""
    if not _assinatura_valida(texto, assinatura, pubkey):
        return None
    return _ler_manifesto(texto)


def _manifesto_premium_valido(texto: object, assinatura: object, versao: str) -> bool:
    """Um manifesto premium assinado pela raiz, desta versão. O agente confere-o
    outra vez, por inteiro: isto só evita deixar-lhe um pedido que vai recusar."""
    if not _assinatura_valida(texto, assinatura, get_settings().NIS2PME_MESTRA_PUBKEY):
        return False
    linhas = str(texto).splitlines()
    return bool(linhas) and linhas[0] == _CABECALHO_MANIFESTO \
        and "componente=premium" in linhas[1:] and f"versao={versao}" in linhas[1:]


def _ler_manifesto(texto: str) -> dict | None:
    linhas = texto.splitlines()
    if not linhas or linhas[0] != _CABECALHO_MANIFESTO:
        return None
    campos: dict[str, str] = {}
    for linha in linhas[1:]:
        if not linha:
            continue
        chave, sep, valor = linha.partition("=")
        if not sep or not _RE_CHAVE.match(chave) or not _RE_VALOR.match(valor) or chave in campos:
            return None
        campos[chave] = valor
    if not _RE_VERSAO_ESTRITA.match(campos.get("versao", "")) or not _RE_VERSAO_ESTRITA.match(campos.get("min_origem", "")):
        return None
    if any(not _RE_SHA256.match(campos.get(k, "")) for k in ("compose_sha256", "script_sha256", "agente_sha256")):
        return None
    for k in ("imagem_backend", "imagem_frontend"):
        if campos.get(k) and not _RE_DIGEST.match(campos[k]):
            return None
    canal = campos.get("canal", "stable")
    if canal not in _CANAIS:
        return None
    # O premium mais antigo que esta versão do núcleo ainda serve (opcional).
    if "premium_min" in campos and not _RE_VERSAO_ESTRITA.match(campos["premium_min"]):
        return None
    return {
        "versao": campos["versao"],
        "min_origem": campos["min_origem"],
        "premium_min": campos.get("premium_min"),
        "canal": canal,
        "critica": campos.get("critica") == "true",
        "notas": _url_notas(campos.get("notas")),
    }


# ---------------------------------------------------------------------------
# Verificação periódica
# ---------------------------------------------------------------------------

def _aceitar_resposta(dados: object) -> None:
    """Guarda o que o servidor de versões respondeu, já validado."""
    if not isinstance(dados, dict):
        raise ValueError("resposta sem forma de objeto")
    settings = get_settings()
    # Só se aceita a resposta do canal desta instalação. Uma resposta estável a quem
    # pediu `dev` (token em falta ou errado) ou o inverso não anuncia nada.
    canal = dados.get("channel", "stable")
    if canal != settings.UPDATE_CHANNEL:
        raise ValueError(f"resposta do canal '{canal}', esta instalação segue '{settings.UPDATE_CHANNEL}'")
    ultima = dados.get("latest_version")
    if _versao_chave(ultima) is None:
        raise ValueError("versão anunciada inválida")
    _estado["latest_version"] = ultima.strip().lstrip("v")
    _estado["security_critical"] = dados.get("security_critical") is True
    _estado["notes_url"] = _url_notas(dados.get("notes_url"))
    _estado["verificado_em"] = datetime.now(timezone.utc).isoformat()
    manifesto = verificar_manifesto(
        dados.get("manifesto"), dados.get("assinatura"), settings.NIS2PME_MESTRA_PUBKEY
    )
    # Um manifesto de outra versão, ou de outro canal, não serve a esta: não se
    # atualiza para o que o aviso não anuncia.
    if manifesto and manifesto["versao"] == _estado["latest_version"] and manifesto["canal"] == settings.UPDATE_CHANNEL:
        _estado["manifesto"] = {
            **manifesto,
            "texto": dados["manifesto"],
            "assinatura": dados["assinatura"],
        }
    else:
        _estado["manifesto"] = None


def verificar_updates_sync() -> None:
    """Faz o pedido de verificação (bloqueante; chamar via asyncio.to_thread)."""
    settings = get_settings()
    if not settings.VERIFY_UPDATES:
        return
    dados = {
        "instance_id": obter_instance_id(),
        "version": settings.APP_VERSION,
        "deployment_mode": settings.DEPLOYMENT_MODE,
    }
    cabecalhos = {
        "Content-Type": "application/json",
        "User-Agent": f"NIS2PME/{settings.APP_VERSION}",
    }
    # O canal estável não diz nada: o pedido é o de sempre. Só o `dev` o declara,
    # e leva o token que o servidor exige para o responder.
    if settings.UPDATE_CHANNEL != "stable":
        dados["channel"] = settings.UPDATE_CHANNEL
        if settings.UPDATE_CHANNEL_TOKEN:
            cabecalhos["Authorization"] = f"Bearer {settings.UPDATE_CHANNEL_TOKEN}"
    pedido = urllib.request.Request(
        settings.UPDATE_CHECK_URL,
        data=json.dumps(dados).encode("utf-8"),
        headers=cabecalhos,
        method="POST",
    )
    try:
        with urllib.request.urlopen(pedido, timeout=10) as resp:  # noqa: S310 (URL própria, https)
            bruto = resp.read(_MAX_RESPOSTA + 1)
        if len(bruto) > _MAX_RESPOSTA:
            raise ValueError("resposta acima do teto")
        _aceitar_resposta(json.loads(bruto.decode("utf-8")))
        logger.info("Verificação de atualizações: última versão = %s.", _estado["latest_version"])
    except Exception as exc:  # noqa: BLE001 — nunca bloquear/partir a app
        # Aviso, não debug: uma instalação que não alcança o servidor de versões
        # nunca avisaria ninguém de uma versão de segurança, e ninguém saberia.
        logger.warning("Verificação de atualizações falhou (ignorado): %s", exc)
    if settings.DEPLOYMENT_MODE == "onprem":
        verificar_premium_sync()


def _versao_simples(v: str) -> str | None:
    """`x.y.z` de uma versão (sem pré-versão), a forma que o sidecar aceita."""
    m = _RE_VERSAO.match((v or "").strip())
    return ".".join(m.groups()[:3]) if m else None


def verificar_premium_sync() -> dict:
    """Pergunta ao sidecar pela versão premium seguinte (bloqueante). Guarda o resumo
    para o ecrã e devolve a resposta inteira (com o token) a quem vai aplicar."""
    from app.premium.atualizacao_client import verificar

    instalada = ler_agente()["premium_versao"]
    nucleo = _versao_simples(get_settings().APP_VERSION)
    resposta = verificar(instalada, nucleo) if instalada and nucleo else {"estado": None}
    _estado_premium.update(
        estado=resposta["estado"], versao=resposta.get("versao"),
        seguranca=bool(resposta.get("seguranca")), direito=resposta.get("direito"),
    )
    return resposta


# ---------------------------------------------------------------------------
# Pasta partilhada com o agente do anfitrião
# ---------------------------------------------------------------------------

_FASES_PERCENTAGEM = {
    "pedido_recebido": 5,
    "a_verificar": 10,
    "a_descarregar": 20,
    "a_preparar": 30,
    "a_criar_backup": 40,
    "a_carregar": 50,
    "a_atualizar_imagens": 60,
    "a_arrancar": 75,
    "a_confirmar": 90,
    "concluido": 100,
}
_FASES_FINAIS = {"concluido", "falhou", "revertido"}
_CODIGOS_ERRO = {
    "pedido_invalido", "assinatura_invalida", "canal_invalido", "versao_nao_superior", "origem_antiga",
    "descarga_falhou", "hash_invalido", "sem_espaco", "sem_backup", "backup_falhou",
    "pull_falhou", "digest_invalido", "arranque_falhou", "migracao_aplicada", "interno",
    # Só os do premium, ou a guarda do núcleo que já não o serve.
    "premium_incompativel", "sem_premium", "nucleo_antigo", "sem_memoria", "pacote_invalido",
    "load_falhou", "imagem_invalida",
}
_COMPONENTES = ("nucleo", "premium")
# Um estado a meio, sem mexer, há mais de isto, é um agente que morreu: deixa de
# bloquear novos pedidos.
_ESTADO_PARADO = timedelta(minutes=45)


class PedidoRecusado(Exception):
    """O pedido não pode seguir; `codigo` é o que a API devolve."""

    def __init__(self, codigo: str):
        super().__init__(codigo)
        self.codigo = codigo


def _pasta_pedido() -> Path:
    return Path(get_settings().ATUALIZACAO_DIR) / "pedido"


def _pasta_estado() -> Path:
    return Path(get_settings().ATUALIZACAO_DIR) / "estado"


def _ler_pares(caminho: Path) -> dict[str, str] | None:
    """Um ficheiro `chave=valor` pequeno e não-simbólico, ou None."""
    try:
        if caminho.is_symlink() or not caminho.is_file() or caminho.stat().st_size > 4096:
            return None
        texto = caminho.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    pares: dict[str, str] = {}
    for linha in texto.splitlines():
        chave, sep, valor = linha.partition("=")
        if sep and _RE_CHAVE.match(chave):
            pares[chave] = valor
    return pares


def ler_agente() -> dict:
    """A marca de presença do agente, com a versão premium que ele instalou e se o
    núcleo a usa. A verdade é do agente; aqui só se mostra e se pergunta por ela."""
    pares = _ler_pares(_pasta_estado() / "agente.txt") or {}
    ativo = bool(re.match(r"^\d{1,3}$", pares.get("agente_versao", "")))
    premium = pares.get("premium_versao", "")
    return {
        "ativo": ativo,
        # O que o agente sabe fazer: um pedido que ele não conhece fica por fazer.
        "versao": int(pares["agente_versao"]) if ativo else 0,
        "premium_versao": premium if ativo and _RE_VERSAO_ESTRITA.match(premium) else None,
        "premium_ativo": ativo and pares.get("premium_ativo") == "1",
    }


def agente_ativo() -> bool:
    """O agente deixou a marca de presença? (Não prova que a unidade corre: a
    prova é o pedido ser recolhido, que o ecrã de progresso mede.)"""
    return ler_agente()["ativo"]


def _ler_estado_agente() -> dict | None:
    pares = _ler_pares(_pasta_estado() / "estado.txt")
    if not pares:
        return None
    fase = pares.get("fase", "")
    if fase not in _FASES_PERCENTAGEM and fase not in _FASES_FINAIS:
        return None
    codigo = pares.get("codigo", "")
    return {
        "pedido_id": pares.get("pedido_id", "") if re.match(r"^[0-9a-f-]{36}$", pares.get("pedido_id", "")) else "",
        "fase": fase,
        "versao_alvo": pares.get("versao_alvo", "") if _RE_VERSAO_ESTRITA.match(pares.get("versao_alvo", "")) else None,
        "versao_origem": pares.get("versao_origem", "") if _RE_VERSAO_ESTRITA.match(pares.get("versao_origem", "")) else None,
        "codigo": codigo if codigo in _CODIGOS_ERRO else None,
        "backup": pares.get("backup", "") if re.match(r"^[A-Za-z0-9._-]{1,128}$", pares.get("backup", "")) else None,
        "atualizado": pares.get("atualizado", ""),
    }


def _ultimo_pedido() -> dict | None:
    """O pedido que ESTE backend fez por último (guardado na pasta do pedido, que
    é a única que escreve): serve para não tomar por nosso um estado antigo."""
    try:
        dados = json.loads((_pasta_pedido() / "ultimo.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dados if isinstance(dados, dict) and isinstance(dados.get("pedido_id"), str) else None


def _pedido_pendente() -> bool:
    return (_pasta_pedido() / "pedido.txt").exists()


def _estado_parado(estado: dict) -> bool:
    try:
        quando = datetime.fromisoformat(estado["atualizado"].replace("Z", "+00:00"))
    except (ValueError, KeyError):
        return True
    return datetime.now(timezone.utc) - quando > _ESTADO_PARADO


def ler_progresso() -> dict:
    """O que o ecrã mostra. `estado` ∈ inativo | pendente | a_correr | concluido |
    falhou | revertido."""
    ultimo = _ultimo_pedido()
    base = {
        "estado": "inativo", "fase": None, "percentagem": 0, "versao_alvo": None,
        "versao_origem": None, "codigo": None, "pedido_id": None, "backup": None,
        "componente": "nucleo",
    }
    if not ultimo:
        return base
    base["pedido_id"] = ultimo["pedido_id"]
    if ultimo.get("componente") in _COMPONENTES:
        base["componente"] = ultimo["componente"]
    base["versao_alvo"] = ultimo.get("versao") if isinstance(ultimo.get("versao"), str) else None
    estado = _ler_estado_agente()
    if estado is None or estado["pedido_id"] != ultimo["pedido_id"]:
        if _pedido_pendente():
            base["estado"] = "pendente"
        return base
    base.update(
        fase=estado["fase"], versao_alvo=estado["versao_alvo"] or base["versao_alvo"],
        versao_origem=estado["versao_origem"], codigo=estado["codigo"], backup=estado["backup"],
    )
    if estado["fase"] in _FASES_FINAIS:
        base["estado"] = estado["fase"]
        base["percentagem"] = 100 if estado["fase"] == "concluido" else _FASES_PERCENTAGEM.get(estado["fase"], 0)
    elif _estado_parado(estado):
        base["estado"] = "inativo"
    else:
        base["estado"] = "a_correr"
        base["percentagem"] = _FASES_PERCENTAGEM[estado["fase"]]
    return base


def atualizacao_em_curso() -> bool:
    return ler_progresso()["estado"] in {"pendente", "a_correr"}


def resultado_por_registar() -> dict | None:
    """O desfecho do último pedido, uma única vez: ao ler um estado final, marca-o
    como visto (criação exclusiva — duas leituras ao mesmo tempo não o registam
    duas vezes) e devolve-o para a trilha."""
    progresso = ler_progresso()
    if progresso["estado"] not in _FASES_FINAIS:
        return None
    if not re.match(r"^[0-9a-f-]{36}$", progresso["pedido_id"] or ""):
        return None
    ultimo = _ultimo_pedido() or {}
    marca = _pasta_pedido() / f"visto-{progresso['pedido_id']}"
    try:
        os.close(os.open(marca, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return None
    except OSError:
        return None
    return {"progresso": progresso, "pedido": ultimo}


def uuid_ou_none(valor) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(valor))
    except ValueError:
        return None


def registar_desfecho(db, *, empresa_padrao: uuid.UUID | None = None) -> dict | None:
    """O desfecho do último pedido ao agente, uma única vez: a linha na trilha e,
    aos administradores da empresa que pediu, a notificação (e o email, se não
    terminou). Corre no ecrã do progresso e no ciclo de 5 em 5 minutos, para o
    aviso sair mesmo sem ninguém a olhar. Faz commit."""
    from sqlmodel import select

    from app.auth.models import RoleUtilizador, Utilizador
    from app.notificacoes.catalogo import Codigo
    from app.notificacoes.email import alertar_atualizacao_falhada
    from app.notificacoes.service import criar_notificacao
    from app.shared.audit import Acao, ResultadoAcao, registar_acao

    desfecho = resultado_por_registar()
    if not desfecho:
        return None
    progresso, pedido = desfecho["progresso"], desfecho["pedido"]
    concluida = progresso["estado"] == "concluido"
    empresa_id = uuid_ou_none(pedido.get("empresa_id")) or empresa_padrao
    registar_acao(
        db,
        acao=Acao.SISTEMA_ATUALIZACAO_CONCLUIDA if concluida else Acao.SISTEMA_ATUALIZACAO_FALHADA,
        resultado=ResultadoAcao.SUCESSO if concluida else ResultadoAcao.FALHA,
        empresa_id=empresa_id,
        utilizador_id=uuid_ou_none(pedido.get("utilizador_id")),
        dados_novos={
            "pedido_id": progresso["pedido_id"], "componente": progresso["componente"],
            "versao_origem": progresso["versao_origem"],
            "versao_alvo": progresso["versao_alvo"], "estado": progresso["estado"],
            "codigo": progresso["codigo"], "backup": progresso["backup"],
        },
    )
    if empresa_id:
        params = {"componente": progresso["componente"], "versao": progresso["versao_alvo"] or ""}
        if not concluida:
            params.update(codigo=progresso["codigo"] or "", revertido=progresso["estado"] == "revertido")
        admins = db.exec(select(Utilizador).where(
            Utilizador.empresa_id == empresa_id,
            Utilizador.ativo.is_(True),  # type: ignore[union-attr]
            Utilizador.role == RoleUtilizador.ADMIN,
        )).all()
        for u in admins:
            criar_notificacao(
                db, empresa_id=empresa_id, utilizador_id=u.id,
                codigo=Codigo.ATUALIZACAO_CONCLUIDA if concluida else Codigo.ATUALIZACAO_FALHOU,
                params=params, dedup_partes=(progresso["componente"],),
            )
        if not concluida:
            alertar_atualizacao_falhada(db, empresa_id=empresa_id, pedido_id=progresso["pedido_id"], params=params)
    db.commit()
    return desfecho


def _deixar_pedido(campos: dict[str, str], ultimo: dict) -> str:
    """Escreve o pedido para o agente (e, ao lado, o que este backend pediu) e
    devolve o seu identificador."""
    pedido_id = str(uuid.uuid4())
    corpo = "".join(f"{k}={v}\n" for k, v in {"pedido_id": pedido_id, **campos}.items())
    pasta = _pasta_pedido()
    try:
        (pasta / "ultimo.json").write_text(json.dumps({"pedido_id": pedido_id, **ultimo}), encoding="utf-8")
        # Escrita atómica: o agente é acordado quando o ficheiro aparece e nunca
        # pode ler metade dele.
        tmp = pasta / f".pedido.{pedido_id}.tmp"
        tmp.write_text(corpo, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, pasta / "pedido.txt")
    except OSError as exc:
        logger.error("Não foi possível escrever o pedido de atualização: %s", exc)
        raise PedidoRecusado("nao_atualizavel") from exc
    return pedido_id


def criar_pedido(*, utilizador_id: uuid.UUID, empresa_id: uuid.UUID, versao: str, sem_backup: bool,
                 incluir_premium: bool = False) -> str:
    """Deixa o pedido para o agente e devolve o seu identificador. Com
    `incluir_premium`, o núcleo novo pede o premium quando arrancar bem."""
    estado = obter_estado()
    if estado["atualizacao_em_curso"]:
        raise PedidoRecusado("em_curso")
    manifesto = _estado.get("manifesto")
    if not estado["atualizavel_pelo_ui"] or not manifesto:
        raise PedidoRecusado("nao_atualizavel")
    if versao != manifesto["versao"]:
        raise PedidoRecusado("versao_diferente")
    if not estado["backups_ativos"] and not sem_backup:
        raise PedidoRecusado("backups_desativados")

    return _deixar_pedido(
        {
            "versao": manifesto["versao"],
            "utilizador": str(utilizador_id),
            "sem_backup": "1" if sem_backup else "0",
            "assinatura": manifesto["assinatura"],
            "manifesto_b64": base64.b64encode(manifesto["texto"].encode("utf-8")).decode("ascii"),
        },
        {
            "versao": manifesto["versao"], "componente": "nucleo",
            "utilizador_id": str(utilizador_id), "empresa_id": str(empresa_id), "sem_backup": sem_backup,
            "premium_a_seguir": bool(incluir_premium and estado["premium_versao"]),
        },
    )


_RE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_RE_ASSINATURA = re.compile(r"^[A-Za-z0-9_-]{86}$")


def _pedido_premium(resposta: dict, *, utilizador_id: object, empresa_id: object, sem_backup: bool) -> str:
    """O pedido `atualizar_premium` com o que o sidecar acabou de devolver. Os
    campos vão para um ficheiro de linhas: só com a forma exata."""
    versao = resposta.get("versao") or ""
    if resposta.get("estado") != "disponivel" or not _RE_VERSAO_ESTRITA.match(versao):
        raise PedidoRecusado("nao_atualizavel")
    if not (_RE_TOKEN.match(resposta.get("token") or "") and _RE_ASSINATURA.match(resposta.get("assinatura") or "")
            and resposta.get("ficheiro") == f"premium-onprem-{versao}.tar.gz"
            and _manifesto_premium_valido(resposta.get("manifesto"), resposta.get("assinatura"), versao)):
        logger.warning("Atualização premium: a resposta do sidecar não confere; nada pedido ao agente.")
        raise PedidoRecusado("nao_atualizavel")
    return _deixar_pedido(
        {
            "acao": "atualizar_premium",
            "versao": versao,
            "utilizador": str(utilizador_id),
            "sem_backup": "1" if sem_backup else "0",
            "assinatura": resposta["assinatura"],
            "manifesto_b64": base64.b64encode(resposta["manifesto"].encode("utf-8")).decode("ascii"),
            "ficheiro": resposta["ficheiro"],
            "token": resposta["token"],
        },
        {
            "versao": versao, "componente": "premium",
            "utilizador_id": str(utilizador_id), "empresa_id": str(empresa_id), "sem_backup": sem_backup,
        },
    )


def criar_pedido_premium(*, utilizador_id: uuid.UUID, empresa_id: uuid.UUID, versao: str, sem_backup: bool) -> str:
    """O premium sozinho (a versão que o ecrã anunciou). Pergunta outra vez ao
    sidecar: o token de download é curto."""
    estado = obter_estado()
    if estado["atualizacao_em_curso"]:
        raise PedidoRecusado("em_curso")
    if not estado["premium_atualizavel"]:
        raise PedidoRecusado("nao_atualizavel")
    if not estado["backups_ativos"] and not sem_backup:
        raise PedidoRecusado("backups_desativados")
    resposta = verificar_premium_sync()
    if resposta.get("estado") == "disponivel" and resposta.get("versao") != versao:
        raise PedidoRecusado("versao_diferente")
    return _pedido_premium(resposta, utilizador_id=utilizador_id, empresa_id=empresa_id, sem_backup=sem_backup)


def encadear_premium() -> dict | None:
    """Depois de o núcleo atualizar bem, o pedido do premium que o administrador
    incluiu. Uma só tentativa por atualização do núcleo (marca exclusiva, como o
    desfecho): sem versão nova, ou sem resposta do sidecar, o ecrã volta a mostrar
    o premium para se atualizar à parte. Devolve o pedido feito, para a trilha."""
    ultimo = _ultimo_pedido() or {}
    if ultimo.get("componente", "nucleo") != "nucleo" or ultimo.get("premium_a_seguir") is not True:
        return None
    if not re.match(r"^[0-9a-f-]{36}$", ultimo["pedido_id"]) or ler_progresso()["estado"] != "concluido":
        return None
    try:
        os.close(os.open(_pasta_pedido() / f"encadeado-{ultimo['pedido_id']}", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except OSError:
        return None
    resposta = verificar_premium_sync()
    if resposta.get("estado") != "disponivel":
        logger.info("Atualização premium a seguir ao núcleo: nada a aplicar (%s).", resposta.get("estado"))
        return None
    try:
        pedido_id = _pedido_premium(
            resposta, utilizador_id=ultimo.get("utilizador_id"), empresa_id=ultimo.get("empresa_id"),
            sem_backup=ultimo.get("sem_backup") is True,
        )
    except PedidoRecusado:
        return None
    return {
        "pedido_id": pedido_id, "versao": resposta["versao"],
        "utilizador_id": ultimo.get("utilizador_id"), "empresa_id": ultimo.get("empresa_id"),
    }


# ---------------------------------------------------------------------------
# Estado para o ecrã
# ---------------------------------------------------------------------------

def _backups_ativos() -> bool:
    try:
        from app.backup.service import passphrase_definida

        return bool(passphrase_definida())
    except Exception:  # noqa: BLE001 — o estado não pode falhar por causa dos backups
        return False


def obter_estado() -> dict:
    """Estado para o endpoint /updates/status."""
    settings = get_settings()
    atual = settings.APP_VERSION
    ultima = _estado.get("latest_version")
    disponivel = bool(settings.VERIFY_UPDATES and _ha_versao_mais_recente(atual, ultima))
    manifesto = _estado.get("manifesto") if disponivel else None
    onprem = settings.DEPLOYMENT_MODE == "onprem"
    agente = ler_agente() if onprem else {"ativo": False, "premium_versao": None, "premium_ativo": False}
    premium = agente["premium_versao"]

    motivo = None
    if disponivel:
        if not onprem:
            motivo = "so_onprem"
        elif not manifesto:
            motivo = "sem_pacote_assinado"
        elif (_versao_chave(atual) or (0,)) < (_versao_chave(manifesto["min_origem"]) or (0,)):
            motivo = "origem_antiga"
        elif premium and manifesto.get("premium_min") \
                and (_versao_chave(premium) or (0,)) < (_versao_chave(manifesto["premium_min"]) or (0,)):
            motivo = "premium_incompativel"
        elif not agente["ativo"]:
            motivo = "agente_inativo"
    premium_disponivel = bool(
        onprem and settings.VERIFY_UPDATES and premium and _estado_premium["estado"] == "disponivel"
        and _ha_versao_mais_recente(premium, _estado_premium["versao"])
    )
    return {
        "verificar_ativo": settings.VERIFY_UPDATES,
        "versao_atual": atual,
        "canal": settings.UPDATE_CHANNEL,
        "ultima_versao": ultima,
        "update_disponivel": disponivel,
        "security_critical": (
            bool(_estado.get("security_critical")) or bool(manifesto and manifesto["critica"])
        ) if disponivel else False,
        "notes_url": _estado.get("notes_url") if disponivel else None,
        "atualizavel_pelo_ui": bool(disponivel and motivo is None),
        "motivo_nao_atualizavel": motivo,
        "backups_ativos": _backups_ativos() if onprem else False,
        "atualizacao_em_curso": atualizacao_em_curso() if onprem else False,
        "premium_versao": premium,
        "premium_ativo": agente["premium_ativo"],
        "premium_ultima": _estado_premium["versao"] if premium_disponivel else None,
        "premium_atualizavel": premium_disponivel,
        "premium_seguranca": premium_disponivel and _estado_premium["seguranca"],
        # todas | seguranca: com a licença expirada ou revogada, só as de segurança.
        "premium_direito": _estado_premium["direito"] if premium else None,
    }
