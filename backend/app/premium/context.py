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
from app.evidencias.models import Evidencia, EvidenciaRequisito
from app.evidencias.service import _decifrar_texto_evidencia
from app.frameworks.models import Control, ControloEmpresaV2, Framework
from app.frameworks.runtime import load_thresholds_map, resolver_framework_empresa
from app.premium.sealing import cifrar_envelope, maximo_em_claro
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


# O que o gateway lê quando uma evidência (ou parte dela) não coube no envelope.
_MARCA_EXCLUIDO = "limite de tamanho do payload atingido"


def _json(valor) -> bytes:
    return json.dumps(valor, ensure_ascii=False).encode("utf-8")


def _base64_maximo(ev: Evidencia) -> int:
    """Até quantos bytes ocupa o ficheiro da evidência em base64, sem o ler.

    Cifrado, o ficheiro em disco é um token Fernet: o próprio conteúdo cifrado já
    em base64, com ~57 bytes a mais. Por isso o tamanho em disco é um majorante
    do base64 do conteúdo decifrado. Em claro, é o base64 do próprio ficheiro."""
    tamanho = os.path.getsize(ev.ficheiro_path)
    if ev.ficheiro_cifrado and settings.EVIDENCE_ENCRYPTION_KEY:
        return tamanho
    return 4 * -(-tamanho // 3)


def _item_da_evidencia(ev: Evidencia, cabe: int) -> list[bytes] | None:
    """As partes do item JSON da evidência, que somam no máximo `cabe` bytes.

    Entra o texto se couber; depois o ficheiro, se couber no que sobra — e um
    ficheiro que não cabe nem sequer se lê. O que fica de fora é marcado, para o
    modelo saber que a evidência existe. None se nem o item mínimo couber."""
    partes = [b'{"tipo":' + _json(ev.tipo.value) + b',"titulo":' + _json(ev.titulo)]
    tamanho = len(partes[0]) + 1  # + o "}" do fim
    marca_ficheiro = b',"ficheiro_excluido":' + _json(_MARCA_EXCLUIDO)
    marca_texto = b',"conteudo_excluido":' + _json(_MARCA_EXCLUIDO)
    tem_ficheiro = bool(ev.ficheiro_path) and os.path.isfile(ev.ficheiro_path)
    reserva = len(marca_ficheiro) if tem_ficheiro else 0
    # O mínimo: o cabeçalho, e as marcas do que pode ficar de fora.
    if tamanho + reserva + (len(marca_texto) if ev.conteudo_texto else 0) > cabe:
        return None

    if ev.conteudo_texto:
        texto = ev.conteudo_texto
        if ev.conteudo_texto_cifrado and settings.EVIDENCE_ENCRYPTION_KEY:
            texto = _decifrar_texto_evidencia(texto)
        valor = _json(texto)
        del texto
        nome = b',"conteudo_texto":'
        if tamanho + len(nome) + len(valor) + reserva <= cabe:
            partes += [nome, valor]
            tamanho += len(nome) + len(valor)
        else:
            partes.append(marca_texto)
            tamanho += len(marca_texto)
        del valor

    if tem_ficheiro:
        cabeca = (b',"ficheiro_nome":' + _json(decifrar_pii(ev.ficheiro_nome))
                  + b',"ficheiro_tipo":' + _json(ev.ficheiro_tipo) + b',"ficheiro_base64":"')
        if tamanho + len(cabeca) + _base64_maximo(ev) + 1 <= cabe:
            with open(ev.ficheiro_path, "rb") as fh:
                raw = fh.read()
            if ev.ficheiro_cifrado and settings.EVIDENCE_ENCRYPTION_KEY:
                from app.evidencias.service import decifrar_bytes_evidencia

                raw = decifrar_bytes_evidencia(raw)
            partes += [cabeca, base64.b64encode(raw), b'"']
            del raw
        else:
            partes.append(marca_ficheiro)

    partes.append(b"}")
    return partes


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
    não-determinístico). Evidências tal como estão (texto + ficheiros), pela
    ordem em que foram criadas, até ao que cabe no envelope.
    """
    # As evidências de um controlo são as que lhe estão LIGADAS. Pela coluna
    # antiga, a análise recebia menos provas do que o controlo tem — e concluiria
    # sobre um controlo sem ver a política partilhada que o sustenta. A ordem é
    # fixa: a mesma lista dá o mesmo payload e a mesma chave de idempotência (sem
    # ORDER BY, o Postgres pode devolver as linhas por outra ordem, e a dedup
    # falhava).
    evidencias = db.exec(
        select(Evidencia)
        .join(EvidenciaRequisito, EvidenciaRequisito.evidencia_id == Evidencia.id)
        .where(
            EvidenciaRequisito.requisito_id == ce.id,
            EvidenciaRequisito.desligado_em.is_(None),
            Evidencia.empresa_id == empresa.id,
            Evidencia.deleted_at.is_(None),
        )
        .order_by(Evidencia.created_at, Evidencia.id)
    ).all()

    # O payload monta-se já em bytes, item a item, e conta-se à medida: o que
    # já não cabe no envelope fica de fora (e marcado) — sem isto, o envelope
    # passava dos tetos do sidecar, do gateway e da borda, e a análise falhava
    # depois de gastar memória a montá-lo. Nenhuma evidência pequena fica de
    # fora por causa de uma grande que veio antes.
    inicio = b'{"controlo_codigo":' + _json(controlo_codigo) + b',"evidencias":['
    fim = b"]}"
    partes = [inicio]
    usado = len(inicio) + len(fim)
    teto = maximo_em_claro()
    for n, ev in enumerate(evidencias):
        separador = b"," if n else b""
        item = _item_da_evidencia(ev, teto - usado - len(separador))
        if item is None:
            break
        if separador:
            partes.append(separador)
        partes += item
        usado += len(separador) + sum(len(p) for p in item)
    partes.append(fim)
    return b"".join(partes)


def _idempotency_key(ce_id: uuid.UUID, payload_plaintext: bytes) -> str:
    """
    Chave de idempotência gerada pelo core: controlo + impressão do conteúdo. Reenvio
    do mesmo controlo com as mesmas evidências → o sidecar/gateway desduplicam (sem
    re-correr o LLM). Evidências alteradas mudam a chave → nova análise (correto).

    A chave sai da instalação (vai ao gateway do fornecedor): é um HMAC com um
    pepper local e não um SHA-256 simples, para que quem a veja não possa confirmar
    se umas evidências que adivinhe são as desta empresa.
    """
    import hmac

    from app.shared.hashes import _get_pepper

    pepper = _get_pepper(b"ia-idempotencia")
    h = hmac.new(pepper, digestmod=hashlib.sha256) if pepper else hashlib.sha256()
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
