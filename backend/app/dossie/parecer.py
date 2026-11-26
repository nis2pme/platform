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

O mesmo caminho recebe o trabalho de um CONSULTOR (cabeçalho `tipo =
consultoria`): quem ajudou a implementar não audita, por isso aí não há
relatórios externos nem selo — só as recomendações (tarefas), os pedidos e o
texto, que se leem no ecrã dos pareceres. Achados e pedidos trazem um id fixo
entre ficheiros: o mesmo achado atualiza a sua tarefa e o mesmo pedido não
volta a avisar. O ficheiro pode trazer o convite para o envio seguinte,
assinado pela mesma chave.
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
# Estado da implementação de um controlo, no trabalho de um consultor.
ESTADOS_CONSULTORIA = ("por_iniciar", "em_curso", "implementado")
SEVERIDADES = ("baixa", "media", "alta", "critica")

# A natureza vem do tipo do cabeçalho assinado: "parecer" é uma auditoria
# (relatórios externos e selo); "consultoria" é o trabalho de quem ajuda a
# implementar, e não conta como auditoria externa.
NATUREZA_AUDITORIA = "auditoria"
NATUREZA_CONSULTORIA = "consultoria"
_NATUREZA_DO_TIPO = {"parecer": NATUREZA_AUDITORIA, "consultoria": NATUREZA_CONSULTORIA}
_CATEGORIA_TAREFA = {NATUREZA_AUDITORIA: "auditoria_externa", NATUREZA_CONSULTORIA: "consultoria"}

_MAX_EVIDENCIAS_CONTROLO = 200
_CONVITE_MAX = 8000

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


def _id_fixo(valor) -> str | None:
    """Id fixo de um achado ou pedido (UUID vindo da app do auditor). Ausente →
    None (ficheiros de versões anteriores); presente mas inválido → recusa."""
    if valor is None or valor == "":
        return None
    try:
        return str(uuid.UUID(str(valor)))
    except (ValueError, TypeError, AttributeError):
        raise _invalido("parecer")


def _evidencias_analisadas(valor) -> list[dict]:
    """Evidências que o auditor declara ter analisado num controlo."""
    if valor is None:
        return []
    if not isinstance(valor, list) or len(valor) > _MAX_EVIDENCIAS_CONTROLO:
        raise _invalido("parecer")
    out = []
    for e in valor:
        if not isinstance(e, dict):
            raise _invalido("parecer")
        sha = e.get("sha256")
        sha = sha.lower() if isinstance(sha, str) and len(sha) == 64 and all(
            c in "0123456789abcdefABCDEF" for c in sha) else None
        out.append({
            "id": _texto(e.get("id"), 64).strip(),
            "sha256": sha,
            "titulo": _texto(e.get("titulo"), 500).strip(),
            "estado": _texto(e.get("estado"), 30).strip(),
        })
    return out


