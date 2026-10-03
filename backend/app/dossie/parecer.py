"""
Importação do parecer do auditor (.nis2pme tipo=parecer) — o round-trip.

Um auditor que recebeu um dossiê desta instância pode devolver um parecer:
um ficheiro assinado com a identidade Ed25519 dele e cifrado para o
destinatário de resposta gerado com esse dossiê. A importação verifica tudo
em camadas, fail-closed — nada produz efeitos antes de todas passarem:

    1. magic + envelope (JSON numa linha, com limite de tamanho)
    2. assinatura Ed25519 sobre os bytes exatos de payload_b64
    3. cabeçalho: formato, versão, tipo=parecer, cifra age-v1 modo convite
    4. dossie_ref tem de apontar para um dossiê gerado POR ESTA instância
       e desta empresa — pareceres órfãos são rejeitados ANTES de decifrar
    5. corpo com o tamanho e SHA-256 exatamente como assinados
    6. ZIP de transporte só com a entrada esperada, STORED e lida com teto
    7. decifra com a identidade de resposta guardada com o dossiê (X25519)
    8. zip interno com nomes exatos e limites de descompressão
    9. manifest e parecer coerentes com o cabeçalho assinado
   10. confiança na CHAVE do auditor: já fixada (auditores_confiaveis) ou
       TOFU — o admin confirma o fingerprint explicitamente no 1.º parecer

Só depois o conteúdo produz efeitos: relatórios de auditoria EXTERNOS por
controlo, achados → tarefas (plano de ação), pedidos de esclarecimento →
notificações e o selo no dashboard.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone

import pyrage
from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.auth.models import RoleUtilizador, Utilizador
from app.controlos.models import DecisaoAuditor, RelatorioAuditoria
from app.dossie import crypto
from app.dossie.models import AuditorConfiavel, DossieGerado, ParecerImportado
from app.dossie.verificar import ler_entrada
from app.empresas.models import Empresa
from app.frameworks.models import Control, ControloEmpresaV2
from app.notificacoes.catalogo import Codigo
from app.notificacoes.service import criar_notificacao
from app.shared.pii import cifrar_pii, decifrar_pii
from app.tarefas.models import Periodicidade, Tarefa, TipoTarefa

logger = logging.getLogger(__name__)

# Limites anti-hostil: um parecer real tem poucos KB; tudo acima é suspeito.
FICHEIRO_MAX = 32 * 1024 * 1024
_ENVELOPE_MAX = 64 * 1024
_INTERNO_CLARO_MAX = 16 * 1024 * 1024
# O `dados.age` é o zip interno cifrado: o claro máximo mais o que o age lhe
# junta (o cabeçalho e 16 bytes por bloco de 64 KiB).
_DADOS_AGE_MAX = _INTERNO_CLARO_MAX + _INTERNO_CLARO_MAX // 4096 + 4096
_MAX_CONTROLOS = 2000
_MAX_ACHADOS = 500
_MAX_PEDIDOS = 500
_TEXTO_MAX = 20_000

ESTADOS_EXTERNOS = ("conforme", "parcial", "nao_conforme")

# Prazo por omissão de uma tarefa criada por achado sem prazo sugerido válido.
_PRAZO_OMISSAO_DIAS = 30


def _erro(http_status: int, codigo: str, **extra) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"codigo": codigo, **extra})


def _invalido(camada: str) -> HTTPException:
    return _erro(
        status.HTTP_400_BAD_REQUEST, "parecer_invalido", camada=camada
    )


# ---------------------------------------------------------------------------
# Verificação em camadas
# ---------------------------------------------------------------------------

def _ler_interno(zint: zipfile.ZipFile, nome: str) -> bytes:
    """Uma entrada do zip interno, só até ao tamanho que declara; ValueError se
    não ler (o `zint.read` descomprimia tudo antes de cortar ao declarado)."""
    dados = ler_entrada(zint, nome, _INTERNO_CLARO_MAX)
    if dados is None:
        raise ValueError(nome)
    return dados


def _texto(valor, maximo: int = _TEXTO_MAX) -> str:
    """Normaliza um campo textual vindo do parecer (tipo + truncagem)."""
    if not isinstance(valor, str):
        return ""
    return valor[:maximo]


def _validar_parecer_json(parecer: dict, payload: dict, pub_envelope: str) -> dict:
    """Estrutura do dados/parecer.json — normalizada e com limites.

    O conteúdo vem de fora (e um auditor pode ser hostil): tipos e tamanhos
    são impostos aqui; listas acima dos limites rejeitam o ficheiro inteiro.
    """
    if not isinstance(parecer, dict):
        raise _invalido("parecer")
    if parecer.get("dossie_ref") != payload["dossie_ref"]:
        raise _invalido("parecer")

    # A identidade dentro do corpo cifrado tem de ser a mesma que assinou o
    # envelope — deteta recombinações de interior/exterior.
    auditor = parecer.get("auditor")
    if isinstance(auditor, dict) and auditor.get("pub_b64") not in (None, pub_envelope):
        raise _invalido("parecer")

    bruto_global = parecer.get("global")
    if bruto_global is not None and not isinstance(bruto_global, dict):
        raise _invalido("parecer")
    bruto_global = bruto_global or {}

    controlos_in = parecer.get("controlos") or []
    achados_in = parecer.get("achados") or []
    pedidos_in = parecer.get("pedidos") or []
    if not isinstance(controlos_in, list) or len(controlos_in) > _MAX_CONTROLOS:
        raise _invalido("parecer")
    if not isinstance(achados_in, list) or len(achados_in) > _MAX_ACHADOS:
        raise _invalido("parecer")
    if not isinstance(pedidos_in, list) or len(pedidos_in) > _MAX_PEDIDOS:
        raise _invalido("parecer")

    controlos = []
    for c in controlos_in:
        if not isinstance(c, dict):
            raise _invalido("parecer")
        codigo = _texto(c.get("codigo"), 30).strip()
        estado = c.get("estado")
        if not codigo or estado not in ESTADOS_EXTERNOS:
            raise _invalido("parecer")
        controlos.append({
            "codigo": codigo,
            "estado": estado,
            "observacoes": _texto(c.get("observacoes")),
        })

    achados = []
    for a in achados_in:
        if not isinstance(a, dict):
            raise _invalido("parecer")
        descricao = _texto(a.get("descricao"), 5000).strip()
        if not descricao:
            continue
        achados.append({
            "controlo": _texto(a.get("controlo"), 30).strip(),
            "severidade": _texto(a.get("severidade"), 20).strip(),
            "descricao": descricao,
            "acao_recomendada": _texto(a.get("acao_recomendada"), 5000).strip(),
            "prazo_sugerido": _texto(a.get("prazo_sugerido"), 100).strip(),
        })

    pedidos = []
    for p in pedidos_in:
        if not isinstance(p, dict):
            raise _invalido("parecer")
        texto = _texto(p.get("texto"), 5000).strip()
        if not texto:
            continue
        pedidos.append({"controlo": _texto(p.get("controlo"), 30).strip(), "texto": texto})

    return {
        "global": {
            "nivel_validado": _texto(bruto_global.get("nivel_validado"), 100).strip(),
            "texto": _texto(bruto_global.get("texto")),
            "ambito": _texto(bruto_global.get("ambito"), 5000),
            "limitacoes": _texto(bruto_global.get("limitacoes"), 5000),
        },
        "auditor_nome": _texto(parecer.get("auditor_nome"), 200).strip(),
        "controlos": controlos,
        "achados": achados,
        "pedidos": pedidos,
    }


def _verificar(db: Session, empresa: Empresa, dados: bytes) -> dict:
    """Corre TODAS as camadas de verificação e devolve o material validado.

    Nunca produz efeitos — inspecionar e confirmar usam ambas este caminho
    (o confirmar re-verifica sempre; o preview não é fonte de confiança).
    """
    if len(dados) > FICHEIRO_MAX:
        raise _erro(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "ficheiro_demasiado_grande")

    # 1. magic + envelope
    if not dados.startswith(crypto.MAGIC):
        raise _invalido("formato")
    resto = dados[len(crypto.MAGIC):]
    # Limite do verificador de referência: linha completa (com '\n') ≤ 64 KiB.
    fim_linha = resto.find(b"\n", 0, _ENVELOPE_MAX)
    if fim_linha < 0:
        raise _invalido("envelope")
    linha, corpo = resto[:fim_linha], resto[fim_linha + 1:]

    try:
        envelope = json.loads(linha.decode("utf-8"))
        pub_envelope = envelope["pub"]
    except (ValueError, KeyError, UnicodeDecodeError):
        raise _invalido("envelope")

    # 2. assinatura Ed25519 (sobre os bytes exatos de payload_b64)
    payload = crypto.verificar_envelope(linha)
    if payload is None:
        raise _erro(status.HTTP_400_BAD_REQUEST, "parecer_assinatura_invalida")

    # 3. cabeçalho assinado
    try:
        if payload["formato"] != "nis2pme":
            raise _invalido("cabecalho")
        versao = payload["versao"]
        if not isinstance(versao, int) or versao != 1:
            raise _erro(status.HTTP_400_BAD_REQUEST, "versao_nao_suportada")
        if payload["tipo"] != "parecer":
            raise _erro(status.HTTP_400_BAD_REQUEST, "nao_e_parecer")
        parecer_id = uuid.UUID(payload["dossie_id"])
        dossie_ref = uuid.UUID(payload["dossie_ref"])
        payload["dossie_ref"] = str(dossie_ref)
        if payload["cifra"]["alg"] != "age-v1" or payload["cifra"]["modo"] != "convite":
            raise _invalido("cifra")
        corpo_sha256 = payload["corpo"]["sha256"]
        corpo_bytes = payload["corpo"]["bytes"]
        if not isinstance(corpo_sha256, str) or not isinstance(corpo_bytes, int):
            raise _invalido("cabecalho")
    except (KeyError, TypeError, ValueError):
        raise _invalido("cabecalho")

    # 4. o dossiê referenciado tem de ser NOSSO — antes de decifrar seja o que for
    dossie = db.exec(
        select(DossieGerado).where(
            DossieGerado.id == dossie_ref, DossieGerado.empresa_id == empresa.id
        )
    ).first()
    if dossie is None:
        raise _erro(status.HTTP_404_NOT_FOUND, "parecer_orfao")

    identidade_cifrada = dossie.identidade_resposta
    identidade_str = decifrar_pii(identidade_cifrada) if identidade_cifrada else None
    if not identidade_str or not identidade_str.startswith("AGE-SECRET-KEY-"):
        raise _erro(status.HTTP_422_UNPROCESSABLE_ENTITY, "sem_identidade_resposta")

    # Anti-replay: cada parecer entra uma única vez.
    if db.get(ParecerImportado, parecer_id) is not None:
        raise _erro(status.HTTP_409_CONFLICT, "parecer_ja_importado")

    # 5. corpo exatamente como assinado
    if len(corpo) != corpo_bytes or hashlib.sha256(corpo).hexdigest() != corpo_sha256:
        raise _invalido("corpo")

    # 6. ZIP de transporte: só o esperado, STORED (o conteúdo é ciphertext, não
    # comprime; uma entrada comprimida seria a porta de uma bomba de
    # descompressão) e lido com teto — tudo antes de decifrar.
    try:
        zext = zipfile.ZipFile(io.BytesIO(corpo))
    except zipfile.BadZipFile:
        raise _invalido("corpo")
    if zext.namelist() != ["dados.age"]:
        raise _invalido("corpo")
    info = zext.getinfo("dados.age")
    if info.compress_type != zipfile.ZIP_STORED or info.file_size != info.compress_size:
        raise _invalido("corpo")
    dados_age = ler_entrada(zext, "dados.age", _DADOS_AGE_MAX)
    if dados_age is None:
        raise _invalido("corpo")

    # 7. decifra com a identidade de resposta deste dossiê
    try:
        identidade = pyrage.x25519.Identity.from_str(identidade_str)
        interno_claro = pyrage.decrypt(dados_age, [identidade])
    except Exception:
        # Não foi cifrado para o destinatário de resposta deste dossiê.
        raise _erro(status.HTTP_400_BAD_REQUEST, "parecer_nao_decifra")
    if len(interno_claro) > _INTERNO_CLARO_MAX:
        raise _invalido("dados")

    # 8. zip interno com nomes exatos; cada entrada lê-se só até ao tamanho que
    # declara (a soma dos declarados confere-se antes).
    try:
        zint = zipfile.ZipFile(io.BytesIO(interno_claro))
    except zipfile.BadZipFile:
        raise _invalido("dados")
    if sorted(zint.namelist()) != ["dados/parecer.json", "manifest.json"]:
        raise _invalido("dados")
    if sum(i.file_size for i in zint.infolist()) > _INTERNO_CLARO_MAX:
        raise _invalido("dados")

    # 9. manifest e parecer coerentes com o cabeçalho assinado
    try:
        manifest = json.loads(_ler_interno(zint, "manifest.json").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _invalido("manifest")
    if not isinstance(manifest, dict) or manifest.get("tipo") != "parecer":
        raise _invalido("manifest")
    if manifest.get("dossie_ref") != payload["dossie_ref"]:
        raise _invalido("manifest")

    try:
        parecer_bruto = json.loads(_ler_interno(zint, "dados/parecer.json").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _invalido("parecer")
    parecer = _validar_parecer_json(parecer_bruto, payload, pub_envelope)

    # 10. confiança na chave do auditor — TOFU (pinning local) OU atestação
    # NIS2PME (verificada de novo em cada parecer, nunca cacheada: é barata e
    # a chave mestra pode ter revogado o que ontem era válido só no futuro,
    # não há necessidade de confiar num estado guardado).
    fixado = db.exec(
        select(AuditorConfiavel).where(
            AuditorConfiavel.empresa_id == empresa.id,
            AuditorConfiavel.pub == pub_envelope,
        )
    ).first()
    try:
        atestacao_estado = crypto.verificar_atestacao(
            envelope.get("atestacao"), pub_envelope, crypto.SUBJECT_AUDITOR
        )
    except ValueError as erro:
        raise _erro(status.HTTP_400_BAD_REQUEST, "atestacao_invalida", detalhe=str(erro))

    criado_em = None
    try:
        criado_em = datetime.fromisoformat(payload.get("criado_em", ""))
    except (TypeError, ValueError):
        pass

    return {
        "parecer_id": parecer_id,
        "dossie": dossie,
        "payload": payload,
        "parecer": parecer,
        "auditor_pub": pub_envelope,
        "fingerprint": crypto.fingerprint(pub_envelope),
        "confiavel": fixado is not None or atestacao_estado == "verificada",
        "atestado": atestacao_estado == "verificada",
        "corpo_sha256": corpo_sha256,
        "criado_em": criado_em,
    }


# ---------------------------------------------------------------------------
# Inspeção (preview) e confirmação (efeitos)
# ---------------------------------------------------------------------------

def inspecionar(db: Session, empresa: Empresa, dados: bytes) -> dict:
    """Verifica tudo e devolve o preview — sem gravar nada."""
    v = _verificar(db, empresa, dados)
    return {
        "parecer_id": str(v["parecer_id"]),
        "dossie": {
            "id": str(v["dossie"].id),
            "criado_em": v["dossie"].criado_em.isoformat() if v["dossie"].criado_em else None,
        },
        "auditor": {
            "fingerprint": v["fingerprint"],
            "nome": v["parecer"]["auditor_nome"] or None,
            "confiavel": v["confiavel"],
            "atestado": v["atestado"],
        },
        "criado_em": v["criado_em"].isoformat() if v["criado_em"] else None,
        "nivel_validado": v["parecer"]["global"]["nivel_validado"] or None,
        "contagens": {
            "controlos": len(v["parecer"]["controlos"]),
            "achados": len(v["parecer"]["achados"]),
            "pedidos": len(v["parecer"]["pedidos"]),
        },
    }


def _mapa_controlos(db: Session, empresa: Empresa) -> dict[str, ControloEmpresaV2]:
    """codigo do controlo → ControloEmpresaV2 da empresa."""
    linhas = db.exec(
        select(ControloEmpresaV2, Control.code)
        .join(Control, Control.id == ControloEmpresaV2.control_id)  # type: ignore[arg-type]
        .where(ControloEmpresaV2.empresa_id == empresa.id)
    ).all()
    return {code: ce for ce, code in linhas}


def _prazo_do_achado(prazo_sugerido: str) -> date:
    """Prazo sugerido em texto → data da tarefa (ISO à cabeça, senão 30 dias)."""
    try:
        return date.fromisoformat(prazo_sugerido[:10])
    except ValueError:
        return date.today() + timedelta(days=_PRAZO_OMISSAO_DIAS)


def confirmar(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    dados: bytes,
    confirmar_fingerprint: bool,
) -> dict:
    """Re-verifica tudo e aplica os efeitos do parecer. O commit é do caller
    (junto com a entrada de auditoria)."""
    v = _verificar(db, empresa, dados)
    parecer = v["parecer"]
    agora = datetime.now(timezone.utc)

    # TOFU: chave nova exige confirmação explícita do fingerprint pelo admin
    # (por outro canal — telefone, presencial). Nunca se fixa em silêncio.
    if not v["confiavel"]:
        if not confirmar_fingerprint:
            raise _erro(
                status.HTTP_409_CONFLICT,
                "fingerprint_nao_confirmado",
                fingerprint=v["fingerprint"],
            )
        db.add(AuditorConfiavel(
            empresa_id=empresa.id,
            pub=v["auditor_pub"],
            fingerprint=v["fingerprint"],
            nome=cifrar_pii(parecer["auditor_nome"]) if parecer["auditor_nome"] else None,
            criado_por=utilizador.id,
        ))

    registo = ParecerImportado(
        id=v["parecer_id"],
        empresa_id=empresa.id,
        dossie_gerado_id=v["dossie"].id,
        sha256=v["corpo_sha256"],
        auditor_pub=v["auditor_pub"],
        auditor_fingerprint=v["fingerprint"],
        auditor_nome=cifrar_pii(parecer["auditor_nome"]) if parecer["auditor_nome"] else None,
        global_json=cifrar_pii(json.dumps(parecer["global"], ensure_ascii=False)),
        n_controlos=len(parecer["controlos"]),
        n_achados=len(parecer["achados"]),
        n_pedidos=len(parecer["pedidos"]),
        parecer_criado_em=v["criado_em"],
        importado_por=utilizador.id,
        importado_em=agora,
    )
    db.add(registo)

    por_codigo = _mapa_controlos(db, empresa)
    nome_para_relatorio = parecer["auditor_nome"] or v["fingerprint"]

    # Pareceres por controlo → relatórios de auditoria EXTERNOS (imutáveis).
    for c in parecer["controlos"]:
        ce = por_codigo.get(c["codigo"])
        texto = c["observacoes"] or c["codigo"]
        db.add(RelatorioAuditoria(
            controlo_empresa_v2_id=ce.id if ce else None,
            empresa_id=empresa.id,
            auditor_id=None,
            auditor_nome=cifrar_pii(nome_para_relatorio) or nome_para_relatorio,
            decisao=(
                DecisaoAuditor.APROVADO
                if c["estado"] == "conforme"
                else DecisaoAuditor.NAO_APROVADO
            ),
            texto=cifrar_pii(texto) or texto,
            externo=True,
            estado_externo=c["estado"],
            parecer_id=registo.id,
        ))

    # Achados → tarefas (o plano de ação que o próximo dossiê mostra resolvido).
    for a in parecer["achados"]:
        titulo = (f"{a['controlo']}: {a['descricao']}" if a["controlo"] else a["descricao"])
        detalhes = [a["descricao"]]
        if a["severidade"]:
            detalhes.append(f"({a['severidade']})")
        if a["acao_recomendada"]:
            detalhes.append(a["acao_recomendada"])
        if a["prazo_sugerido"]:
            detalhes.append(a["prazo_sugerido"])
        db.add(Tarefa(
            empresa_id=empresa.id,
            titulo=titulo[:255],
            descricao="\n\n".join(detalhes),
            categoria="auditoria_externa",
            tipo=TipoTarefa.RECORRENTE,
            controlos=a["controlo"],
            periodicidade=Periodicidade.PONTUAL,
            proximo_prazo=_prazo_do_achado(a["prazo_sugerido"]),
            origem_parecer_id=registo.id,
        ))

    # Pedidos de esclarecimento → notificações aos admins/subadmins ativos.
    destinatarios = db.exec(
        select(Utilizador).where(
            Utilizador.empresa_id == empresa.id,
            Utilizador.ativo == True,  # noqa: E712
            Utilizador.role.in_([RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN]),  # type: ignore[union-attr]
        )
    ).all()
    notificacoes = 0
    for p in parecer["pedidos"]:
        ce = por_codigo.get(p["controlo"]) if p["controlo"] else None
        for destinatario in destinatarios:
            criar_notificacao(
                db,
                empresa_id=empresa.id,
                utilizador_id=destinatario.id,
                codigo=Codigo.PARECER_PEDIDO,
                params={"controlo": p["controlo"] or "", "texto": p["texto"]},
                entidade_id=ce.id if ce else None,
                controlo_empresa_id=ce.id if ce else None,
            )
            notificacoes += 1

    return {
        "parecer_id": str(registo.id),
        "auditor_fingerprint": v["fingerprint"],
        "nivel_validado": parecer["global"]["nivel_validado"] or None,
        "relatorios_criados": len(parecer["controlos"]),
        "tarefas_criadas": len(parecer["achados"]),
        "notificacoes_criadas": notificacoes,
        "dossie_ref": str(v["dossie"].id),
    }


# ---------------------------------------------------------------------------
# Selo do dashboard
# ---------------------------------------------------------------------------

def selo(db: Session, empresa: Empresa) -> dict:
    """Último parecer importado — "dossiê de {data} revisto por {auditor}"."""
    ultimo = db.exec(
        select(ParecerImportado)
        .where(ParecerImportado.empresa_id == empresa.id)
        .order_by(ParecerImportado.importado_em.desc())  # type: ignore[union-attr]
    ).first()
    if ultimo is None:
        return {"existe": False}

    nivel = None
    if ultimo.global_json:
        try:
            nivel = json.loads(decifrar_pii(ultimo.global_json) or "{}").get("nivel_validado")
        except ValueError:
            pass

    dossie = db.get(DossieGerado, ultimo.dossie_gerado_id)
    return {
        "existe": True,
        "parecer_id": str(ultimo.id),
        "auditor_fingerprint": ultimo.auditor_fingerprint,
        "auditor_nome": decifrar_pii(ultimo.auditor_nome) if ultimo.auditor_nome else None,
        "nivel_validado": nivel or None,
        "parecer_criado_em": ultimo.parecer_criado_em.isoformat() if ultimo.parecer_criado_em else None,
        "importado_em": ultimo.importado_em.isoformat() if ultimo.importado_em else None,
        "dossie_criado_em": dossie.criado_em.isoformat() if dossie and dossie.criado_em else None,
        "n_controlos": ultimo.n_controlos,
        "n_achados": ultimo.n_achados,
    }
