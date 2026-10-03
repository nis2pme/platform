"""
Geração do dossiê de auditoria (.nis2pme).

Pipeline: conteúdo (JSON) → zip interno → cifra (age) → ZIP de transporte →
envelope assinado → ficheiro final em streaming. Nada persiste no servidor
além do registo em `dossies_gerados` (sem conteúdo).

Corpo do ficheiro (ZIP de transporte, entradas sem compressão — já são
ciphertext; nomes opacos, nenhum metadado em claro além do nº e tamanho
aproximado das evidências):

    chave.age      identidade age do dossiê (aleatória, única por dossiê),
                   cifrada para o destinatário real. Nesta fase o destinatário
                   é uma passphrase (scrypt); decifra-se UMA vez e abre o resto.
    dados.age      zip com manifest.json + dados/*.json, cifrado para a
                   identidade do dossiê — pequeno, decifra direto em memória.
    ev/<uuid>.age  cada ficheiro de evidência cifrado INDIVIDUALMENTE para a
                   identidade do dossiê — processado um a um na geração (nunca
                   todos em memória) e decifrável a pedido na leitura.

O manifest inclui ainda o destinatário de resposta: uma chave age efémera,
gerada por dossiê, para a qual um auditor poderá um dia cifrar o parecer —
só esta instância (que guarda a privada, cifrada em repouso) o consegue abrir.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import shutil
import tempfile
import unicodedata
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pyrage
from fastapi import HTTPException, Request, status
from sqlalchemy import func
from sqlmodel import Session, select

from app.auth.models import Utilizador
from app.dossie import conteudo as conteudo_mod
from app.dossie import crypto, keyslot
from app.dossie.models import AuditorConfiavel, DossieGerado
from app.empresas.models import Empresa
from app.evidencias.models import Evidencia
from app.shared.concorrencia import LIMITE_OPERACOES_PESADAS
from app.shared.pii import cifrar_pii, decifrar_pii

logger = logging.getLogger(__name__)

# Versões do formato: `formato_versao` = container/envelope; `schema_versao`
# = layout dos JSON. Só incrementam com alterações incompatíveis — campos
# novos são sempre aditivos e o leitor aceita versões anteriores.
FORMATO_VERSAO = 1
SCHEMA_VERSAO = 1

PASSPHRASE_MIN = 12

# Períodos aceites para o extrato de atividade (None = desde sempre).
PERIODOS_ATIVIDADE = (3, 6, 12, None)


def _slug(nome: str | None) -> str:
    """Nome da empresa → parte do nome do ficheiro (ascii, sem espaços)."""
    if not nome:
        return "empresa"
    ascii_nome = (
        unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode("ascii")
    )
    limpo = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_nome).strip("-").lower()
    return limpo[:40] or "empresa"


def _sha256_ficheiro(caminho: Path) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def obter_chave_publica(db: Session, empresa: Empresa) -> dict:
    """Chave de assinatura desta empresa, para mostrar nas Definições/exportação,
    com o estado da atestação dessa mesma chave."""
    chave = crypto.obter_chave_empresa(db, empresa)
    return {
        "pub": chave["pub"],
        "fingerprint": crypto.fingerprint(chave["pub"]),
        "criado_em": chave.get("criado_em"),
        "atestacao_estado": (
            crypto.estado_atestacao(chave["pub"])
        ),
    }


def _atestacao_para(chave: dict) -> dict | None:
    """A atestação viaja no envelope só se for da chave que assina."""
    return crypto.obter_atestacao(chave["pub"])


# ---------------------------------------------------------------------------
# Estimativa e espaço em disco
# ---------------------------------------------------------------------------

def estimar(db: Session, empresa: Empresa) -> dict:
    """Contagens e tamanho previsto, para a UI mostrar ANTES de gerar."""
    def _contar(modelo, *filtros) -> int:
        return db.exec(
            select(func.count()).select_from(modelo).where(
                modelo.empresa_id == empresa.id, *filtros
            )
        ).one()

    from app.controlos.models import RelatorioAuditoria
    from app.formacao.models import AcaoFormacao
    from app.frameworks.models import ControloEmpresaV2
    from app.incidentes.models import Incidente
    from app.tarefas.models import Tarefa

    evidencias_bytes = db.exec(
        select(func.coalesce(func.sum(Evidencia.ficheiro_tamanho), 0)).where(
            Evidencia.empresa_id == empresa.id,
            Evidencia.deleted_at.is_(None),  # type: ignore[union-attr]
        )
    ).one()

    return {
        "controlos": _contar(ControloEmpresaV2),
        "evidencias": _contar(Evidencia, Evidencia.deleted_at.is_(None)),  # type: ignore[union-attr]
        "evidencias_mb": round(int(evidencias_bytes) / 1_048_576, 1),
        "relatorios_auditoria": _contar(RelatorioAuditoria),
        "incidentes": _contar(Incidente, Incidente.deleted_at.is_(None)),  # type: ignore[union-attr]
        "tarefas": _contar(Tarefa, Tarefa.deleted_at.is_(None)),  # type: ignore[union-attr]
        "formacoes": _contar(AcaoFormacao, AcaoFormacao.deleted_at.is_(None)),  # type: ignore[union-attr]
        "fingerprint": crypto.fingerprint(crypto.obter_chave_empresa(db, empresa)["pub"]),
    }


def _verificar_espaco(tmp_dir: Path, estimativa_bytes: int) -> None:
    """Exige ≥2× a estimativa livre (corpo em staging + ficheiro final)."""
    livre = shutil.disk_usage(tmp_dir).free
    necessario = max(estimativa_bytes * 2, 50 * 1024 * 1024)
    if livre < necessario:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail={
                "codigo": "espaco_insuficiente",
                "livre_mb": round(livre / 1_048_576),
                "necessario_mb": round(necessario / 1_048_576),
            },
        )


# ---------------------------------------------------------------------------
# Geração
# ---------------------------------------------------------------------------

def _ler_evidencia_clara(ficheiro: dict) -> bytes:
    """Conteúdo em claro de um ficheiro de evidência (decifra o repouso se
    preciso). Ficheiros de evidência são pequenos (limite de upload da app) —
    um de cada vez em memória, nunca todos."""
    dados = Path(ficheiro["path"]).read_bytes()
    if ficheiro["cifrado"]:
        from app.evidencias.service import decifrar_bytes_evidencia

        dados = decifrar_bytes_evidencia(dados)
    return dados


def auditor_fixado(db: Session, empresa: Empresa, pub: str) -> AuditorConfiavel | None:
    """A chave `pub` de um auditor já está fixada (pinning) nesta empresa?"""
    from sqlmodel import select as _select
    return db.exec(
        _select(AuditorConfiavel).where(
            AuditorConfiavel.empresa_id == empresa.id,
            AuditorConfiavel.pub == pub,
        )
    ).first()


# Um dossiê de cada vez, na vaga das operações pesadas (partilhada com a análise
# IA e o scrypt dos backups): o keyslot por frase-passe pede 128 MiB ao scrypt,
# e cada evidência passa pela memória em claro e cifrada enquanto é escrita.
@LIMITE_OPERACOES_PESADAS.ocupar()
def gerar_dossie(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    passphrase: str | None = None,
    incluir_evidencias: bool = True,
    periodo_atividade_meses: int | None = 12,
    request: Request | None = None,
    convite: str | None = None,
    confirmar_fingerprint: bool = False,
) -> dict:
    """
    Gera o ficheiro .nis2pme num diretório temporário e devolve os metadados
    (caminho, nome, tmp_dir para limpeza após o envio, contagens…).
    O caller é responsável por: registar auditoria + commit; apagar tmp_dir
    depois de a resposta ser enviada.

    Modo de cifra (exatamente um): `passphrase` (≥12, comunicada fora de banda)
    OU `convite` (token do auditor — cifra-se ao destinatário age dele, sem
    partilhar segredos). O keyslot `chave.age` reflete o modo escolhido.

    Confiança no convite (imposta AQUI, não só na UI): cifra-se para a chave de
    um auditor apenas se a identidade dele estiver atestada pela NIS2PME, já
    fixada nesta empresa (pinning), ou explicitamente confirmada pelo admin
    (`confirmar_fingerprint` — fingerprint conferido por outro canal). Nunca em
    silêncio para uma chave desconhecida: um convite trocado no caminho é
    exatamente o ataque que este passo fecha.
    """
    if bool(passphrase) == bool(convite):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"codigo": "modo_cifra_ambiguo"},
        )

    convite_info = None
    if convite:
        try:
            convite_info = crypto.verificar_convite(convite)
        except crypto.ConviteInvalido as erro:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"codigo": "convite_invalido", "detalhe": str(erro)},
            ) from erro
        fixado = auditor_fixado(db, empresa, convite_info["auditor_pub"])
        if (
            convite_info["atestacao_estado"] != "verificada"
            and fixado is None
            and not confirmar_fingerprint
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "codigo": "fingerprint_nao_confirmado",
                    "fingerprint": convite_info["auditor_fingerprint"],
                },
            )
        if fixado is None:
            # Pinning recíproco: a identidade conhecida ao colar o
            # convite fica fixada — o 1.º parecer deste auditor entra depois
            # sem nova cerimónia. Persiste no commit do caller.
            db.add(AuditorConfiavel(
                empresa_id=empresa.id,
                pub=convite_info["auditor_pub"],
                fingerprint=convite_info["auditor_fingerprint"],
                nome=cifrar_pii(convite_info["auditor_nome"]) if convite_info["auditor_nome"] else None,
                criado_por=utilizador.id,
            ))
    else:
        if len(passphrase) < PASSPHRASE_MIN:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"codigo": "passphrase_curta", "minimo": PASSPHRASE_MIN},
            )
    if periodo_atividade_meses not in PERIODOS_ATIVIDADE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"codigo": "periodo_invalido"},
        )

    chave_instancia = crypto.obter_chave_empresa(db, empresa)

    # Identidade de cifra do dossiê (única, aleatória) e identidade de resposta.
    identidade_dossie = pyrage.x25519.Identity.generate()
    destinatario_dossie = identidade_dossie.to_public()
    identidade_resposta = pyrage.x25519.Identity.generate()

    conteudo, contagens, ficheiros_evidencia = conteudo_mod.montar_conteudo(
        db, empresa, utilizador, request, incluir_evidencias, periodo_atividade_meses
    )

    agora = datetime.now(timezone.utc)
    dossie_id = uuid.uuid4()
    from app.config import get_settings
    app_version = get_settings().APP_VERSION

    ambito = {
        "modo": "convite" if convite_info else "passphrase",
        "evidencias_ficheiros": incluir_evidencias,
        "periodo_atividade_meses": periodo_atividade_meses,
        "app_version": app_version,
    }
    if convite_info:
        # Sem PII: só o id do convite (a plataforma resolve a chave por ele) e
        # o fingerprint do auditor a quem o dossiê foi cifrado.
        ambito["convite_id"] = convite_info["convite_id"]
        ambito["auditor_fingerprint"] = convite_info["auditor_fingerprint"]

    tmp_dir = Path(tempfile.mkdtemp(prefix="dossie-"))
    try:
        _verificar_espaco(tmp_dir, sum(f["bytes"] for f in ficheiros_evidencia))

        # Corpo: ZIP de transporte com entradas STORED (ciphertext não comprime).
        # As evidências entram primeiro, uma a uma — o manifest precisa dos
        # hashes de todas as entradas e só depois pode ser fechado e cifrado.
        corpo_path = tmp_dir / "corpo.bin"
        entradas: dict[str, dict] = {}
        mapa_evidencias: dict[str, dict] = {}
        with zipfile.ZipFile(corpo_path, "w", zipfile.ZIP_STORED) as z:
            for ficheiro in ficheiros_evidencia:
                claro = _ler_evidencia_clara(ficheiro)
                nome_entrada = f"ev/{ficheiro['id']}.age"
                # Hash do CLARO no manifest: o leitor confere após decifrar.
                mapa_evidencias[ficheiro["id"]] = {
                    "nome": ficheiro["nome"],
                    "sha256": hashlib.sha256(claro).hexdigest(),
                    "bytes": len(claro),
                }
                cifrado = pyrage.encrypt(claro, [destinatario_dossie])
                del claro
                entradas[nome_entrada] = {
                    "sha256": hashlib.sha256(cifrado).hexdigest(),
                    "bytes": len(cifrado),
                }
                z.writestr(nome_entrada, cifrado)
                del cifrado

            for caminho, dados in conteudo.items():
                entradas[caminho] = {
                    "sha256": hashlib.sha256(dados).hexdigest(),
                    "bytes": len(dados),
                }

            manifest = {
                "schema_versao": SCHEMA_VERSAO,
                "tipo": "dossie",
                "dossie_id": str(dossie_id),
                "gerado_em": agora.isoformat(),
                "app_version": app_version,
                "gerado_por": {
                    "id": str(utilizador.id),
                    "nome": decifrar_pii(utilizador.nome),
                    "role": utilizador.role.value if hasattr(utilizador.role, "value") else utilizador.role,
                },
                "empresa": {"id": str(empresa.id), "nome": decifrar_pii(empresa.nome)},
                "ambito": ambito,
                # Destinatário para um futuro parecer sobre ESTE dossiê.
                "resposta": {"destinatario_age": str(identidade_resposta.to_public())},
                "contagens": contagens,
                "entradas": entradas,
                # Entrada cifrada → nome original + hash do claro de cada evidência.
                "evidencias_ficheiros": mapa_evidencias,
            }

            # Zip interno (comprimido — é só JSON) cifrado para a identidade
            # do dossiê, e o keyslot: a identidade cifrada para o destinatário
            # real. Modo passphrase → scrypt (corre UMA vez, aqui); modo convite
            # → destinatário age X25519 do auditor (nenhum segredo partilhado).
            interno = io.BytesIO()
            with zipfile.ZipFile(interno, "w", zipfile.ZIP_DEFLATED) as zi:
                zi.writestr(
                    "manifest.json",
                    json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=1),
                )
                for caminho, dados in conteudo.items():
                    zi.writestr(caminho, dados)
            z.writestr(
                "dados.age",
                pyrage.encrypt(interno.getvalue(), [destinatario_dossie]),
            )
            id_dossie_bytes = str(identidade_dossie).encode("ascii")
            if convite_info:
                dest_auditor = pyrage.x25519.Recipient.from_str(convite_info["destinatario_age"])
                chave_age = pyrage.encrypt(id_dossie_bytes, [dest_auditor])
            else:
                # scrypt de custo fixo (128 MiB), e não o do `pyrage.passphrase`,
                # que se calibra pelo CPU e chegava aos 512 MiB.
                chave_age = keyslot.cifrar(id_dossie_bytes, passphrase)
            z.writestr("chave.age", chave_age)

        corpo_sha256 = _sha256_ficheiro(corpo_path)
        corpo_bytes = corpo_path.stat().st_size

        # Cabeçalho assinado: sem PII em claro; a assinatura cobre o sha256 do
        # corpo — origem e integridade verificam-se ANTES de decifrar. No modo
        # convite leva o `convite_id` (a plataforma resolve por ele a chave
        # efémera que decifra o keyslot) — é público e opaco.
        cifra = {"alg": "age-v1", "modo": "convite" if convite_info else "passphrase"}
        if convite_info:
            cifra["convite_id"] = convite_info["convite_id"]
        payload = {
            "formato": "nis2pme",
            "versao": FORMATO_VERSAO,
            "tipo": "dossie",
            "dossie_id": str(dossie_id),
            "criado_em": agora.isoformat(),
            "app_version": app_version,
            "cifra": cifra,
            "corpo": {"sha256": corpo_sha256, "bytes": corpo_bytes},
        }
        envelope = crypto.assinar_envelope(
            payload, chave_instancia["priv"], chave_instancia["pub"],
            atestacao=_atestacao_para(chave_instancia),
        )

        nome = (
            f"dossie-{_slug(decifrar_pii(empresa.nome))}-"
            f"{agora.strftime('%Y%m%d-%H%M')}.nis2pme"
        )
        final_path = tmp_dir / nome
        with final_path.open("wb") as destino, corpo_path.open("rb") as corpo:
            destino.write(crypto.MAGIC)
            destino.write(envelope)
            shutil.copyfileobj(corpo, destino, 1024 * 1024)
        corpo_path.unlink()
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    # Registo local (sem conteúdo): prova de emissão + chave de resposta,
    # cifrada em repouso. O commit é do caller, junto com a auditoria.
    db.add(DossieGerado(
        id=dossie_id,
        empresa_id=empresa.id,
        sha256=corpo_sha256,
        ambito=json.dumps(ambito),
        criado_por=utilizador.id,
        criado_em=agora,
        identidade_resposta=cifrar_pii(str(identidade_resposta)),
    ))

    return {
        "tmp_dir": str(tmp_dir),
        "caminho": str(final_path),
        "ficheiro": nome,
        "dossie_id": str(dossie_id),
        "sha256": corpo_sha256,
        "bytes": final_path.stat().st_size,
        "contagens": contagens,
        "ambito": ambito,
        "fingerprint": crypto.fingerprint(chave_instancia["pub"]),
        # Se o convite trouxe um destino de relay, o caller pode oferecer o envio.
        "relay": convite_info["relay"] if convite_info else None,
    }


def relay_permitido(url: str) -> bool:
    """O relay de um convite só se usa se o host estiver em
    `RELAY_HOSTS_PERMITIDOS`. O URL vem no convite, e quem faz o convite
    escolhe-o: sem a lista, um convite forjado (em SaaS, pelo admin de um
    cliente) punha o servidor a enviar pedidos para qualquer endereço que ele
    alcance. Também não se aceitam credenciais nem query no URL."""
    from urllib.parse import urlsplit

    from app.config import get_settings

    permitidos = {
        h.strip().lower() for h in (get_settings().RELAY_HOSTS_PERMITIDOS or "").split(",") if h.strip()
    }
    try:
        partes = urlsplit(url)
        _ = partes.port  # uma porta ilegível levanta ValueError
    except ValueError:
        return False
    anfitriao = (partes.hostname or "").lower()
    if not anfitriao or anfitriao not in permitidos:
        return False
    if partes.username or partes.password or partes.query or partes.fragment:
        return False
    # Em http só chega o loopback (o convite recusa os outros).
    return partes.scheme == "https" or (
        partes.scheme == "http" and anfitriao in ("localhost", "127.0.0.1", "::1")
    )


def enviar_para_relay(dossie_path: Path, relay: dict) -> dict:
    """Faz upload do ficheiro .nis2pme (cifrado ponta-a-ponta) para o relay
    indicado no convite, em streaming a partir do disco — o ficheiro nunca é
    carregado inteiro em memória. O relay é um correio cego: recebe bytes
    opacos autenticados pelo `token_upload` do convite, nunca chaves nem
    plaintext.

    `relay` = {url, convite_id, token_upload} (validado por verificar_convite).
    Levanta HTTPException com um código estável em caso de recusa do relay, ou
    400 `relay_nao_permitido` se o host não estiver na lista (sem pedido).
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    from app.shared.tls_fornecedor import contexto_fornecedor

    if not relay_permitido(relay["url"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail={"codigo": "relay_nao_permitido"}
        )
    # O id vem do convite: vai para o caminho do URL sempre escapado.
    destino = f"{relay['url']}/api/upload/{urllib.parse.quote(relay['convite_id'], safe='')}"
    try:
        with dossie_path.open("rb") as ficheiro:
            pedido = urllib.request.Request(
                destino, data=ficheiro, method="PUT",
                headers={
                    "Authorization": f"Bearer {relay['token_upload']}",
                    "Content-Type": "application/octet-stream",
                    # urllib não calcula o tamanho de um ficheiro: sem isto
                    # tentaria transferência chunked, que o relay não aceita.
                    "Content-Length": str(dossie_path.stat().st_size),
                },
            )
            # O relay pode estar servido com um certificado da CA do fornecedor.
            with urllib.request.urlopen(pedido, timeout=120, context=contexto_fornecedor()) as resposta:
                corpo = resposta.read()
        return json.loads(corpo.decode("utf-8")) if corpo else {"ok": True}
    except urllib.error.HTTPError as erro:
        # 409 usado · 410 expirado · 413 grande demais · 404 convite inválido.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"codigo": "relay_recusou", "estado": erro.code},
        ) from erro
    except (urllib.error.URLError, OSError, TimeoutError) as erro:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"codigo": "relay_inacessivel"},
        ) from erro
