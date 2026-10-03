"""
Harness de conformidade do formato .nis2pme (corre com `--auto-teste`).

Constrói um dossiê sintético de forma INDEPENDENTE do exportador — só a
partir da especificação — e verifica que o verificador o aceita; depois
adultera o ficheiro de várias formas e verifica que cada camada rejeita o
que lhe compete. Se a construção independente e o verificador concordarem,
o formato está a ser cumprido pelos dois lados.

Inclui o cenário mais forte: um atacante que conhece a passphrase e assina
com a SUA chave consegue passar as camadas de assinatura e de hash do corpo
— é o manifest (cifrado) que denuncia a entrada alterada, e é o fingerprint
(pinning/confirmação fora de banda) que denuncia a chave trocada.
"""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app.dossie import crypto, keyslot
from app.dossie.verificar import FalhaVerificacao, verificar_ficheiro

_PASSPHRASE = "passphrase-de-conformidade-123"


# ---------------------------------------------------------------------------
# Construção independente (só a partir da especificação)
# ---------------------------------------------------------------------------

def _gerar_par() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return crypto.b64u_encode(priv_raw), crypto.b64u_encode(pub_raw)


def _construir(destino: Path, priv_b64: str, pub_b64: str) -> dict:
    """Escreve um .nis2pme sintético válido. Devolve as peças para os ataques."""
    import pyrage

    identidade = pyrage.x25519.Identity.generate()
    destinatario = identidade.to_public()

    ev_id = str(uuid.uuid4())
    ev_claro = b"conteudo de evidencia sintetica " * 512
    ev_cifrado = pyrage.encrypt(ev_claro, [destinatario])

    dados_json = {
        "dados/empresa.json": json.dumps({"nome": "Empresa Sintetica"}).encode(),
        "dados/conformidade.json": json.dumps({"score": 2.5}).encode(),
    }
    entradas = {
        nome: {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}
        for nome, b in dados_json.items()
    }
    entradas[f"ev/{ev_id}.age"] = {
        "sha256": hashlib.sha256(ev_cifrado).hexdigest(), "bytes": len(ev_cifrado),
    }

    dossie_id = str(uuid.uuid4())
    manifest = {
        "schema_versao": 1,
        "tipo": "dossie",
        "dossie_id": dossie_id,
        "gerado_em": datetime.now(timezone.utc).isoformat(),
        "empresa": {"id": str(uuid.uuid4()), "nome": "Empresa Sintetica"},
        "gerado_por": {"nome": "Harness"},
        "contagens": {"controlos": 2, "evidencias": 1},
        "entradas": entradas,
        "evidencias_ficheiros": {
            ev_id: {"nome": "prova.txt", "sha256": hashlib.sha256(ev_claro).hexdigest(),
                    "bytes": len(ev_claro)},
        },
    }

    interno = io.BytesIO()
    with zipfile.ZipFile(interno, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
        for nome, b in dados_json.items():
            z.writestr(nome, b)

    corpo_io = io.BytesIO()
    with zipfile.ZipFile(corpo_io, "w", zipfile.ZIP_STORED) as z:
        z.writestr(f"ev/{ev_id}.age", ev_cifrado)
        z.writestr("dados.age", pyrage.encrypt(interno.getvalue(), [destinatario]))
        # Keyslot: destinatário scrypt do age com o custo que a especificação
        # fixa (o `pyrage.passphrase` escolhe-o pelo CPU).
        z.writestr("chave.age", keyslot.cifrar(str(identidade).encode(), _PASSPHRASE))
    corpo = corpo_io.getvalue()

    payload = {
        "formato": "nis2pme",
        "versao": 1,
        "tipo": "dossie",
        "dossie_id": dossie_id,
        "criado_em": datetime.now(timezone.utc).isoformat(),
        "app_version": "harness",
        "cifra": {"alg": "age-v1", "modo": "passphrase"},
        "corpo": {"sha256": hashlib.sha256(corpo).hexdigest(), "bytes": len(corpo)},
    }
    envelope = crypto.assinar_envelope(payload, priv_b64, pub_b64)
    destino.write_bytes(crypto.MAGIC + envelope + corpo)
    return {
        "payload": payload, "corpo": corpo, "ev_id": ev_id, "dossie_id": dossie_id,
        "identidade": str(identidade),
    }


def _reescrever(destino: Path, payload: dict, corpo: bytes, priv_b64: str, pub_b64: str) -> None:
    """Reconstrói o ficheiro com um payload/corpo dados (simula um atacante
    que recalcula o hash do corpo e assina com a chave DELE)."""
    payload = dict(payload)
    payload["corpo"] = {"sha256": hashlib.sha256(corpo).hexdigest(), "bytes": len(corpo)}
    destino.write_bytes(
        crypto.MAGIC + crypto.assinar_envelope(payload, priv_b64, pub_b64) + corpo
    )


def _com_custo(chave_age: bytes, n_log2: int) -> bytes:
    """O mesmo keyslot a anunciar outro custo de scrypt no cabeçalho (sem o
    derivar: o MAC deixa de conferir, mas um leitor sem teto pagava o custo
    antes de chegar a ele)."""
    versao, stanza, resto = chave_age.split(b"\n", 2)
    return b"\n".join([versao, stanza.rsplit(b" ", 1)[0] + b" " + str(n_log2).encode(), resto])


def _substituir_entrada(
    corpo: bytes, nome: str, novo: bytes | None, extra: tuple[str, bytes] | None = None,
    metodo: int = zipfile.ZIP_STORED,
) -> bytes:
    """Novo corpo ZIP com uma entrada substituída/removida (novo=None) e/ou extra."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(corpo)) as zin, zipfile.ZipFile(out, "w", metodo) as zout:
        for info in zin.infolist():
            if info.filename == nome:
                if novo is not None:
                    zout.writestr(nome, novo)
                continue
            zout.writestr(info.filename, zin.read(info.filename))
        if extra is not None:
            zout.writestr(extra[0], extra[1])
    return out.getvalue()


# ---------------------------------------------------------------------------
# Casos
# ---------------------------------------------------------------------------

def correr() -> int:
    resultados: list[tuple[str, bool, str]] = []

    def caso(nome: str, fn, camada_esperada: str | None) -> None:
        """camada_esperada=None → tem de PASSAR; senão tem de falhar NESSA camada."""
        try:
            fn()
            ok, detalhe = camada_esperada is None, "aceite"
        except FalhaVerificacao as falha:
            ok = falha.camada == camada_esperada
            detalhe = f"rejeitado na camada '{falha.camada}': {falha}"
        resultados.append((nome, ok, detalhe))

    with tempfile.TemporaryDirectory(prefix="nis2pme-teste-") as tmp:
        f = Path(tmp) / "sintetico.nis2pme"
        priv, pub = _gerar_par()
        pecas = _construir(f, priv, pub)
        atacante_priv, atacante_pub = _gerar_par()

        # 1–2: o ficheiro válido passa, com e sem passphrase.
        caso("válido sem passphrase", lambda: verificar_ficheiro(f, None), None)
        caso("válido com passphrase", lambda: verificar_ficheiro(f, _PASSPHRASE), None)

        # 3: magic errado.
        g = Path(tmp) / "magic.nis2pme"
        g.write_bytes(b"XIS2PME1\n" + f.read_bytes()[len(crypto.MAGIC):])
        caso("magic adulterado", lambda: verificar_ficheiro(g, None), "formato")

        # 4: um byte trocado no cabeçalho assinado.
        bruto = bytearray(f.read_bytes())
        pos = bruto.index(b'"payload_b64"') + 20
        bruto[pos] ^= 0x01
        g.write_bytes(bytes(bruto))
        caso("cabeçalho adulterado", lambda: verificar_ficheiro(g, None), "assinatura")

        # 5: um byte trocado no corpo (sem tocar no cabeçalho).
        bruto = bytearray(f.read_bytes())
        bruto[-10] ^= 0x01
        g.write_bytes(bytes(bruto))
        caso("corpo adulterado", lambda: verificar_ficheiro(g, None), "corpo")

        # 6: passphrase errada.
        caso(
            "passphrase errada",
            lambda: verificar_ficheiro(f, "errada-mas-comprida-123"), "cifra",
        )

        # 7: atacante com a passphrase troca uma evidência, recalcula o hash do
        # corpo e assina com a chave DELE — só o manifest cifrado o denuncia.
        corpo_mau = _substituir_entrada(
            pecas["corpo"], f"ev/{pecas['ev_id']}.age", b"ciphertext forjado"
        )
        _reescrever(g, pecas["payload"], corpo_mau, atacante_priv, atacante_pub)
        caso("evidência trocada + reassinado", lambda: verificar_ficheiro(g, _PASSPHRASE), "manifest")

        # 8: entrada extra não declarada no manifest.
        corpo_mau = _substituir_entrada(
            pecas["corpo"], "nao-existe", None,
            extra=(f"ev/{uuid.uuid4()}.age", b"entrada intrusa"),
        )
        _reescrever(g, pecas["payload"], corpo_mau, atacante_priv, atacante_pub)
        caso("entrada extra no corpo", lambda: verificar_ficheiro(g, _PASSPHRASE), "manifest")

        # 9: nome de entrada malicioso (path traversal) é barrado pela estrutura.
        corpo_mau = _substituir_entrada(
            pecas["corpo"], "nao-existe", None, extra=("../evil.sh", b"x")
        )
        _reescrever(g, pecas["payload"], corpo_mau, atacante_priv, atacante_pub)
        caso("nome de entrada malicioso", lambda: verificar_ficheiro(g, _PASSPHRASE), "corpo")

        # 10: dossie_id do cabeçalho trocado — o manifest (assinado pela cifra)
        # deixa de bater certo.
        payload_mau = dict(pecas["payload"], dossie_id=str(uuid.uuid4()))
        _reescrever(g, payload_mau, pecas["corpo"], atacante_priv, atacante_pub)
        caso("dossie_id do cabeçalho trocado", lambda: verificar_ficheiro(g, _PASSPHRASE), "manifest")

        # 11: transporte comprimido (DEFLATE) — a porta de uma bomba de
        # descompressão; o formato manda STORED.
        corpo_mau = _substituir_entrada(pecas["corpo"], "nao-existe", None, metodo=zipfile.ZIP_DEFLATED)
        _reescrever(g, pecas["payload"], corpo_mau, atacante_priv, atacante_pub)
        caso("transporte comprimido", lambda: verificar_ficheiro(g, _PASSPHRASE), "corpo")

        # 12: keyslot com um custo de scrypt acima do aceite — recusado pelo
        # cabeçalho, antes de derivar (o custo é escolhido por quem gera, e a
        # derivação paga-se antes de o MAC do cabeçalho se poder conferir).
        chave_cara = _com_custo(
            keyslot.cifrar(pecas["identidade"].encode(), _PASSPHRASE), keyslot.N_LOG2_MAX + 1
        )
        corpo_mau = _substituir_entrada(pecas["corpo"], "chave.age", chave_cara)
        _reescrever(g, pecas["payload"], corpo_mau, atacante_priv, atacante_pub)
        caso("custo do scrypt acima do aceite", lambda: verificar_ficheiro(g, _PASSPHRASE), "cifra")

    falhas = [r for r in resultados if not r[1]]
    for nome, ok, detalhe in resultados:
        print(f"{'[OK]' if ok else '[X]'} {nome} — {detalhe}")
    print(f"\n{len(resultados) - len(falhas)}/{len(resultados)} casos de conformidade OK")
    return 1 if falhas else 0
