"""
Criptografia do dossiê de auditoria (.nis2pme) — assinatura e chave da instância.

Estrutura de um ficheiro .nis2pme:

    NIS2PME1\\n                                 ← identificador do formato
    {"payload_b64":"…","sig":"…","pub":"…"}\\n  ← envelope (1 linha JSON)
    <corpo cifrado (binário)>

O envelope contém os metadados assinados (Ed25519). Assina-se e verifica-se
sobre os BYTES exatos de `payload_b64` — o leitor valida a assinatura sobre
esses bytes e só depois faz parse do JSON, sem reserialização (sem diferenças
de canonicalização entre implementações/linguagens).

A chave de assinatura pertence à INSTÂNCIA: é gerada no primeiro uso, fica em
/app/data (entra nos backups → sobrevive a restauros) e serve exclusivamente
para provar origem e integridade. A confidencialidade é sempre do corpo
cifrado (age) — uma chave pública nunca decifra nada.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from fastapi import HTTPException, status

# Identificador do formato (magic) — primeira linha de qualquer .nis2pme.
MAGIC = b"NIS2PME1\n"

# Prefixo do token de convite (o resto é base64url do envelope JSON assinado).
CONVITE_PREFIXO = "NIS2CONVITE1."

# Chave Ed25519 da instância (0600). Vive em data/ — entra nos backups.
_CHAVE_INSTANCIA_FILE = Path("/app/data/dossie-assinatura.json")
# Atestações por chave pública — a chave com que cada empresa assina os
# dossiês (0600, em data/, entra nos backups como a chave da instância).
_ATESTACOES_FILE = Path("/app/data/dossie-atestacoes.json")


def pub_mestra() -> str:
    """Chave pública mestra NIS2PME que valida atestações (base64url, 32
    bytes RAW). Vem da configuração da instalação (`NIS2PME_MESTRA_PUBKEY`,
    no compose) — sem ela, atestações ficam por verificar (nunca é gate: ver
    `estado_atestacao`)."""
    from app.config import get_settings

    return (get_settings().NIS2PME_MESTRA_PUBKEY or "").strip()


def b64u_encode(raw: bytes) -> str:
    """base64url sem padding."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _canonical(payload: dict) -> bytes:
    """Bytes determinísticos do payload — são ESTES os bytes assinados e
    transmitidos; o verificador valida exatamente sobre eles."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Chave da instância
# ---------------------------------------------------------------------------

def obter_chave_instancia() -> dict:
    """
    Devolve {"priv": …, "pub": …, "criado_em": …} (chaves RAW em base64url).
    No primeiro uso gera o par e guarda-o com permissões restritas. Um ficheiro
    ilegível é erro (nunca se regenera em silêncio — a chave pode já estar
    fixada/confiada por terceiros que receberam dossiês desta instância).
    """
    if _CHAVE_INSTANCIA_FILE.exists():
        try:
            dados = json.loads(_CHAVE_INSTANCIA_FILE.read_text(encoding="utf-8"))
            # Valida que a privada corresponde mesmo à pública guardada.
            priv = Ed25519PrivateKey.from_private_bytes(b64u_decode(dados["priv"]))
            pub_raw = priv.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
            if b64u_encode(pub_raw) != dados["pub"]:
                raise ValueError("chave pública não corresponde à privada")
            return dados
        except (OSError, ValueError, KeyError) as erro:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"codigo": "chave_instancia_corrompida"},
            ) from erro

    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    dados = {
        "priv": b64u_encode(priv_raw),
        "pub": b64u_encode(pub_raw),
        "criado_em": datetime.now(timezone.utc).isoformat(),
    }
    _CHAVE_INSTANCIA_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CHAVE_INSTANCIA_FILE.write_text(json.dumps(dados), encoding="utf-8")
    _CHAVE_INSTANCIA_FILE.chmod(0o600)
    return dados


def pub_instancia_se_existir() -> str | None:
    """A chave pública da instância, sem a gerar se ainda não existir."""
    if not _CHAVE_INSTANCIA_FILE.exists():
        return None
    try:
        return json.loads(_CHAVE_INSTANCIA_FILE.read_text(encoding="utf-8")).get("pub")
    except (OSError, ValueError):
        return None


def obter_chave_empresa(db, empresa) -> dict:
    """A chave Ed25519 com que ESTA empresa assina os dossiês.

    Uma por empresa, não por instalação: em SaaS uma chave partilhada faria
    todos os clientes apresentarem a mesma identidade ao auditor. Gera-se no
    primeiro uso e fica na linha da empresa, privada cifrada em repouso. Uma
    chave ilegível é erro — nunca se regenera em silêncio, porque pode já estar
    fixada por auditores que receberam dossiês desta empresa.

    Numa instalação com uma só empresa que já tinha a chave da instância, a
    migração que criou as colunas fê-la herdar essa chave; aqui só se gera o
    que ainda não existe.
    """
    from app.shared.pii import cifrar_pii, decifrar_pii

    if empresa.dossie_chave_pub and empresa.dossie_chave_priv:
        priv_b64 = decifrar_pii(empresa.dossie_chave_priv)
        try:
            if not priv_b64:
                raise ValueError("chave privada ilegível")
            priv = Ed25519PrivateKey.from_private_bytes(b64u_decode(priv_b64))
            pub_raw = priv.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
            if b64u_encode(pub_raw) != empresa.dossie_chave_pub:
                raise ValueError("chave pública não corresponde à privada")
        except (ValueError, KeyError) as erro:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"codigo": "chave_empresa_corrompida"},
            ) from erro
        return {
            "priv": priv_b64,
            "pub": empresa.dossie_chave_pub,
            "criado_em": (
                empresa.dossie_chave_criada_em.isoformat() if empresa.dossie_chave_criada_em else None
            ),
        }

    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    pub_raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    agora = datetime.now(timezone.utc)
    empresa.dossie_chave_priv = cifrar_pii(b64u_encode(priv_raw))
    empresa.dossie_chave_pub = b64u_encode(pub_raw)
    empresa.dossie_chave_criada_em = agora
    db.add(empresa)
    db.commit()
    db.refresh(empresa)
    return {"priv": b64u_encode(priv_raw), "pub": empresa.dossie_chave_pub, "criado_em": agora.isoformat()}


def fingerprint(pub_b64: str) -> str:
    """
    Impressão digital legível da chave pública (ex.: 7F3A-91C2-0B5D-E644):
    primeiros 8 bytes do SHA-256, em grupos de 4 hex. Serve para confirmação
    humana por outro canal (telefone) — igual ao modelo do SSH.
    """
    resumo = hashlib.sha256(b64u_decode(pub_b64)).hexdigest()[:16].upper()
    return "-".join(resumo[i : i + 4] for i in range(0, 16, 4))


# ---------------------------------------------------------------------------
# Envelope assinado
# ---------------------------------------------------------------------------

def assinar_envelope(
    payload: dict, priv_b64: str, pub_b64: str, atestacao: dict | None = None
) -> bytes:
    """Linha do envelope (JSON + \\n) com o payload assinado pela instância.

    `atestacao`, quando presente, é o envelope `{payload_b64,sig}` assinado
    pela chave MESTRA NIS2PME a dizer "esta chave pertence a alguém
    registado" (ver `guardar_atestacao`) — viaja ao lado da assinatura da
    instância, nunca dentro dela (são chaves e assinantes diferentes)."""
    raw = _canonical(payload)
    priv = Ed25519PrivateKey.from_private_bytes(b64u_decode(priv_b64))
    sig = priv.sign(raw)
    envelope = {
        "payload_b64": b64u_encode(raw),
        "sig": b64u_encode(sig),
        "pub": pub_b64,
    }
    if atestacao:
        envelope["atestacao"] = atestacao
    return (json.dumps(envelope, separators=(",", ":")) + "\n").encode("utf-8")


def pub_certificada(raiz_b64: str, cert: dict, fim: str, agora: int | None = None) -> str | None:
    """Cadeia raiz → chave do servidor: verifica o certificado `{payload_b64, sig}`
    com a raiz e devolve a pública certificada para `fim`; None se não confere,
    expirou ou não serve para esse fim."""
    import time as _time

    if not isinstance(cert, dict):
        return None
    agora_i = int(agora if agora is not None else _time.time())
    try:
        raiz = Ed25519PublicKey.from_public_bytes(b64u_decode(raiz_b64))
        raw = b64u_decode(str(cert["payload_b64"]))
        raiz.verify(b64u_decode(str(cert["sig"])), raw)
        p = json.loads(raw.decode("utf-8"))
        if p.get("v") != 1 or fim not in (p.get("fins") or []):
            return None
        if not (int(p["valido_de"]) <= agora_i <= int(p["valido_ate"])):
            return None
        pub = str(p["pub"])
        if len(b64u_decode(pub)) != 32:
            return None
        return pub
    except Exception:  # noqa: BLE001 — qualquer defeito = certificado inválido
        return None


def verificar_envelope(linha: bytes) -> dict | None:
    """
    Verifica a assinatura do envelope com a chave pública NELE incluída e
    devolve o payload (dict), ou None se inválido. Atenção: isto prova que o
    cabeçalho não foi adulterado e que quem o assinou detém a chave `pub` —
    confiar nessa chave (pinning/confirmação do fingerprint) é decisão do leitor.
    """
    try:
        env = json.loads(linha.decode("utf-8"))
        pub = Ed25519PublicKey.from_public_bytes(b64u_decode(env["pub"]))
        raw = b64u_decode(env["payload_b64"])
        pub.verify(b64u_decode(env["sig"]), raw)
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 — qualquer defeito = envelope inválido
        return None


# ---------------------------------------------------------------------------
# Atestação de chave (cadeia de confiança NIS2PME → instância, D5)
# ---------------------------------------------------------------------------
#
# Uma atestação é um envelope `{payload_b64,sig}` assinado pela chave MESTRA
# NIS2PME a dizer "a chave X está registada" (payload com um campo `chave`).
# Distribui-se fora de banda, tal como a licença — o admin copia a pública da
# UI, envia-a por um canal já existente, e cola de volta o blob devolvido.
# É o upgrade do fingerprint/TOFU: uma vez atestada, a chave passa a validar-
# -se sozinha, sem telefonema. Nunca é gate — a app funciona sem atestação.
#
# A mestra assina, com a mesma chave, as atestações das chaves com que as
# empresas assinam os dossiês e as das identidades de auditor; o `subject` diz
# qual é. Cada verificador exige o do seu titular: a chave de dossiê atestada de
# uma PME não passa por auditor atestado num convite, nem o contrário.
SUBJECT_DOSSIE = "dossie"
SUBJECT_AUDITOR = "auditor_identidade"


def verificar_atestacao(atestacao: dict | None, pub_titular: str, subject: str) -> str:
    """Estado da atestação de `pub_titular`, que tem de ser do tipo `subject`
    (`SUBJECT_DOSSIE` ou `SUBJECT_AUDITOR`). Devolve um texto informativo
    ("ausente"/"presente_nao_verificada"/"verificada") ou levanta `ValueError`
    se a atestação está PRESENTE mas é inconsistente (assinada por outra
    chave, adulterada, de outra chave ou de outro tipo de chave) — uma
    atestação errada anexada a um ficheiro é pior do que nenhuma, por isso
    rejeita-se em vez de degradar silenciosamente para TOFU."""
    if atestacao is None:
        return "ausente"
    mestra = pub_mestra()
    if not mestra:
        return "presente_nao_verificada"
    env = dict(atestacao)
    # Duas chaves: a atestação pode vir assinada pela chave do servidor, com o
    # certificado (assinado pela raiz, para `atestacao`) dentro do envelope.
    assinante = mestra
    cert = env.pop("cert", None)
    if cert is not None:
        assinante = pub_certificada(mestra, cert, "atestacao")
        if assinante is None:
            raise ValueError("certificado da chave do servidor inválido, expirado ou sem a finalidade")
    if env.setdefault("pub", assinante) != assinante:
        raise ValueError("atestação assinada por chave desconhecida")
    payload = verificar_envelope(json.dumps(env, separators=(",", ":")).encode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("assinatura da atestação inválida")
    if payload.get("chave") != pub_titular:
        raise ValueError("a atestação refere-se a outra chave")
    if payload.get("subject") != subject:
        raise ValueError("a atestação é de outro tipo de chave")
    return "verificada"


def _ler_atestacoes() -> dict:
    if not _ATESTACOES_FILE.exists():
        return {}
    try:
        dados = json.loads(_ATESTACOES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dados if isinstance(dados, dict) else {}


def guardar_atestacao(atestacao: dict, pub_titular: str) -> str:
    """Valida a atestação recebida (colada pelo admin) contra a chave com que
    a empresa assina os dossiês — a que a UI lhe mostra — e guarda-a para essa
    chave. Recusa qualquer atestação que não verifique: nunca se guarda algo
    não confirmável."""
    if not pub_mestra():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"codigo": "mestra_nao_configurada"},
        )
    try:
        estado = verificar_atestacao(atestacao, pub_titular, SUBJECT_DOSSIE)
    except ValueError as erro:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"codigo": "atestacao_invalida", "detalhe": str(erro)},
        ) from erro
    todas = _ler_atestacoes()
    todas[pub_titular] = atestacao
    temporario = _ATESTACOES_FILE.with_suffix(".tmp")
    temporario.write_text(json.dumps(todas), encoding="utf-8")
    temporario.chmod(0o600)
    temporario.replace(_ATESTACOES_FILE)
    return estado


def _atestacao_da_instancia() -> dict | None:
    """A atestação que versões anteriores guardavam junto da chave da instância."""
    if not _CHAVE_INSTANCIA_FILE.exists():
        return None
    try:
        dados = json.loads(_CHAVE_INSTANCIA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dados.get("atestacao")


def obter_atestacao(pub: str) -> dict | None:
    """A atestação guardada para esta chave pública, se existir."""
    atestacao = _ler_atestacoes().get(pub)
    if atestacao is None and pub == pub_instancia_se_existir():
        atestacao = _atestacao_da_instancia()
    return atestacao


def estado_atestacao(pub: str) -> str:
    """Estado legível da atestação desta chave, para a UI."""
    try:
        return verificar_atestacao(obter_atestacao(pub), pub, SUBJECT_DOSSIE)
    except ValueError:
        return "invalida"


# ---------------------------------------------------------------------------
# Convites do auditor (modo `convite` — D5)
# ---------------------------------------------------------------------------
#
# Um convite é o que o auditor entrega à PME (token colável/QR) para receber um
# dossiê cifrado sem partilhar segredos. Estrutura = o MESMO envelope assinado
# dos dossiês (`{payload_b64,sig,pub,atestacao?}`), só que o `pub` é a IDENTIDADE
# do auditor e o payload traz `tipo:"convite"` + um destinatário age efémero.
# A PME valida a assinatura (o convite viaja fora de banda e transporta a chave
# de cifra — sem assinatura, quem o troca troca a chave) e a atestação da
# identidade do auditor (upgrade do TOFU), e só então cifra o dossiê para o
# destinatário. A metade privada do destinatário vive na plataforma do auditor.


class ConviteInvalido(ValueError):
    """O token de convite é malformado, não verifica, ou é inconsistente."""


def verificar_convite(token: str) -> dict:
    """Descodifica e verifica um token de convite. Devolve os campos úteis
    (convite_id, destinatário age, identidade+atestação do auditor) ou levanta
    `ConviteInvalido`. NÃO decide a confiança na chave — isso é do chamador
    (atestação verificada = automático; senão confirmação do fingerprint)."""
    if not isinstance(token, str) or not token.startswith(CONVITE_PREFIXO):
        raise ConviteInvalido("token de convite não reconhecido")
    corpo = token[len(CONVITE_PREFIXO):].strip()
    try:
        envelope_raw = b64u_decode(corpo)
        envelope = json.loads(envelope_raw.decode("utf-8"))
        if not isinstance(envelope, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError) as erro:
        raise ConviteInvalido("token de convite ilegível") from erro

    # Assinatura da identidade do auditor sobre o payload (bytes exatos).
    payload = verificar_envelope(json.dumps(envelope, separators=(",", ":")).encode("utf-8"))
    if payload is None:
        raise ConviteInvalido("assinatura do convite inválida")
    if payload.get("tipo") != "convite":
        raise ConviteInvalido("o token não é um convite")
    convite_id = payload.get("convite_id")
    destinatario = payload.get("destinatario_age")
    if not isinstance(convite_id, str) or not convite_id:
        raise ConviteInvalido("convite sem identificador")
    if not isinstance(destinatario, str) or not destinatario.startswith("age1"):
        raise ConviteInvalido("convite sem destinatário de cifra válido")

    auditor_pub = envelope["pub"]
    # Atestação da IDENTIDADE do auditor (envelope-level, como nos dossiês).
    try:
        estado_atest = verificar_atestacao(envelope.get("atestacao"), auditor_pub, SUBJECT_AUDITOR)
    except ValueError as erro:
        raise ConviteInvalido(f"atestação do convite inválida: {erro}") from erro

    # Bloco de relay OPCIONAL. Vem assinado dentro do payload — se estiver
    # presente mas malformado, é convite inválido (fail-closed): quem confia no
    # convite tem de confiar no destino de upload que ele indica.
    relay = _extrair_relay(payload.get("relay"))

    return {
        "convite_id": convite_id,
        "destinatario_age": destinatario,
        "auditor_pub": auditor_pub,
        "auditor_fingerprint": fingerprint(auditor_pub),
        "auditor_nome": payload.get("auditor_nome"),
        "atestacao_estado": estado_atest,
        "uso_unico": bool(payload.get("uso_unico", True)),
        "relay": relay,
    }


def _extrair_relay(bloco) -> dict | None:
    """Valida o bloco relay assinado do convite. None se ausente; levanta
    `ConviteInvalido` se presente mas incoerente. O transporte exige TLS —
    `http://` só é aceite para endereços de loopback (desenvolvimento/teste):
    sem TLS, o `token_upload` viajaria em claro na rede."""
    if bloco is None:
        return None
    if not isinstance(bloco, dict):
        raise ConviteInvalido("bloco relay malformado")
    url = bloco.get("url")
    convite_id = bloco.get("convite_id")
    token_upload = bloco.get("token_upload")
    if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
        raise ConviteInvalido("relay sem URL válido")
    if url.startswith("http://"):
        from urllib.parse import urlparse
        anfitriao = (urlparse(url).hostname or "").lower()
        if anfitriao not in ("localhost", "127.0.0.1", "::1"):
            raise ConviteInvalido("relay sem TLS (https obrigatório)")
    if not (isinstance(convite_id, str) and convite_id):
        raise ConviteInvalido("relay sem identificador")
    if not (isinstance(token_upload, str) and token_upload):
        raise ConviteInvalido("relay sem token de upload")
    return {"url": url.rstrip("/"), "convite_id": convite_id, "token_upload": token_upload}
