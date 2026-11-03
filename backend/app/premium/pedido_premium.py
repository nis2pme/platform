"""
Pedido dos módulos premium pelo interface (on-prem, só o open-core instalado).

O administrador pede; o fornecedor aprova depois do pagamento; o agente do
anfitrião descarrega e arranca o sidecar (o núcleo não reinicia); o fornecedor
emite a licença para a identidade do sidecar; o administrador ativa (o backend
reinicia uma vez). Este módulo é a parte do núcleo:

- fala com o serviço de licenças do fornecedor (`NIS2PME_LICENCAS_URL`, com a CA
  do fornecedor) — só enquanto há um pedido em aberto;
- guarda o estado do pedido num ficheiro da instalação (o segredo do pedido, cifrado);
- deixa ao agente os pedidos `instalar_premium` e `ativar_premium` e lê o progresso
  que ele escreve; com a licença emitida, `acordar_premium` (o sidecar procura-a já).

O que vem do serviço e da pasta do agente é tratado como não fiável: tamanhos,
conjuntos fechados, nunca HTML. O núcleo nunca indica URLs ao agente.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

ESTADO_FICHEIRO = Path("/app/data/premium_pedido.json")

# Versão dos termos da licença premium que o administrador aceita ao pedir.
TERMOS_VERSAO = "2026-10"
PLANOS = ("premium",)

_MAX_RESPOSTA = 64 * 1024
_TIMEOUT = 15
# Sem nenhuma resposta do serviço durante isto, o pedido expira do lado da instalação.
_SEM_RESPOSTA = timedelta(days=35)
# Um progresso do agente sem mexer há mais disto é de um agente que morreu.
_AGENTE_PARADO = timedelta(minutes=45)
# Entre dois pedidos ao agente para acordar o sidecar; e a versão do agente que os sabe fazer.
_ACORDAR_MINIMO = timedelta(minutes=1)
_AGENTE_QUE_ACORDA = 2

ESTADOS_SERVICO = frozenset({
    "pedido", "aprovado", "identidade_recebida", "licenca_emitida", "licenca_pronta", "ativo",
    "rejeitado", "expirado", "cancelado",
})
FECHOS = frozenset({"rejeitado", "expirado", "cancelado"})
FASES_AGENTE = frozenset({
    "pedido_recebido", "a_verificar", "a_descarregar", "a_carregar", "a_arrancar", "instalado",
    "a_confirmar_licenca", "a_ativar", "ativado", "falhou", "revertido",
})
CODIGOS_AGENTE = frozenset({
    "pedido_invalido", "assinatura_invalida", "canal_invalido", "versao_nao_superior", "descarga_falhou",
    "hash_invalido", "sem_espaco", "sem_memoria", "sem_redes", "pacote_invalido", "load_falhou",
    "imagem_invalida", "arranque_falhou", "licenca_invalida", "nucleo_antigo", "pedido_recusado", "interno",
})
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SEGREDO = re.compile(r"^[A-Za-z0-9_-]{43}$")
_VERSAO = re.compile(r"^(\d{1,4}\.\d{1,4}\.\d{1,4})")


class PedidoErro(Exception):
    """O pedido não pode seguir; `codigo` (e `campo`) é o que a API devolve."""

    def __init__(self, codigo: str, campo: str | None = None):
        super().__init__(codigo)
        self.codigo = codigo
        self.campo = campo


# ---------------------------------------------------------------------------
# Estado local (um ficheiro da instalação)
# ---------------------------------------------------------------------------

def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _data(texto: object) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(texto))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def ler_estado() -> dict | None:
    try:
        if ESTADO_FICHEIRO.is_symlink() or ESTADO_FICHEIRO.stat().st_size > 16384:
            return None
        dados = json.loads(ESTADO_FICHEIRO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dados if isinstance(dados, dict) else None


def _gravar_estado(estado: dict) -> None:
    estado["atualizado_em"] = _iso(_agora())
    ESTADO_FICHEIRO.parent.mkdir(parents=True, exist_ok=True)
    tmp = ESTADO_FICHEIRO.with_name(f".{ESTADO_FICHEIRO.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, ESTADO_FICHEIRO)


def _apagar_estado() -> None:
    try:
        ESTADO_FICHEIRO.unlink()
    except FileNotFoundError:
        pass


def em_aberto(estado: dict | None = None) -> bool:
    estado = ler_estado() if estado is None else estado
    return bool(estado) and estado.get("fechado") is not True


# O ciclo do pedido: ao serviço pergunta-se de hora a hora; enquanto o agente faz
# alguma coisa por nós (instalar, ativar), o desfecho lê-se de minuto a minuto, só
# do ficheiro local, para o aviso não ficar à espera de alguém abrir o cartão.
CICLO_SERVICO = 3600
CICLO_AGENTE = 60


def acao_em_curso(estado: dict | None = None) -> bool:
    estado = ler_estado() if estado is None else estado
    return em_aberto(estado) and bool(estado.get("acao_agente"))


def _segredo(estado: dict) -> str:
    from app.shared.pii import decifrar_pii

    valor = decifrar_pii(estado.get("segredo_cifrado")) or ""
    if not _SEGREDO.match(valor):
        raise PedidoErro("estado_local_ilegivel")
    return valor


# ---------------------------------------------------------------------------
# Serviço de licenças do fornecedor
# ---------------------------------------------------------------------------

def _url_base() -> str:
    url = get_settings().NIS2PME_LICENCAS_URL.rstrip("/")
    if not (url.startswith("https://") or url.startswith(("http://localhost", "http://127.0.0.1"))):
        raise PedidoErro("servico_mal_configurado")
    return url


def _post(caminho: str, corpo: dict) -> tuple[int, dict]:
    """POST JSON ao serviço. (código HTTP, corpo) — erros HTTP incluídos; falha de
    rede → PedidoErro("servico_indisponivel")."""
    from app.shared.tls_fornecedor import contexto_fornecedor

    pedido = urllib.request.Request(
        _url_base() + caminho,
        data=json.dumps(corpo).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": f"NIS2PME/{get_settings().APP_VERSION}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(pedido, timeout=_TIMEOUT, context=contexto_fornecedor()) as resp:  # noqa: S310
            codigo, bruto = resp.status, resp.read(_MAX_RESPOSTA + 1)
    except urllib.error.HTTPError as exc:
        codigo, bruto = exc.code, exc.read(_MAX_RESPOSTA + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("Serviço de licenças inacessível: %s", exc)
        raise PedidoErro("servico_indisponivel") from None
    if len(bruto) > _MAX_RESPOSTA:
        raise PedidoErro("servico_indisponivel")
    try:
        dados = json.loads(bruto.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        dados = {}
    return codigo, dados if isinstance(dados, dict) else {}


def _detalhe(dados: dict) -> dict:
    d = dados.get("detail")
    return d if isinstance(d, dict) else {}


def _motivo_do_servico(valor: object) -> str:
    """O motivo que o fornecedor escreveu: texto curto, sem controlo (nunca HTML)."""
    texto = "" if not isinstance(valor, str) else valor
    texto = "".join(c if c.isprintable() else " " for c in texto)
    return " ".join(texto.split())[:500]


# ---------------------------------------------------------------------------
# Pasta do agente (a mesma das atualizações)
# ---------------------------------------------------------------------------

def _ler_progresso_agente(pedido_local: str | None) -> dict | None:
    """O que o agente deixou para o NOSSO pedido (fases premium), ou None."""
    from app.updates.service import _ler_pares, _pasta_estado

    if not pedido_local:
        return None
    pares = _ler_pares(_pasta_estado() / "estado.txt")
    if not pares or pares.get("pedido_id") != pedido_local:
        return None
    fase = pares.get("fase", "")
    if fase not in FASES_AGENTE:
        return None
    codigo = pares.get("codigo", "")
    atualizado = _data(pares.get("atualizado", "").replace("Z", "+00:00"))
    parado = fase not in {"instalado", "ativado", "falhou", "revertido"} and (
        atualizado is None or _agora() - atualizado > _AGENTE_PARADO
    )
    return {"fase": fase, "codigo": codigo if codigo in CODIGOS_AGENTE else None, "parado": parado}


def _escrever_pedido_agente(linhas: dict[str, str]) -> str:
    """Deixa o pedido para o agente (escrita atómica). Devolve o id local."""
    from app.updates.service import _pasta_pedido, _pedido_pendente, atualizacao_em_curso

    if _pedido_pendente() or atualizacao_em_curso():
        raise PedidoErro("agente_ocupado")
    pedido_local = str(uuid.uuid4())
    corpo = "".join(f"{k}={v}\n" for k, v in {"pedido_id": pedido_local, **linhas}.items())
    pasta = _pasta_pedido()
    try:
        tmp = pasta / f".pedido.{pedido_local}.tmp"
        tmp.write_text(corpo, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, pasta / "pedido.txt")
    except OSError as exc:
        logger.error("Não foi possível deixar o pedido ao agente: %s", exc)
        raise PedidoErro("agente_inativo") from exc
    return pedido_local


# ---------------------------------------------------------------------------
# O que o administrador faz
# ---------------------------------------------------------------------------

def _nif_digitos(texto: str | None) -> str:
    return "".join(c for c in (texto or "") if c.isdigit())


def nif_pt_valido(nif: str) -> bool:
    """NIF português: 9 dígitos com o dígito de controlo certo (a mesma regra do
    serviço de licenças, que o recusa sem ela)."""
    if not re.fullmatch(r"\d{9}", nif or ""):
        return False
    controlo = 11 - sum(int(nif[i]) * (9 - i) for i in range(8)) % 11
    return (0 if controlo >= 10 else controlo) == int(nif[8])


def dados_da_empresa(db, empresa_id: uuid.UUID) -> dict:
    """Nome, NIF e email da empresa (decifrados) para preencher o pedido."""
    from app.empresas.models import Empresa
    from app.shared.pii import decifrar_pii

    empresa = db.get(Empresa, empresa_id)
    if empresa is None:
        return {"nome": "", "nif": "", "email": "", "locale": "pt"}
    return {
        "nome": decifrar_pii(empresa.nome) or "",
        "nif": _nif_digitos(decifrar_pii(empresa.nif)),
        "email": decifrar_pii(empresa.email) or "",
        "locale": empresa.locale_preferido or "pt",
    }


def motivo_nao_pode_pedir(db, empresa_id: uuid.UUID) -> str | None:
    from app.updates.service import agente_ativo

    s = get_settings()
    if s.DEPLOYMENT_MODE != "onprem":
        return "so_onprem"
    if s.PREMIUM_ENABLED:
        return "premium_ja_ativo"
    if em_aberto():
        return "em_curso"
    if not agente_ativo():
        return "agente_inativo"
    if not nif_pt_valido(dados_da_empresa(db, empresa_id)["nif"]):
        return "sem_nif"
    return None


def criar(db, *, utilizador, faturacao: dict, contacto: dict, plano: str, termos_versao: str,
          transferencia: bool = False) -> dict:
    """Pede os módulos premium ao fornecedor. Uma transferência substitui outra
    instalação da mesma empresa (máquina nova ou restauro): o fornecedor vê as
    licenças anteriores e revoga-as ao emitir a nova."""
    from app.shared.pii import cifrar_pii
    from app.updates.service import obter_instance_id

    motivo = motivo_nao_pode_pedir(db, utilizador.empresa_id)
    if motivo:
        raise PedidoErro(motivo)
    if plano not in PLANOS:
        raise PedidoErro("campo_invalido", "plano")
    if termos_versao != TERMOS_VERSAO:
        raise PedidoErro("termos_desatualizados")
    empresa = dados_da_empresa(db, utilizador.empresa_id)
    versao = _VERSAO.match(get_settings().APP_VERSION or "")
    corpo = {
        "instalacao_id": obter_instance_id(),
        "empresa_id": str(utilizador.empresa_id),
        "nif": empresa["nif"],
        "empresa_nome": empresa["nome"],
        "faturacao": faturacao,
        "contacto": contacto,
        "plano": plano,
        "termos_versao": termos_versao,
        "versao_nucleo": versao.group(1) if versao else "0.0.0",
        "idioma": "en" if empresa["locale"].startswith("en") else "pt",
        "tipo": "transferencia" if transferencia else "novo",
    }
    codigo, dados = _post("/pedidos", corpo)
    if codigo == 422:
        raise PedidoErro("campo_invalido", str(_detalhe(dados).get("campo") or "") or None)
    if codigo == 409:
        raise PedidoErro("pedido_em_curso")
    if codigo == 429:
        raise PedidoErro("demasiados_pedidos")
    pedido_id, segredo = dados.get("pedido_id"), dados.get("segredo")
    if codigo != 201 or not isinstance(pedido_id, str) or not _UUID.match(pedido_id) \
            or not isinstance(segredo, str) or not _SEGREDO.match(segredo):
        raise PedidoErro("servico_indisponivel")
    estado = {
        "pedido_id": pedido_id,
        "segredo_cifrado": cifrar_pii(segredo),
        "estado_servico": "pedido",
        "motivo": "",
        "plano": plano,
        "empresa_id": str(utilizador.empresa_id),
        "utilizador_id": str(utilizador.id),
        "nif": empresa["nif"],
        "criado_em": _iso(_agora()),
        "ultima_resposta": _iso(_agora()),
        "acao_agente": None,
        "pedido_agente": None,
        "avisos": [],
    }
    _gravar_estado(estado)
    return estado


def cancelar() -> dict:
    estado = ler_estado()
    if not em_aberto(estado):
        raise PedidoErro("sem_pedido")
    codigo, dados = _post(f"/pedidos/{estado['pedido_id']}/cancelar", {"segredo": _segredo(estado)})
    if codigo == 409:
        raise PedidoErro("pedido_nao_cancelavel")
    if codigo not in (200, 404):
        raise PedidoErro("servico_indisponivel")
    return _fechar(estado, "cancelado", "")


def _fechar(estado: dict, tipo: str, motivo: str) -> dict:
    """O pedido acabou sem premium: deixa de se consultar e fica só o aviso, até o
    administrador o dispensar ou pedir de novo."""
    fechado = {
        "fechado": True,
        "tipo": tipo,
        "motivo": motivo,
        "pedido_id": estado.get("pedido_id"),
        "empresa_id": estado.get("empresa_id"),
        "fechado_em": _iso(_agora()),
    }
    _gravar_estado(fechado)
    return fechado


def dispensar_aviso() -> None:
    estado = ler_estado()
    if estado and estado.get("fechado"):
        _apagar_estado()


def ativar() -> str:
    """Deixa ao agente o pedido de ativação: ele confirma a licença com o sidecar,
    liga os overlays e recria o backend (este processo termina pelo caminho)."""
    estado = ler_estado()
    if not em_aberto(estado) or estado.get("estado_servico") != "licenca_pronta":
        raise PedidoErro("licenca_por_validar")
    if estado.get("acao_agente") and not _acao_terminada(estado):
        raise PedidoErro("agente_ocupado")
    pedido_local = _escrever_pedido_agente({"acao": "ativar_premium"})
    estado.update(acao_agente="ativar", pedido_agente=pedido_local, ultimo_erro=None)
    _gravar_estado(estado)
    return pedido_local


def tentar_de_novo() -> str:
    """Depois de uma instalação falhada, com o pedido ainda aprovado."""
    estado = ler_estado()
    if not em_aberto(estado) or estado.get("estado_servico") != "aprovado":
        raise PedidoErro("nada_a_repetir")
    if estado.get("acao_agente") and not _acao_terminada(estado):
        raise PedidoErro("agente_ocupado")
    return _iniciar_instalacao(estado)


def _acao_terminada(estado: dict) -> bool:
    prog = _ler_progresso_agente(estado.get("pedido_agente"))
    return prog is None or prog["fase"] in {"instalado", "ativado", "falhou", "revertido"} or prog["parado"]


def _iniciar_instalacao(estado: dict) -> str:
    pedido_local = _escrever_pedido_agente({
        "acao": "instalar_premium",
        "premium_pedido": estado["pedido_id"],
        "segredo": _segredo(estado),
        "nif": estado["nif"],
        "empresa_id": estado["empresa_id"],
    })
    estado.update(acao_agente="instalar", pedido_agente=pedido_local, ultimo_erro=None)
    _gravar_estado(estado)
    return pedido_local


# ---------------------------------------------------------------------------
# Consulta (o ciclo de hora a hora, o «Verificar agora» e o arranque)
# ---------------------------------------------------------------------------

def processar_agente(estado: dict) -> list[tuple[str, dict]]:
    """O desfecho do que o agente fez por nós, uma vez. Devolve os eventos novos
    (`instalado`, `instalacao_falhou`, `ativado`, `ativacao_falhou`)."""
    prog = _ler_progresso_agente(estado.get("pedido_agente"))
    acao = estado.get("acao_agente")
    if prog is None or not acao:
        return []
    eventos: list[tuple[str, dict]] = []
    if acao == "instalar" and prog["fase"] == "instalado":
        eventos.append(("instalado", {}))
        estado.update(acao_agente=None, instalado=True)
    elif acao == "ativar" and prog["fase"] == "ativado":
        eventos.append(("ativado", {}))
        estado.update(acao_agente=None, ativado=True)
    elif prog["fase"] in {"falhou", "revertido"} or prog["parado"]:
        codigo = prog["codigo"] or "interno"
        eventos.append(("instalacao_falhou" if acao == "instalar" else "ativacao_falhou",
                        {"codigo": codigo, "revertido": prog["fase"] == "revertido"}))
        estado.update(acao_agente=None, ultimo_erro=codigo)
    return eventos


def consultar(*, rede: bool = True) -> tuple[dict | None, list[tuple[str, dict]]]:
    """Atualiza o estado do pedido. Com `rede`, pergunta ao serviço; sem ela, só lê o
    agente. Devolve (estado, eventos) — os eventos são para avisar e registar."""
    estado = ler_estado()
    if not em_aberto(estado):
        return estado, []
    eventos = processar_agente(estado)
    if estado.get("ativado"):
        # O backend já arrancou com o premium: o pedido acabou.
        try:
            _post(f"/pedidos/{estado['pedido_id']}/concluir", {"segredo": _segredo(estado)})
        except PedidoErro:
            pass  # o serviço fica com `licenca_emitida`; a instalação segue
        _apagar_estado()
        return None, eventos
    if not rede:
        _gravar_estado(estado)
        return estado, eventos
    try:
        codigo, dados = _post(f"/pedidos/{estado['pedido_id']}/estado", {"segredo": _segredo(estado)})
    except PedidoErro:
        criado = _data(estado.get("ultima_resposta")) or _data(estado.get("criado_em")) or _agora()
        if _agora() - criado > _SEM_RESPOSTA:
            eventos.append(("fechado", {"tipo": "sem_resposta", "motivo": ""}))
            return _fechar(estado, "sem_resposta", ""), eventos
        _gravar_estado(estado)
        return estado, eventos
    if codigo == 404:
        eventos.append(("fechado", {"tipo": "desconhecido", "motivo": ""}))
        return _fechar(estado, "desconhecido", ""), eventos
    novo = dados.get("estado") if codigo == 200 else None
    if novo not in ESTADOS_SERVICO:
        _gravar_estado(estado)
        return estado, eventos
    estado["ultima_resposta"] = _iso(_agora())
    anterior = estado.get("estado_servico")
    estado["estado_servico"] = novo
    if novo in FECHOS:
        motivo = _motivo_do_servico(dados.get("motivo"))
        eventos.append(("fechado", {"tipo": novo, "motivo": motivo}))
        return _fechar(estado, novo, motivo), eventos
    if novo == "aprovado" and not estado.get("acao_agente") and not estado.get("instalado") \
            and not estado.get("ultimo_erro"):
        # A instalação arranca sozinha; com o agente ocupado, tenta-se na consulta seguinte.
        try:
            _iniciar_instalacao(estado)
            eventos.append(("aprovado", {}))
        except PedidoErro as exc:
            logger.info("Pedido premium aprovado, instalação adiada: %s", exc.codigo)
    if novo == "licenca_emitida":
        _acordar_sidecar(estado)
    if novo == "licenca_pronta" and anterior != "licenca_pronta":
        eventos.append(("pronto", {}))
    _gravar_estado(estado)
    return estado, eventos


def _acordar_sidecar(estado: dict) -> None:
    """A licença está emitida e o sidecar ainda não a foi buscar: o agente pede-lhe a
    procura já (sozinho, o sidecar só procura de 15 em 15 minutos). É um toque: não
    ocupa o pedido, e se não chegar a licença vem na procura seguinte do sidecar."""
    from app.updates.service import ler_agente

    if not estado.get("instalado") or estado.get("acao_agente"):
        return
    acordado = _data(estado.get("acordado_em"))
    if acordado and _agora() - acordado < _ACORDAR_MINIMO:
        return
    if ler_agente()["versao"] < _AGENTE_QUE_ACORDA:
        return
    try:
        _escrever_pedido_agente({"acao": "acordar_premium"})
    except PedidoErro as exc:
        logger.info("Pedido premium: sidecar por acordar (%s)", exc.codigo)
        return
    estado["acordado_em"] = _iso(_agora())


def vista(db, empresa_id: uuid.UUID) -> dict:
    """O que o cartão mostra."""
    estado = ler_estado()
    base = {
        "pode_pedir": False,
        "motivo_nao_pode": None,
        "estado": "sem_pedido",
        "motivo": "",
        "aviso": None,
        "plano": None,
        "criado_em": None,
        "agente": None,
        "erro": None,
        "termos_versao": TERMOS_VERSAO,
        "empresa": None,
        "sugere_transferencia": False,
    }
    if estado and estado.get("fechado"):
        base["aviso"] = {"tipo": estado.get("tipo"), "motivo": estado.get("motivo") or ""}
    if em_aberto(estado):
        prog = _ler_progresso_agente(estado.get("pedido_agente"))
        base.update(
            estado=estado.get("estado_servico"), plano=estado.get("plano"), criado_em=estado.get("criado_em"),
            agente=prog if prog and estado.get("acao_agente") else None, erro=estado.get("ultimo_erro"),
        )
        return base
    motivo = motivo_nao_pode_pedir(db, empresa_id)
    base.update(pode_pedir=motivo is None, motivo_nao_pode=motivo)
    if motivo is None:
        empresa = dados_da_empresa(db, empresa_id)
        base["empresa"] = {"nome": empresa["nome"], "nif": empresa["nif"], "email": empresa["email"]}
        # Um restauro deixou dados premium por repor: esta instalação substitui outra.
        from app.backup.service import premium_pendente

        base["sugere_transferencia"] = premium_pendente() is not None
    return base


# ---------------------------------------------------------------------------
# Avisos e trilha dos eventos (um sítio só)
# ---------------------------------------------------------------------------

def avisar(db, *, empresa_id: uuid.UUID, pedido_id: str | None, eventos: list[tuple[str, dict]]) -> None:
    """Para cada evento: notificação aos administradores, linha na trilha e, nos que
    pedem ação ou dão um desfecho, email. Não faz commit."""
    from sqlmodel import select

    from app.auth.models import RoleUtilizador, Utilizador
    from app.notificacoes.catalogo import Codigo
    from app.notificacoes.email import alertar_pedido_premium
    from app.notificacoes.service import criar_notificacao, marcar_lidas_por_chave_prefixo
    from app.shared.audit import Acao, ResultadoAcao, registar_acao

    if not eventos:
        return
    admins = list(db.exec(select(Utilizador).where(
        Utilizador.empresa_id == empresa_id,
        Utilizador.ativo.is_(True),  # type: ignore[union-attr]
        Utilizador.role == RoleUtilizador.ADMIN,
    )).all())
    for evento, p in eventos:
        # Cada evento escreve a sua ação na trilha com a constante à vista: é por
        # ela que o catálogo e as traduções se conferem.
        if evento == "aprovado":
            codigo, email, params = Codigo.PREMIUM_APROVADO, None, {}
            registar_acao(db, acao=Acao.SISTEMA_PREMIUM_INSTALACAO, empresa_id=empresa_id,
                          dados_novos={"pedido_id": pedido_id})
        elif evento == "instalado":
            codigo, email, params = Codigo.PREMIUM_INSTALADO, None, {}
            registar_acao(db, acao=Acao.SISTEMA_PREMIUM_INSTALADO, empresa_id=empresa_id,
                          dados_novos={"pedido_id": pedido_id})
        elif evento == "pronto":
            codigo, email, params = Codigo.PREMIUM_PRONTO, "pronto", {}
        elif evento == "fechado":
            codigo, email = Codigo.PREMIUM_RECUSADO, "recusado"
            params = {"tipo": p.get("tipo", ""), "motivo": p.get("motivo", "")}
            registar_acao(db, acao=Acao.SISTEMA_PREMIUM_PEDIDO_FECHADO, empresa_id=empresa_id,
                          dados_novos={"pedido_id": pedido_id, "tipo": params["tipo"], "motivo": params["motivo"]})
        elif evento == "instalacao_falhou":
            codigo, email = Codigo.PREMIUM_FALHOU, "falhou"
            params = {"etapa": "instalar", "codigo": p.get("codigo", ""), "revertido": False}
            registar_acao(db, acao=Acao.SISTEMA_PREMIUM_INSTALACAO_FALHADA, resultado=ResultadoAcao.FALHA,
                          empresa_id=empresa_id, dados_novos={"pedido_id": pedido_id, "codigo": params["codigo"]})
        elif evento == "ativacao_falhou":
            codigo, email = Codigo.PREMIUM_FALHOU, "falhou"
            params = {"etapa": "ativar", "codigo": p.get("codigo", ""), "revertido": bool(p.get("revertido"))}
            registar_acao(db, acao=Acao.SISTEMA_PREMIUM_ATIVACAO_FALHADA, resultado=ResultadoAcao.FALHA,
                          empresa_id=empresa_id,
                          dados_novos={"pedido_id": pedido_id, "codigo": params["codigo"],
                                       "revertido": params["revertido"]})
        elif evento == "ativado":
            codigo, email, params = Codigo.PREMIUM_ATIVADO, "ativado", {}
            registar_acao(db, acao=Acao.SISTEMA_PREMIUM_ATIVADO, empresa_id=empresa_id,
                          dados_novos={"pedido_id": pedido_id})
        else:
            continue
        for u in admins:
            criar_notificacao(db, empresa_id=empresa_id, utilizador_id=u.id, codigo=codigo, params=params,
                              dedup_partes=(pedido_id or "",))
        if evento in {"instalado", "ativado", "fechado"} and pedido_id:
            # Uma falha anterior deste pedido deixou de pedir ação. Cada falha tem
            # chave própria (não deduplica), por isso fecham-se pelo prefixo.
            marcar_lidas_por_chave_prefixo(db, prefixo=f"{Codigo.PREMIUM_FALHOU}:{pedido_id}:",
                                           empresa_id=empresa_id)
        if email:
            alertar_pedido_premium(
                db, empresa_id=empresa_id, evento=email, params=params,
                chave_evento=f"{pedido_id}:{evento}:{uuid.uuid4().hex if evento.endswith('_falhou') else ''}",
            )


def consultar_e_avisar(db, *, rede: bool = True) -> dict | None:
    """O ciclo, o «Verificar agora» e o arranque: consulta e avisa do que mudou."""
    antes = ler_estado() or {}
    estado, eventos = consultar(rede=rede)
    empresa = (estado or {}).get("empresa_id") or antes.get("empresa_id")
    pedido_id = (estado or {}).get("pedido_id") or antes.get("pedido_id")
    if eventos and empresa:
        try:
            avisar(db, empresa_id=uuid.UUID(str(empresa)), pedido_id=pedido_id, eventos=eventos)
            db.commit()
        except Exception:  # noqa: BLE001 — um aviso que falha não pára o pedido
            db.rollback()
            logger.exception("Pedido premium: não foi possível registar os avisos")
    return estado
