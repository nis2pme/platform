"""
Verificação de atualizações (on-prem).

A app verifica periodicamente se existe uma versão mais recente. O pedido envia
apenas um identificador ANÓNIMO da instância (não derivado de dados da empresa),
a versão e o modo de deployment — nunca dados de clientes. Controlado por
VERIFY_UPDATES (a false, não sai qualquer pedido).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)

_INSTANCE_ID_FILE = Path("/app/data/instance_id")

# Estado em memória do último check (lido pelo endpoint /updates/status).
_estado: dict = {
    "latest_version": None,
    "security_critical": False,
    "notes_url": None,
    "verificado_em": None,
}


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


def _versao_tuplo(v: str) -> tuple:
    partes: list[int] = []
    for p in (v or "").strip().lstrip("v").split("."):
        digitos = "".join(ch for ch in p if ch.isdigit())
        partes.append(int(digitos) if digitos else 0)
    return tuple(partes)


def _ha_versao_mais_recente(atual: str, ultima: str | None) -> bool:
    if not ultima:
        return False
    try:
        return _versao_tuplo(ultima) > _versao_tuplo(atual)
    except Exception:  # noqa: BLE001
        return False


def verificar_updates_sync() -> None:
    """Faz o pedido de verificação (bloqueante; chamar via asyncio.to_thread)."""
    settings = get_settings()
    if not settings.VERIFY_UPDATES:
        return
    corpo = json.dumps({
        "instance_id": obter_instance_id(),
        "version": settings.APP_VERSION,
        "deployment_mode": settings.DEPLOYMENT_MODE,
    }).encode("utf-8")
    pedido = urllib.request.Request(
        settings.UPDATE_CHECK_URL,
        data=corpo,
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"NIS2PME/{settings.APP_VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(pedido, timeout=10) as resp:  # noqa: S310 (URL própria, https)
            dados = json.loads(resp.read().decode("utf-8"))
        _estado["latest_version"] = dados.get("latest_version")
        _estado["security_critical"] = bool(dados.get("security_critical", False))
        _estado["notes_url"] = dados.get("notes_url")
        _estado["verificado_em"] = datetime.now(timezone.utc).isoformat()
        logger.info("Verificação de atualizações: última versão = %s.", _estado["latest_version"])
    except Exception as exc:  # noqa: BLE001 — nunca bloquear/partir a app
        logger.debug("Verificação de atualizações falhou (ignorado): %s", exc)


def obter_estado() -> dict:
    """Estado para o endpoint /updates/status."""
    settings = get_settings()
    atual = settings.APP_VERSION
    ultima = _estado.get("latest_version")
    disponivel = bool(settings.VERIFY_UPDATES and _ha_versao_mais_recente(atual, ultima))
    return {
        "verificar_ativo": settings.VERIFY_UPDATES,
        "versao_atual": atual,
        "ultima_versao": ultima,
        "update_disponivel": disponivel,
        "security_critical": bool(_estado.get("security_critical")) if disponivel else False,
        "notes_url": _estado.get("notes_url") if disponivel else None,
    }
