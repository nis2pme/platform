"""
Provedor de contexto do open-core para os módulos premium (custódia de dados).

Monta o contexto que o core entrega ao sidecar para uma análise de um controlo:
metadados não-PII + o payload de evidências (LIDO e DECIFRADO do core-db, porque só
o core tem as chaves) e sela-o em envelope antes de sair. Não decide nada sobre a
análise — isso é do sidecar; aqui só se reúnem e protegem os dados do cliente.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid

from fastapi import HTTPException
from sqlmodel import Session, select

from app.config import get_settings
from app.empresas.models import Empresa
from app.evidencias.models import Evidencia
from app.evidencias.service import _decifrar_texto_evidencia
from app.frameworks.models import Control, ControloEmpresaV2, Framework
from app.frameworks.runtime import load_thresholds_map, resolver_framework_empresa
from app.premium.sealing import cifrar_envelope
from app.shared.pii import decifrar_pii
from app.shared.utils import resolver_locale

settings = get_settings()


def get_ce_or_404(
    db: Session, controlo_empresa_id: uuid.UUID, empresa_id: uuid.UUID
) -> ControloEmpresaV2:
    ce = db.get(ControloEmpresaV2, controlo_empresa_id)
    if not ce or ce.empresa_id != empresa_id:
        raise HTTPException(status_code=404, detail={"codigo": "controlo_nao_encontrado"})
    return ce


def _construir_meta(
    db: Session,
    ce: ControloEmpresaV2,
    empresa: Empresa,
    framework: Framework,
    locale: str,
) -> dict:
    """Metadados estruturados (não-PII) do contexto.

    Identifica apenas o framework, o controlo e o nível de conformidade exigido — o
    servidor resolve título/descrição/exemplos de evidência a partir do framework.
    NÃO inclui estado de implementação da entidade (gaps ou nível atual): só as
    evidências submetidas são analisadas, minimizando a informação sensível enviada.
    """
    control = db.get(Control, ce.control_id)
    thresholds = load_thresholds_map(db, framework, empresa)
    nivel_minimo = thresholds.get(ce.control_id, framework.maturity_scale_min)

    return {
        "tenant_id": str(empresa.id),
        "controlo_empresa_id": str(ce.id),
        "framework_id": framework.registry_id,
        "controlo_codigo": control.code if control else "",
        "nivel_minimo": int(nivel_minimo),
        "locale": locale,
    }


def _construir_payload_evidencias(
    db: Session,
    ce: ControloEmpresaV2,
    empresa: Empresa,
    controlo_codigo: str,
) -> bytes:
    """
    Monta o payload de evidências (decifra do DB) e devolve-o **em claro**.

    O caller deriva a `idempotency_key` deste plaintext (conteúdo estável) e só
    depois sela em envelope — hashear o ciphertext não serviria (o sealed box é
    não-determinístico). Evidências tal como estão (texto + ficheiros).
    """
    evidencias = db.exec(
        select(Evidencia).where(
            Evidencia.controlo_empresa_v2_id == ce.id,
            Evidencia.empresa_id == empresa.id,
            Evidencia.deleted_at.is_(None),
        )
    ).all()

    items: list[dict] = []
    total_bytes = 0
    limite = settings.PREMIUM_EVIDENCE_MAX_BYTES

    for ev in evidencias:
        item: dict = {"tipo": ev.tipo.value, "titulo": ev.titulo}

        if ev.conteudo_texto:
            texto = ev.conteudo_texto
            if ev.conteudo_texto_cifrado and settings.EVIDENCE_ENCRYPTION_KEY:
                texto = _decifrar_texto_evidencia(texto)
            item["conteudo_texto"] = texto

        if ev.ficheiro_path and os.path.isfile(ev.ficheiro_path):
            with open(ev.ficheiro_path, "rb") as fh:
                raw = fh.read()
            if ev.ficheiro_cifrado and settings.EVIDENCE_ENCRYPTION_KEY:
                from cryptography.fernet import Fernet

                raw = Fernet(settings.EVIDENCE_ENCRYPTION_KEY.encode()).decrypt(raw)
            total_bytes += len(raw)
            if total_bytes > limite:
                item["ficheiro_excluido"] = "limite de tamanho do payload atingido"
            else:
                item["ficheiro_nome"] = decifrar_pii(ev.ficheiro_nome)
                item["ficheiro_tipo"] = ev.ficheiro_tipo
                item["ficheiro_base64"] = base64.b64encode(raw).decode("ascii")

        items.append(item)

    return json.dumps(
        {"controlo_codigo": controlo_codigo, "evidencias": items}, ensure_ascii=False
    ).encode("utf-8")


def _idempotency_key(ce_id: uuid.UUID, payload_plaintext: bytes) -> str:
    """
    Chave de idempotência gerada pelo core: controlo + hash do conteúdo. Reenvio do
    mesmo controlo com as mesmas evidências → o sidecar/gateway desduplicam (sem
    re-correr o LLM). Evidências alteradas mudam a chave → nova análise (correto).
    """
    h = hashlib.sha256()
    h.update(str(ce_id).encode("utf-8"))
    h.update(b":")
    h.update(payload_plaintext)
    return h.hexdigest()


def construir_contexto_controlo(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    empresa: Empresa,
) -> tuple[dict, bytes]:
    """
    Reúne o contexto de um controlo e sela as evidências.

    Devolve `(meta, evidencias_blob)` onde `meta` já inclui a `idempotency_key` e
    `evidencias_blob` é o payload SELADO em envelope (pronto para o sidecar). Levanta
    404 se o controlo não pertencer à empresa.
    """
    ce = get_ce_or_404(db, controlo_empresa_id, empresa.id)
    framework = resolver_framework_empresa(db, empresa)
    locale = resolver_locale(empresa, framework)

    meta = _construir_meta(db, ce, empresa, framework, locale)
    payload_plaintext = _construir_payload_evidencias(db, ce, empresa, meta["controlo_codigo"])
    meta["idempotency_key"] = _idempotency_key(ce.id, payload_plaintext)
    evidencias_blob = cifrar_envelope(payload_plaintext)
    return meta, evidencias_blob