def _validar_parecer_json(parecer: dict, payload: dict, pub_envelope: str, natureza: str) -> dict:
    """Estrutura do dados/parecer.json — normalizada e com limites.

    O conteúdo vem de fora (e um auditor pode ser hostil): tipos e tamanhos
    são impostos aqui; listas acima dos limites rejeitam o ficheiro inteiro.
    `natureza` vem do cabeçalho assinado; o corpo, se a declarar, tem de bater.
    """
    if not isinstance(parecer, dict):
        raise _invalido("parecer")
    if parecer.get("dossie_ref") != payload["dossie_ref"]:
        raise _invalido("parecer")
    if parecer.get("natureza") not in (None, natureza):
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

    estados = ESTADOS_EXTERNOS if natureza == NATUREZA_AUDITORIA else ESTADOS_CONSULTORIA
    controlos = []
    for c in controlos_in:
        if not isinstance(c, dict):
            raise _invalido("parecer")
        codigo = _texto(c.get("codigo"), 30).strip()
        estado = c.get("estado")
        if not codigo or estado not in estados:
            raise _invalido("parecer")
        controlos.append({
            "codigo": codigo,
            "estado": estado,
            "observacoes": _texto(c.get("observacoes")),
            "evidencias": _evidencias_analisadas(c.get("evidencias")),
        })

    vistos: set[str] = set()

    def _unico(i: str | None) -> str | None:
        # O mesmo id duas vezes no mesmo ficheiro não tem leitura possível.
        if i is not None:
            if i in vistos:
                raise _invalido("parecer")
            vistos.add(i)
        return i

    achados = []
    for a in achados_in:
        if not isinstance(a, dict):
            raise _invalido("parecer")
        descricao = _texto(a.get("descricao"), 5000).strip()
        if not descricao:
            continue
        severidade = _texto(a.get("severidade"), 20).strip()
        achados.append({
            "id": _unico(_id_fixo(a.get("id"))),
            "controlo": _texto(a.get("controlo"), 30).strip(),
            "severidade": severidade if severidade in SEVERIDADES else "",
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
        pedidos.append({
            "id": _unico(_id_fixo(p.get("id"))),
            "controlo": _texto(p.get("controlo"), 30).strip(),
            "texto": texto,
        })

    if natureza == NATUREZA_AUDITORIA:
        global_ = {
            "nivel_validado": _texto(bruto_global.get("nivel_validado"), 100).strip(),
            "texto": _texto(bruto_global.get("texto")),
            "ambito": _texto(bruto_global.get("ambito"), 5000),
            "limitacoes": _texto(bruto_global.get("limitacoes"), 5000),
        }
    else:
        revisao = _texto(bruto_global.get("proxima_revisao"), 10).strip()
        try:
            revisao = date.fromisoformat(revisao).isoformat() if revisao else ""
        except ValueError:
            revisao = ""
        global_ = {
            "nivel_validado": "",
            "texto": _texto(bruto_global.get("texto")),
            "proximos_passos": _texto(bruto_global.get("proximos_passos")),
            "proxima_revisao": revisao,
        }

    convite = parecer.get("proximo_convite")
    if convite is not None and (not isinstance(convite, str) or len(convite) > _CONVITE_MAX):
        raise _invalido("parecer")

    return {
        "natureza": natureza,
        "global": global_,
        "auditor_nome": _texto(parecer.get("auditor_nome"), 200).strip(),
        "controlos": controlos,
        "achados": achados,
        "pedidos": pedidos,
        "proximo_convite": convite or None,
    }


def _verificar(
    db: Session, empresa: Empresa, dados: bytes, dossie_esperado: uuid.UUID | None = None,
) -> dict:
    """Corre TODAS as camadas de verificação e devolve o material validado.

    Nunca produz efeitos — inspecionar e confirmar usam ambas este caminho
    (o confirmar re-verifica sempre; o preview não é fonte de confiança).

    `dossie_esperado`: o parecer chegou pelo canal de resposta de um dossiê, e
    tem de ser sobre esse dossiê — senão um relay podia pôr no canal de um
    dossiê o parecer de outro.
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
        natureza = _NATUREZA_DO_TIPO.get(payload["tipo"])
        if natureza is None:
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
    if dossie_esperado is not None and dossie.id != dossie_esperado:
        raise _erro(status.HTTP_400_BAD_REQUEST, "parecer_canal_errado")

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
    if not isinstance(manifest, dict) or manifest.get("tipo") != payload["tipo"]:
        raise _invalido("manifest")
    if manifest.get("dossie_ref") != payload["dossie_ref"]:
        raise _invalido("manifest")

    try:
        parecer_bruto = json.loads(_ler_interno(zint, "dados/parecer.json").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _invalido("parecer")
    parecer = _validar_parecer_json(parecer_bruto, payload, pub_envelope, natureza)
    proximo_convite = _verificar_proximo_convite(parecer.pop("proximo_convite"), pub_envelope)

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
        "natureza": natureza,
        "fixado": fixado,
        "proximo_convite": proximo_convite,
        "auditor_pub": pub_envelope,
        "fingerprint": crypto.fingerprint(pub_envelope),
        "confiavel": fixado is not None or atestacao_estado == "verificada",
        "atestado": atestacao_estado == "verificada",
        "corpo_sha256": corpo_sha256,
        "criado_em": criado_em,
    }


def _verificar_proximo_convite(token: str | None, pub_envelope: str) -> dict | None:
    """O convite para o envio seguinte, se o ficheiro trouxer um.

    Tem de vir assinado pela MESMA chave que assinou o ficheiro: um convite de
    outra chave mandaria o próximo dossiê a outra pessoa, e recusa-se o
    ficheiro inteiro. Um convite já fora de prazo não estraga o resto: fica
    só sem convite guardado."""
    if not token:
        return None
    try:
        info = crypto.verificar_convite(token)
    except crypto.ConviteExpirado:
        return None
    except crypto.ConviteInvalido:
        raise _invalido("convite")
    if info["auditor_pub"] != pub_envelope:
        raise _erro(status.HTTP_400_BAD_REQUEST, "convite_de_outra_chave")
    expira = None
    if info.get("expira_em"):
        expira = datetime.fromisoformat(info["expira_em"])
    return {"token": token, "expira_em": expira, "relay": bool(info.get("relay"))}


# ---------------------------------------------------------------------------
# Inspeção (preview) e confirmação (efeitos)
# ---------------------------------------------------------------------------

def _chave_do_convite(dossie: DossieGerado, fingerprint: str) -> bool | None:
    """O parecer vem assinado pela chave do auditor a quem o dossiê foi
    cifrado? `None` se o dossiê não foi por convite. Uma chave diferente não se
    recusa (o auditor pode ter rodado a identidade): passa pela confiança de
    sempre e o ecrã avisa."""
    try:
        ambito = json.loads(dossie.ambito or "{}")
    except ValueError:
        return None
    esperado = ambito.get("auditor_fingerprint") if isinstance(ambito, dict) else None
    return None if not isinstance(esperado, str) else esperado == fingerprint


def _tarefas_dos_achados(db: Session, empresa: Empresa, ids: list[str]) -> dict[str, Tarefa]:
    """origem_achado_id → tarefa (não apagada) desta empresa."""
    if not ids:
        return {}
    linhas = db.exec(
        select(Tarefa).where(
            Tarefa.empresa_id == empresa.id,
            Tarefa.origem_achado_id.in_(ids),  # type: ignore[union-attr]
            Tarefa.deleted_at == None,  # noqa: E711
        )
    ).all()
    return {t.origem_achado_id: t for t in linhas if t.origem_achado_id}


def _chave_achado(a: dict, i: int) -> str:
    """Como o ecrã de importação se refere a um achado (para o responsável)."""
    return a["id"] or f"#{i}"


def inspecionar(
    db: Session, empresa: Empresa, dados: bytes, dossie_esperado: uuid.UUID | None = None,
) -> dict:
    """Verifica tudo e devolve o preview — sem gravar nada."""
    v = _verificar(db, empresa, dados, dossie_esperado)
    parecer = v["parecer"]
    existentes = _tarefas_dos_achados(db, empresa, [a["id"] for a in parecer["achados"] if a["id"]])
    achados = []
    for i, a in enumerate(parecer["achados"]):
        t = existentes.get(a["id"]) if a["id"] else None
        achados.append({
            "chave": _chave_achado(a, i),
            "controlo": a["controlo"],
            "severidade": a["severidade"] or None,
            "descricao": a["descricao"],
            # "nova" | "atualiza" (tarefa ativa) | "resolvida" (já concluída: não reabre)
            "tarefa": "nova" if t is None else ("atualiza" if t.ativa else "resolvida"),
            "responsavel_id": str(t.responsavel_id) if t is not None and t.responsavel_id else None,
        })
    return {
        "parecer_id": str(v["parecer_id"]),
        "natureza": v["natureza"],
        "dossie": {
            "id": str(v["dossie"].id),
            "criado_em": v["dossie"].criado_em.isoformat() if v["dossie"].criado_em else None,
        },
        "auditor": {
            "fingerprint": v["fingerprint"],
            "nome": parecer["auditor_nome"] or None,
            "confiavel": v["confiavel"],
            "atestado": v["atestado"],
            "chave_do_convite": _chave_do_convite(v["dossie"], v["fingerprint"]),
        },
        "criado_em": v["criado_em"].isoformat() if v["criado_em"] else None,
        "nivel_validado": parecer["global"]["nivel_validado"] or None,
        "proxima_revisao": parecer["global"].get("proxima_revisao") or None,
        "contagens": {
            "controlos": len(parecer["controlos"]),
            "achados": len(parecer["achados"]),
            "pedidos": len(parecer["pedidos"]),
        },
        "achados": achados,
        "traz_convite": v["proximo_convite"] is not None,
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


def _conteudo(registo: ParecerImportado) -> dict | None:
    """O parecer normalizado guardado (None nos importados antes da 0.4.3)."""
    if not registo.conteudo:
        return None
    try:
        valor = json.loads(decifrar_pii(registo.conteudo) or "null")
    except ValueError:
        return None
    return valor if isinstance(valor, dict) else None


def _pedidos_ja_vistos(db: Session, empresa: Empresa) -> set[str]:
    """Ids dos pedidos que já chegaram em pareceres anteriores (já avisados)."""
    vistos: set[str] = set()
    for registo in db.exec(
        select(ParecerImportado).where(ParecerImportado.empresa_id == empresa.id)
    ).all():
        for p in (_conteudo(registo) or {}).get("pedidos") or []:
            if isinstance(p, dict) and p.get("id"):
                vistos.add(p["id"])
    return vistos


def _responsaveis_validos(
    db: Session, empresa: Empresa, responsaveis: dict[str, uuid.UUID] | None,
) -> dict[str, uuid.UUID]:
    """Só utilizadores ativos desta empresa podem ficar donos de uma tarefa."""
    if not responsaveis:
        return {}
    ids = set(responsaveis.values())
    validos = {
        u.id for u in db.exec(
            select(Utilizador).where(
                Utilizador.empresa_id == empresa.id,
                Utilizador.ativo == True,  # noqa: E712
                Utilizador.id.in_(ids),  # type: ignore[union-attr]
            )
        ).all()
    }
    if ids - validos:
        raise _erro(status.HTTP_400_BAD_REQUEST, "responsavel_invalido")
    return dict(responsaveis)


def confirmar(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    dados: bytes,
    confirmar_fingerprint: bool,
    dossie_esperado: uuid.UUID | None = None,
    responsaveis: dict[str, uuid.UUID] | None = None,
) -> dict:
    """Re-verifica tudo e aplica os efeitos do parecer. O commit é do caller
    (junto com a entrada de auditoria).

    `responsaveis`: chave do achado (a do preview) → utilizador que fica dono
    da tarefa. Numa tarefa que já existia, só muda se for indicado."""
    v = _verificar(db, empresa, dados, dossie_esperado)
    parecer = v["parecer"]
    natureza = v["natureza"]
    agora = datetime.now(timezone.utc)
    donos = _responsaveis_validos(db, empresa, responsaveis)

    # TOFU: chave nova exige confirmação explícita do fingerprint pelo admin
    # (por outro canal — telefone, presencial). Nunca se fixa em silêncio.
    if not v["confiavel"] and not confirmar_fingerprint:
        raise _erro(
            status.HTTP_409_CONFLICT,
            "fingerprint_nao_confirmado",
            fingerprint=v["fingerprint"],
        )
    fixado = v["fixado"]
    if fixado is None and (not v["confiavel"] or v["proximo_convite"] is not None):
        # Fixa-se a chave confirmada; uma chave atestada só ganha linha aqui
        # quando é preciso guardar o convite que trouxe.
        fixado = AuditorConfiavel(
            empresa_id=empresa.id,
            pub=v["auditor_pub"],
            fingerprint=v["fingerprint"],
            nome=cifrar_pii(parecer["auditor_nome"]) if parecer["auditor_nome"] else None,
            criado_por=utilizador.id,
        )
    if fixado is not None:
        if parecer["auditor_nome"]:
            fixado.nome = cifrar_pii(parecer["auditor_nome"])
        if v["proximo_convite"] is not None:
            fixado.proximo_convite = cifrar_pii(v["proximo_convite"]["token"])
            fixado.proximo_convite_expira_em = v["proximo_convite"]["expira_em"]
        db.add(fixado)

    # Os pedidos já avisados leem-se antes de este parecer entrar na sessão.
    pedidos_vistos = _pedidos_ja_vistos(db, empresa)
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
        natureza=natureza,
        conteudo=cifrar_pii(json.dumps(parecer, ensure_ascii=False)),
        parecer_criado_em=v["criado_em"],
        importado_por=utilizador.id,
        importado_em=agora,
    )
    db.add(registo)

    por_codigo = _mapa_controlos(db, empresa)
    nome_para_relatorio = parecer["auditor_nome"] or v["fingerprint"]

    # Só um parecer de AUDITORIA vira relatórios de auditoria externos
    # (imutáveis). O comentário de um consultor lê-se no ecrã do parecer.
    if natureza == NATUREZA_AUDITORIA:
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

    # Achados → tarefas (o plano de ação). Um achado com id que já tem tarefa
    # atualiza-a; se a empresa já a concluiu, não se reabre.
    existentes = _tarefas_dos_achados(db, empresa, [a["id"] for a in parecer["achados"] if a["id"]])
    criadas = atualizadas = resolvidas = 0
    for i, a in enumerate(parecer["achados"]):
        titulo = (f"{a['controlo']}: {a['descricao']}" if a["controlo"] else a["descricao"])[:255]
        detalhes = [a["descricao"]]
        if a["acao_recomendada"]:
            detalhes.append(a["acao_recomendada"])
        if a["prazo_sugerido"]:
            detalhes.append(a["prazo_sugerido"])
        descricao = "\n\n".join(detalhes)
        dono = donos.get(_chave_achado(a, i))
        t = existentes.get(a["id"]) if a["id"] else None
        if t is not None:
            if not t.ativa:
                resolvidas += 1
                continue
            t.titulo = titulo
            t.descricao = descricao
            t.controlos = a["controlo"]
            t.severidade = a["severidade"] or None
            t.proximo_prazo = _prazo_do_achado(a["prazo_sugerido"])
            t.origem_parecer_id = registo.id
            if dono is not None:
                t.responsavel_id = dono
            t.updated_at = agora
            db.add(t)
            atualizadas += 1
            continue
        db.add(Tarefa(
            empresa_id=empresa.id,
            titulo=titulo,
            descricao=descricao,
            categoria=_CATEGORIA_TAREFA[natureza],
            tipo=TipoTarefa.RECORRENTE,
            controlos=a["controlo"],
            periodicidade=Periodicidade.PONTUAL,
            proximo_prazo=_prazo_do_achado(a["prazo_sugerido"]),
            origem_parecer_id=registo.id,
            origem_achado_id=a["id"],
            severidade=a["severidade"] or None,
            responsavel_id=dono,
        ))
        criadas += 1

    # Pedidos de esclarecimento → notificações aos admins/subadmins ativos,
    # uma vez por pedido: um pedido com id que já chegou não volta a avisar.
    destinatarios = db.exec(
        select(Utilizador).where(
            Utilizador.empresa_id == empresa.id,
            Utilizador.ativo == True,  # noqa: E712
            Utilizador.role.in_([RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN]),  # type: ignore[union-attr]
        )
    ).all()
    notificacoes = 0
    for p in parecer["pedidos"]:
        if p["id"] and p["id"] in pedidos_vistos:
            continue
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
        "natureza": natureza,
        "auditor_fingerprint": v["fingerprint"],
        "nivel_validado": parecer["global"]["nivel_validado"] or None,
        "relatorios_criados": len(parecer["controlos"]) if natureza == NATUREZA_AUDITORIA else 0,
        "tarefas_criadas": criadas,
        "tarefas_atualizadas": atualizadas,
        "achados_ja_resolvidos": resolvidas,
        "notificacoes_criadas": notificacoes,
        "convite_guardado": v["proximo_convite"] is not None,
        "dossie_ref": str(v["dossie"].id),
    }


# ---------------------------------------------------------------------------
# Selo do dashboard e cartão da consultoria
# ---------------------------------------------------------------------------

def _ultimo(db: Session, empresa: Empresa, natureza: str) -> ParecerImportado | None:
    return db.exec(
        select(ParecerImportado)
        .where(ParecerImportado.empresa_id == empresa.id, ParecerImportado.natureza == natureza)
        .order_by(ParecerImportado.importado_em.desc())  # type: ignore[union-attr]
    ).first()


def _global(registo: ParecerImportado) -> dict:
    if not registo.global_json:
        return {}
    try:
        valor = json.loads(decifrar_pii(registo.global_json) or "{}")
    except ValueError:
        return {}
    return valor if isinstance(valor, dict) else {}


# Um selo de auditoria passa a «antigo» (o painel mostra-o em aviso) quando a
# revisão que o auditor previu já passou ou, sem data prevista, ao fim deste
# prazo. É uma escolha do produto, não um prazo legal.
SELO_DIAS_ATE_ANTIGO = 365
# A consultoria sai do painel quando não há recomendações por concluir e o
# último relatório tem mais do que isto: deixou de haver atividade.
CONSULTORIA_DIAS_SEM_ATIVIDADE = 30


def _utc(valor: datetime | None) -> datetime | None:
    """As colunas guardam UTC sem fuso; compara-se sempre em UTC com fuso."""
    if valor is None:
        return None
    return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)


def _data_iso(texto) -> date | None:
    try:
        return date.fromisoformat(texto) if isinstance(texto, str) and texto else None
    except ValueError:
        return None


def selo_antigo(registo: ParecerImportado, agora: datetime | None = None) -> bool:
    """O selo deixa de ser atual: passou a revisão prevista ou, sem ela, o
    prazo `SELO_DIAS_ATE_ANTIGO` desde o parecer."""
    agora = agora or datetime.now(timezone.utc)
    revisao = _data_iso(_global(registo).get("proxima_revisao"))
    if revisao is not None:
        return agora.date() > revisao
    base = _utc(registo.parecer_criado_em) or _utc(registo.importado_em)
    return base is not None and agora - base > timedelta(days=SELO_DIAS_ATE_ANTIGO)


def consultoria_em_curso(registo: ParecerImportado, abertas: int, agora: datetime | None = None) -> bool:
    """Há atividade: recomendações por concluir, ou o último relatório (criado
    ou importado, o mais recente) tem menos de `CONSULTORIA_DIAS_SEM_ATIVIDADE`."""
    if abertas > 0:
        return True
    agora = agora or datetime.now(timezone.utc)
    datas = [d for d in (_utc(registo.parecer_criado_em), _utc(registo.importado_em)) if d is not None]
    return bool(datas) and agora - max(datas) <= timedelta(days=CONSULTORIA_DIAS_SEM_ATIVIDADE)


def selo(db: Session, empresa: Empresa) -> dict:
    """Último parecer de AUDITORIA importado — "dossiê de {data} revisto por
    {auditor}". O trabalho de um consultor não dá selo. Fica até chegar outro;
    `antigo` diz ao painel que já não é atual."""
    ultimo = _ultimo(db, empresa, NATUREZA_AUDITORIA)
    if ultimo is None:
        return {"existe": False}
    dossie = db.get(DossieGerado, ultimo.dossie_gerado_id)
    return {
        "existe": True,
        "antigo": selo_antigo(ultimo),
        "proxima_revisao": _global(ultimo).get("proxima_revisao") or None,
        "parecer_id": str(ultimo.id),
        "auditor_fingerprint": ultimo.auditor_fingerprint,
        "auditor_nome": decifrar_pii(ultimo.auditor_nome) if ultimo.auditor_nome else None,
        "nivel_validado": _global(ultimo).get("nivel_validado") or None,
        "parecer_criado_em": ultimo.parecer_criado_em.isoformat() if ultimo.parecer_criado_em else None,
        "importado_em": ultimo.importado_em.isoformat() if ultimo.importado_em else None,
        "dossie_criado_em": dossie.criado_em.isoformat() if dossie and dossie.criado_em else None,
        "n_controlos": ultimo.n_controlos,
        "n_achados": ultimo.n_achados,
    }


def consultoria(db: Session, empresa: Empresa) -> dict:
    """Cartão «Consultoria em curso»: a última revisão de um consultor e as
    recomendações dela que continuam por concluir. Sem atividade
    (`consultoria_em_curso`), não há cartão; o histórico fica nos pareceres."""
    ultimo = _ultimo(db, empresa, NATUREZA_CONSULTORIA)
    if ultimo is None:
        return {"existe": False}
    abertas = db.exec(
        select(Tarefa).where(
            Tarefa.empresa_id == empresa.id,
            Tarefa.categoria == _CATEGORIA_TAREFA[NATUREZA_CONSULTORIA],
            Tarefa.ativa == True,  # noqa: E712
            Tarefa.deleted_at == None,  # noqa: E711
        )
    ).all()
    if not consultoria_em_curso(ultimo, len(abertas)):
        return {"existe": False}
    return {
        "existe": True,
        "parecer_id": str(ultimo.id),
        "consultor_nome": decifrar_pii(ultimo.auditor_nome) if ultimo.auditor_nome else None,
        "consultor_fingerprint": ultimo.auditor_fingerprint,
        "criado_em": ultimo.parecer_criado_em.isoformat() if ultimo.parecer_criado_em else None,
        "proxima_revisao": _global(ultimo).get("proxima_revisao") or None,
        "recomendacoes_abertas": len(abertas),
    }


# ---------------------------------------------------------------------------
# Leitura dos pareceres recebidos (ecrã «Pareceres e consultoria»)
# ---------------------------------------------------------------------------

def _resumo(db: Session, registo: ParecerImportado) -> dict:
    dossie = db.get(DossieGerado, registo.dossie_gerado_id)
    g = _global(registo)
    return {
        "id": str(registo.id),
        "natureza": registo.natureza,
        "autor_nome": decifrar_pii(registo.auditor_nome) if registo.auditor_nome else None,
        "autor_fingerprint": registo.auditor_fingerprint,
        "criado_em": registo.parecer_criado_em.isoformat() if registo.parecer_criado_em else None,
        "importado_em": registo.importado_em.isoformat() if registo.importado_em else None,
        "dossie_criado_em": dossie.criado_em.isoformat() if dossie and dossie.criado_em else None,
        "nivel_validado": g.get("nivel_validado") or None,
        "proxima_revisao": g.get("proxima_revisao") or None,
        "contagens": {
            "controlos": registo.n_controlos,
            "achados": registo.n_achados,
            "pedidos": registo.n_pedidos,
        },
    }


def listar(db: Session, empresa: Empresa) -> list[dict]:
    """Todos os pareceres e consultorias importados, do mais recente."""
    registos = db.exec(
        select(ParecerImportado)
        .where(ParecerImportado.empresa_id == empresa.id)
        .order_by(ParecerImportado.importado_em.desc())  # type: ignore[union-attr]
    ).all()
    return [_resumo(db, r) for r in registos]


def detalhe(db: Session, empresa: Empresa, parecer_id: uuid.UUID) -> dict | None:
    """O parecer por inteiro, com o estado da tarefa de cada achado."""
    registo = db.exec(
        select(ParecerImportado).where(
            ParecerImportado.id == parecer_id, ParecerImportado.empresa_id == empresa.id
        )
    ).first()
    if registo is None:
        return None
    out = _resumo(db, registo)
    conteudo = _conteudo(registo)
    if conteudo is None:
        # Importado antes da 0.4.3: só o global ficou guardado.
        out.update({"completo": False, "global": _global(registo),
                    "controlos": [], "achados": [], "pedidos": []})
        return out

    achados_in = [a for a in conteudo.get("achados") or [] if isinstance(a, dict)]
    # A tarefa de um achado com id pode ter sido atualizada por um parecer
    # mais recente; sem id, é a que nasceu deste parecer (pela ordem).
    por_achado = _tarefas_dos_achados(db, empresa, [a["id"] for a in achados_in if a.get("id")])
    sem_id = db.exec(
        select(Tarefa).where(
            Tarefa.empresa_id == empresa.id,
            Tarefa.origem_parecer_id == registo.id,
            Tarefa.origem_achado_id == None,  # noqa: E711
            Tarefa.deleted_at == None,  # noqa: E711
        ).order_by(Tarefa.created_at)  # type: ignore[arg-type]
    ).all()
    fila_sem_id = list(sem_id)
    achados = []
    for a in achados_in:
        if a.get("id"):
            t = por_achado.get(a["id"])
        else:
            t = fila_sem_id.pop(0) if fila_sem_id else None
        achados.append({
            **a,
            "tarefa": None if t is None else {
                "id": str(t.id),
                "ativa": t.ativa,
                "proximo_prazo": t.proximo_prazo.isoformat() if t.proximo_prazo else None,
                "responsavel_id": str(t.responsavel_id) if t.responsavel_id else None,
            },
        })
    out.update({
        "completo": True,
        "global": conteudo.get("global") or {},
        "controlos": conteudo.get("controlos") or [],
        "achados": achados,
        "pedidos": conteudo.get("pedidos") or [],
    })
    return out


# ---------------------------------------------------------------------------
# Convite guardado para o envio seguinte
# ---------------------------------------------------------------------------

def _expira_utc(valor: datetime | None) -> datetime | None:
    if valor is not None and valor.tzinfo is None:
        return valor.replace(tzinfo=timezone.utc)
    return valor


def convites_guardados(db: Session, empresa: Empresa) -> list[dict]:
    """Auditores/consultores com um convite guardado e ainda dentro do prazo.
    O token nunca sai daqui: o ecrã só vê para quem pode enviar."""
    agora = datetime.now(timezone.utc)
    out = []
    for a in db.exec(
        select(AuditorConfiavel).where(
            AuditorConfiavel.empresa_id == empresa.id,
            AuditorConfiavel.proximo_convite != None,  # noqa: E711
        )
    ).all():
        expira = _expira_utc(a.proximo_convite_expira_em)
        if expira is not None and expira <= agora:
            continue
        token = decifrar_pii(a.proximo_convite) if a.proximo_convite else None
        try:
            relay = bool(crypto.verificar_convite(token)["relay"]) if token else False
        except crypto.ConviteInvalido:
            continue
        out.append({
            "auditor_id": str(a.id),
            "nome": decifrar_pii(a.nome) if a.nome else None,
            "fingerprint": a.fingerprint,
            "expira_em": expira.isoformat() if expira else None,
            "relay": relay,
        })
    return out


def tirar_convite_guardado(db: Session, empresa: Empresa, auditor_id: uuid.UUID) -> str:
    """O token do convite guardado para esse auditor, que deixa de estar
    guardado (é de uso único). O caller só faz commit se o envio correr bem."""
    a = db.exec(
        select(AuditorConfiavel).where(
            AuditorConfiavel.id == auditor_id, AuditorConfiavel.empresa_id == empresa.id
        )
    ).first()
    token = decifrar_pii(a.proximo_convite) if a is not None and a.proximo_convite else None
    if a is None or not token:
        raise _erro(status.HTTP_404_NOT_FOUND, "sem_convite_guardado")
    expira = _expira_utc(a.proximo_convite_expira_em)
    if expira is not None and expira <= datetime.now(timezone.utc):
        raise _erro(status.HTTP_410_GONE, "convite_expirado")
    a.proximo_convite = None
    a.proximo_convite_expira_em = None
    db.add(a)
    return token
