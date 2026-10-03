"""
Diretório de auditores — "encontrar auditor".

A app não guarda a lista: pergunta ao serviço público do ecossistema (o mesmo
servidor europeu do relay) **só quando um utilizador abre a página**, e passa
apenas os filtros que ele escreveu. Sem URL configurada não sai pedido nenhum.

O que vem de fora é tratado como dados de terceiros: cada entrada é reduzida a
campos conhecidos, com tamanhos limitados, e ligações só em https.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

from app.config import get_settings
from app.shared.tls_fornecedor import contexto_fornecedor

logger = logging.getLogger(__name__)

# Teto da resposta lida (o serviço devolve no máximo 200 entradas curtas).
RESPOSTA_MAX = 256 * 1024
ENTRADAS_MAX = 200
TEXTO_MAX = 300
APRESENTACAO_MAX = 2000


class DiretorioIndisponivel(Exception):
    """O serviço não respondeu, ou respondeu algo que não é a lista."""


def _texto(valor, maximo: int = TEXTO_MAX) -> str:
    return valor[:maximo] if isinstance(valor, str) else ""


def _https(valor) -> str:
    v = _texto(valor)
    return v if v.startswith("https://") else ""


def _lista(valor, maximo_itens: int = 20) -> list[str]:
    if not isinstance(valor, list):
        return []
    return [x[:100] for x in valor[:maximo_itens] if isinstance(x, str) and x.strip()]


def _credenciais(valor) -> list[dict]:
    if not isinstance(valor, list):
        return []
    saida = []
    for c in valor[:20]:
        if isinstance(c, dict):
            saida.append({
                "tipo": _texto(c.get("tipo"), 100),
                "emissor": _texto(c.get("emissor"), 100),
                "valida_ate": _texto(c.get("valida_ate"), 32),
            })
    return saida


def _entrada(e: dict) -> dict:
    return {
        "fingerprint": _texto(e.get("fingerprint"), 32),
        "nome": _texto(e.get("nome")),
        "firma": _texto(e.get("firma")),
        "site": _https(e.get("site")),
        "apresentacao": _texto(e.get("apresentacao"), APRESENTACAO_MAX),
        "regioes": _lista(e.get("regioes")),
        "idiomas": _lista(e.get("idiomas")),
        "credenciais": _credenciais(e.get("credenciais")),
        "email": _texto(e.get("email")) or None,
        "telefone": _texto(e.get("telefone"), 40) or None,
        "atestacao": _texto(e.get("atestacao"), 32),
        "url": _https(e.get("url")) or None,
    }


def procurar(q: str = "", regiao: str = "", idioma: str = "") -> dict:
    """Consulta o diretório com os filtros dados. Devolve `configurado=False`
    (sem pedido) quando a instalação não tem URL; levanta
    `DiretorioIndisponivel` se o serviço falhar."""
    settings = get_settings()
    url = (settings.DIRETORIO_AUDITORES_URL or "").strip()
    if not url:
        return {"configurado": False, "total": 0, "auditores": []}

    filtros = {"q": q.strip()[:100], "regiao": regiao.strip()[:100], "idioma": idioma.strip()[:20]}
    params = urllib.parse.urlencode({k: v for k, v in filtros.items() if v})
    if params:
        url = url + ("&" if "?" in url else "?") + params
    pedido = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": f"NIS2PME/{settings.APP_VERSION}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 (URL da configuração, https)
            pedido, timeout=8, context=contexto_fornecedor()
        ) as resp:
            dados = json.loads(resp.read(RESPOSTA_MAX).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — a página diz "indisponível", a app segue
        logger.info("diretório de auditores indisponível: %s", exc)
        raise DiretorioIndisponivel(str(exc)) from exc
    brutos = dados.get("auditores") if isinstance(dados, dict) else None
    if not isinstance(brutos, list):
        raise DiretorioIndisponivel("resposta sem lista de auditores")
    auditores = [_entrada(e) for e in brutos[:ENTRADAS_MAX] if isinstance(e, dict)]
    auditores = [a for a in auditores if a["nome"] and a["fingerprint"]]
    return {"configurado": True, "total": len(auditores), "auditores": auditores}
