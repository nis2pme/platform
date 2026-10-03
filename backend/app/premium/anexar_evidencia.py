"""Suporte ao "anexar como evidência" dos módulos premium.

O documento-evidência é gerado no sidecar (payload localizado) e o PDF é feito no
cliente. Para não anexar o mesmo documento repetido (sem alterações) — poupando
processamento e disco —, o core calcula uma impressão digital ESTÁVEL do conteúdo
(sem a data de geração, que muda sempre) e resolve qual o controlo do tenant que o
documento evidencia. O frontend usa isto para saltar a geração/anexação quando nada
mudou desde a última vez.
"""
from __future__ import annotations

import hashlib
import uuid

from sqlmodel import Session

from app.empresas.models import Empresa
from app.evidencias.service import _duplicado_mais_recente, selar_hash_documento
from app.frameworks.runtime import (
    load_company_control_rows,
    resolver_framework_empresa,
)


def hash_documento(doc: dict) -> str:
    """Impressão digital SHA-256 estável do conteúdo do documento.

    Cobre título, subtítulo e todas as secções (prosa + tabelas), mas NÃO a
    `data_geracao` — assim, o mesmo documento gerado noutro dia dá o mesmo hash.
    """
    partes: list[str] = [doc.get("titulo", ""), doc.get("subtitulo", "")]
    for s in doc.get("secoes", []) or []:
        partes.append(s.get("titulo", ""))
        partes.append(s.get("texto", ""))
        partes.extend(s.get("cabecalho", []) or [])
        for linha in s.get("linhas", []) or []:
            partes.extend(linha)
    blob = "\x00".join(str(p) for p in partes)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _resolver_controlo(
    db: Session, empresa: Empresa, codigos: list[str]
) -> uuid.UUID | None:
    """Primeiro controlo do tenant (por ordem dos códigos) que o documento evidencia."""
    if not codigos:
        return None
    framework = resolver_framework_empresa(db, empresa)
    rows = load_company_control_rows(db, empresa.id, framework.id)
    por_codigo = {row.control.code: row.ce.id for row in rows}
    for codigo in codigos:
        if codigo in por_codigo:
            return por_codigo[codigo]
    return None


def enriquecer_documento(db: Session, empresa: Empresa, doc: dict) -> dict:
    """Acrescenta ao payload os campos do "anexar como evidência":
    - `conteudo_hash`: impressão digital estável (para deduplicar);
    - `controlo_empresa_id`: o controlo do tenant a que anexar (ou None);
    - `ja_anexado`: True se a evidência MAIS RECENTE desse controlo já tem este conteúdo.
    """
    conteudo_hash = hash_documento(doc)
    ce_id = _resolver_controlo(db, empresa, doc.get("controlos", []) or [])
    ja_anexado = (
        _duplicado_mais_recente(db, empresa.id, ce_id, conteudo_hash) is not None
        if ce_id is not None
        else False
    )
    # Selada: o browser devolve-a no upload, e sem selo o núcleo não a aceita
    # (calcula a dos bytes, que mudam a cada geração).
    doc["conteudo_hash"] = (
        selar_hash_documento(empresa.id, ce_id, conteudo_hash) if ce_id is not None else conteudo_hash
    )
    doc["controlo_empresa_id"] = str(ce_id) if ce_id else None
    doc["ja_anexado"] = ja_anexado
    return doc
