"""
Tick das verificações técnicas (premium): consome o que o sidecar tem de novo e
produz os efeitos que vivem no core — notificações aos gestores, evidência
técnica automática nos controlos e entradas no log de auditoria.

O sidecar é a autoridade (avalia, materializa os eventos com ids sequenciais e
diz, por verificação, em que controlos a evidência se anexa). O core lembra-se
de onde ficou por empresa (`ConetorCursor`) para nunca perder nem repetir um
evento ou uma execução entre reinícios.

Uma só leitura por ciclo (`processamento`): o tick não pede o painel do ecrã —
não precisa dos textos, e o ecrã não precisa de esperar pelo tick.

A evidência automática é um JSON estável ("captura de verificação técnica")
por controlo: sem o instante da verificação, para o hash de conteúdo só mudar
quando o ESTADO muda — o dedup por hash já existente faz o resto.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid

from sqlmodel import Session, select

from app.auth.models import RoleUtilizador, Utilizador
from app.config import get_settings
from app.empresas.models import Empresa
from app.evidencias import ligacoes
from app.evidencias.models import Evidencia, TipoEvidencia
from app.frameworks.runtime import nivel_qnrcs_efetivo
from app.notificacoes.catalogo import Codigo
from app.notificacoes.service import criar_notificacao
from app.premium import contexto_nucleo
from app.premium.conetor_client import FEATURES_CONETORES
from app.premium.conetor_models import ConetorCursor
from app.shared.audit import Acao, registar_acao

logger = logging.getLogger(__name__)

# Eventos que geram notificação in-app (o "aviso" — recuperação — fica só na
# linha temporal; notificar recuperações seria ruído).
_TIPOS_NOTIFICAVEIS = ("drift", "contradicao")

_CODIGOS = {
    "drift": Codigo.CONETOR_DRIFT,
    "contradicao": Codigo.CONETOR_CONTRADICAO,
}

# A fonte da ligação em linha tal como ficou gravada no formato da evidência.
# O corpo das capturas feitas só com ela não leva a fonte e diz "conetor_m365":
# mudar isto mudava o hash e anexava uma cópia a cada controlo de cada empresa.
_FONTE_DO_FORMATO_ORIGINAL = "entra"

# O título fica gravado na evidência e é o que a lista de provas mostra; sai
# no idioma da empresa, como os outros documentos que a aplicação gera. Só o
# título: o corpo é dados, e mudá-lo mudava o hash e duplicava capturas.
_TITULO_CAPTURA = {
    "pt": "Verificação técnica automática ({code})",
    "en": "Automatic technical check ({code})",
}


def processar_conetores(db: Session, premium=None, cli=None) -> dict:
    """Corre um ciclo de consumo para todas as empresas com alguma fonte.

    `premium`/`cli` são injetáveis para os testes; em produção resolvem-se aos
    singletons reais. Sem premium configurado, não faz nada (fail-soft).
    """
    settings = get_settings()
    if premium is None or cli is None:
        if not settings.PREMIUM_ENABLED or not settings.PREMIUM_SIDECAR_ADDR:
            return {"empresas": 0}
        from app.premium.client import get_premium_client
        from app.premium.conetor_client import get_conetor_client

        premium = premium or get_premium_client()
        cli = cli or get_conetor_client()
    if premium is None or cli is None:
        return {"empresas": 0}

    totais = {"empresas": 0, "eventos": 0, "notificacoes": 0, "evidencias": 0}
    empresas = db.exec(
        select(Empresa).where(
            Empresa.ativo == True,  # noqa: E712
            Empresa.suspenso == False,  # noqa: E712
        )
    ).all()
    for empresa in empresas:
        try:
            # Qualquer fonte: o sidecar devolve só o que é das fontes a que a
            # empresa tem direito.
            if not any(premium.has_feature(str(empresa.id), f) for f in FEATURES_CONETORES):
                continue
            resultado = _processar_empresa(db, cli, empresa)
            db.commit()
            totais["empresas"] += 1
            for chave in ("eventos", "notificacoes", "evidencias"):
                totais[chave] += resultado[chave]
        except Exception:  # noqa: BLE001 — uma empresa nunca trava as outras
            db.rollback()
            logger.warning(
                "Verificações: tick da empresa %s falhou; segue no próximo ciclo.",
                empresa.id,
                exc_info=True,
            )
    return totais


def _processar_empresa(db: Session, cli, empresa: Empresa) -> dict:
    """Um ciclo para uma empresa, com ou sem ligações em linha configuradas: uma
    empresa que só carrega relatórios tem sinais e eventos na mesma."""
    resultado = {"eventos": 0, "notificacoes": 0, "evidencias": 0}
    tenant = str(empresa.id)

    cursor = db.get(ConetorCursor, empresa.id)
    if cursor is None:
        cursor = ConetorCursor(empresa_id=empresa.id)
        db.add(cursor)

    # 0) O que depende do relógio (a idade de cada leitura ou relatório)
    #    reavalia-se com o contexto de hoje. As transições saem como eventos e
    #    são apanhadas logo a seguir, no mesmo ciclo.
    _reavaliar_observacoes(db, cli, empresa)

    proc = cli.processamento(
        tenant,
        a_partir_de=cursor.ultimo_evento_id or 0,
        verificacoes_desde=cursor.ultima_verificacao_vista or "",
        limite=200,
    )

    # 1) Execuções agendadas das ligações → auditoria. As pedidas por uma
    #    pessoa já foram auditadas por quem as pediu; o sidecar só manda estas.
    for v in proc.get("verificacoes", []):
        registar_acao(
            db, acao=Acao.CONETOR_VERIFICACAO, empresa_id=empresa.id,
            entidade_tipo="Conetor", entidade_id=None,
            dados_novos={
                "origem": "agendada",
                "tipo": v.get("fonte"),
                "resultado": v.get("resultado") or "",
                "erro_categoria": v.get("erro_categoria") or None,
            },
        )
        cursor.ultima_verificacao_vista = v.get("iniciada_em")

    # 2) Eventos novos a partir do cursor → notificações + auditoria.
    eventos = proc.get("eventos", [])
    if eventos:
        por_codigo = _controlos_por_codigo(db, empresa)
        gestores = _gestores_ativos(db, empresa)
        for ev in eventos:
            controlos = ev.get("controlos") or []
            ce_id = next((por_codigo[c] for c in controlos if c in por_codigo), None)
            if ev.get("tipo") in _TIPOS_NOTIFICAVEIS:
                params = _params_do_evento(ev)
                for gestor in gestores:
                    criar_notificacao(
                        db,
                        empresa_id=empresa.id,
                        utilizador_id=gestor.id,
                        codigo=_CODIGOS[ev["tipo"]],
                        params=params,
                        entidade_id=ce_id,
                        controlo_empresa_id=ce_id,
                    )
                    resultado["notificacoes"] += 1
                registar_acao(
                    db, acao=Acao.CONETOR_DRIFT_DETETADO, empresa_id=empresa.id,
                    entidade_tipo="Conetor", entidade_id=None,
                    dados_novos={
                        "evento_id": ev.get("id"),
                        "fonte": ev.get("fonte"),
                        "tipo": ev.get("tipo"),
                        "sinal": ev.get("sinal"),
                        "de": ev.get("de_veredicto"),
                        "para": ev.get("para_veredicto"),
                        "controlos": controlos,
                    },
                )
            cursor.ultimo_evento_id = max(cursor.ultimo_evento_id or 0, int(ev["id"]))
            resultado["eventos"] += 1

    # 3) Evidência técnica automática (só quando o estado muda — dedup por hash).
    resultado["evidencias"] = _evidencia_automatica(db, empresa, proc.get("constatacoes", []))
    return resultado


def _reavaliar_observacoes(db: Session, cli, empresa: Empresa) -> None:
    """Pede a reavaliação temporal, com o perfil e as declarações atuais.

    Fail-soft: se falhar, o resto do ciclo segue — perder uma reavaliação atrasa
    um aviso um ciclo; perder o ciclo inteiro atrasava todos os eventos.
    """
    try:
        cli.reavaliar_observacoes(
            str(empresa.id),
            nivel_qnrcs_efetivo(empresa),
            contexto_nucleo.declaracoes_dos_controlos(db, empresa),
        )
    except Exception:  # noqa: BLE001 — ver docstring
        logger.warning(
            "Verificações: reavaliação da empresa %s falhou; segue.",
            empresa.id,
            exc_info=True,
        )


def _params_do_evento(ev: dict) -> dict:
    """Valores da frase, sem dados nominais (esses ficam no ecrã)."""
    return {
        "sinal": ev.get("sinal", ""),
        "de": ev.get("de_veredicto") or "—",
        "para": ev.get("para_veredicto", ""),
        "controlos": ", ".join(ev.get("controlos") or []),
    }


def _gestores_ativos(db: Session, empresa: Empresa) -> list[Utilizador]:
    return list(
        db.exec(
            select(Utilizador).where(
                Utilizador.empresa_id == empresa.id,
                Utilizador.ativo == True,  # noqa: E712
                Utilizador.role.in_(  # type: ignore[union-attr]
                    [RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN]
                ),
            )
        ).all()
    )


def _linhas_de_controlos(db: Session, empresa: Empresa):
    from app.frameworks.runtime import load_company_control_rows, resolver_framework_empresa

    framework = resolver_framework_empresa(db, empresa)
    return load_company_control_rows(db, empresa.id, framework.id)


def _controlos_por_codigo(db: Session, empresa: Empresa) -> dict[str, uuid.UUID]:
    """Código do controlo → id do controlo da empresa. Falha = mapa vazio (as
    notificações seguem sem ligação ao controlo, nunca se perde o evento)."""
    try:
        return {row.control.code: row.ce.id for row in _linhas_de_controlos(db, empresa)}
    except Exception:  # noqa: BLE001
        logger.warning("Verificações: resolução de controlos da empresa %s falhou.", empresa.id, exc_info=True)
        return {}


def _codigos_nao_aplicaveis(db: Session, empresa: Empresa) -> set[str]:
    """Controlos que a empresa marcou como não aplicáveis. A evidência técnica
    não se anexa a eles: a exclusão está justificada, e uma captura automática
    ali contradizia-a em silêncio (é o ecrã do controlo que mostra, à vista, que
    a verificação observa alguma coisa)."""
    from app.shared.enums import EstadoControlo

    try:
        return {
            row.control.code
            for row in _linhas_de_controlos(db, empresa)
            if getattr(row.ce, "estado", None) == EstadoControlo.NAO_APLICAVEL
        }
    except Exception:  # noqa: BLE001
        logger.warning("Verificações: estado dos controlos da empresa %s falhou.", empresa.id, exc_info=True)
        return set()


def _evidencia_automatica(db: Session, empresa: Empresa, constatacoes: list[dict]) -> int:
    """Anexa a captura de verificação técnica aos controlos.

    Por constatação, os controlos-alvo vêm do sidecar (`controlos_evidencia`: os
    marcados como obrigatórios nas metas DESSA fonte; sem marcação, todos os
    ligados). O conteúdo é JSON ESTÁVEL (sem timestamps) — o dedup por hash
    garante que só se anexa quando algo mudou.
    """
    por_controlo: dict[str, list[dict]] = {}
    for s in constatacoes:
        # Um sinal totalmente indeterminado não afirma nada — não é evidência.
        if s.get("veredicto_minimo") == "indeterminado" and s.get("veredicto_politica") == "indeterminado":
            continue
        alvos = s.get("controlos_evidencia") or s.get("controlos") or []
        for code in alvos:
            por_controlo.setdefault(code, []).append(s)
    if not por_controlo:
        return 0
    if not all(s.get("corpo_evidencia_json") for lista in por_controlo.values() for s in lista):
        # O corpo de cada sinal vem do sidecar. Um sidecar mais antigo não o manda,
        # e montá-lo aqui com outra mão mudava o conteúdo (e o hash que evita as
        # cópias): espera-se pela atualização, que retoma sem perder nada.
        logger.warning(
            "Verificações: o sidecar da empresa %s não manda o corpo da evidência; captura adiada.",
            empresa.id,
        )
        return 0

    por_codigo = _controlos_por_codigo(db, empresa)
    nao_aplicaveis = _codigos_nao_aplicaveis(db, empresa)
    gestores = _gestores_ativos(db, empresa)
    if not gestores:
        return 0  # evidência tem autor; sem gestor ativo não se inventa um
    uploader = gestores[0]

    from app.evidencias.service import _duplicado_mais_recente

    settings = get_settings()
    criadas = 0
    for code in sorted(por_controlo):
        ce_id = por_codigo.get(code)
        if ce_id is None or code in nao_aplicaveis:
            continue
        # UMA captura por controlo, com os sinais de todas as fontes. Uma por
        # fonte não servia: o dedup compara com a evidência mais recente do
        # controlo, e duas fontes no mesmo controlo alternavam — uma cópia nova
        # a cada passagem, para sempre.
        sinais = sorted(por_controlo[code], key=_ordem_do_sinal)
        so_em_linha = all(_fonte(s) == _FONTE_DO_FORMATO_ORIGINAL for s in sinais)
        corpo = {
            # Só com a ligação em linha, o corpo é exatamente o de sempre: mudar
            # uma vírgula mudava o hash e criava uma cópia em cada controlo no
            # dia da atualização.
            "origem": "conetor_m365" if so_em_linha else "conetores",
            "controlo": code,
            "sinais": [json.loads(s["corpo_evidencia_json"]) for s in sinais],
        }
        texto = json.dumps(corpo, ensure_ascii=False, sort_keys=True, indent=2)
        conteudo_hash = hashlib.sha256(texto.encode("utf-8")).hexdigest()
        if _duplicado_mais_recente(db, empresa.id, ce_id, conteudo_hash):
            continue  # nada mudou desde a última captura
        guardar = texto
        cifrado = False
        if settings.EVIDENCE_ENCRYPTION_KEY:
            from app.evidencias.service import _cifrar_texto_evidencia

            guardar = _cifrar_texto_evidencia(texto)
            cifrado = True
        evidencia = Evidencia(
            controlo_empresa_v2_id=ce_id,
            empresa_id=empresa.id,
            tipo=TipoEvidencia.TEXTO,
            titulo=_titulo_captura(empresa, code),
            conteudo_texto=guardar,
            conteudo_texto_cifrado=cifrado,
            conteudo_hash=conteudo_hash,
            uploaded_by_id=uploader.id,
        )
        db.add(evidencia)
        db.flush()
        # Sem a ligação a evidência não sustenta controlo nenhum: não
        # apareceria no ecrã do controlo e a captura seguinte não a encontraria,
        # pelo que o tick criaria uma cópia nova a cada passagem. Este caminho
        # não passa pelo `criar_evidencia` — é escrita direta — e por isso tem de
        # ligar explicitamente.
        ligacoes.ligar(
            db,
            evidencia_id=evidencia.id,
            requisito_id=ce_id,
            empresa_id=empresa.id,
            ligado_por_id=uploader.id,
        )
        criadas += 1
    return criadas


def _titulo_captura(empresa: Empresa, code: str) -> str:
    lingua = (empresa.locale_preferido or "pt").split("-")[0].lower()
    return _TITULO_CAPTURA.get(lingua, _TITULO_CAPTURA["pt"]).format(code=code)


def _fonte(sinal: dict) -> str:
    return sinal.get("fonte") or _FONTE_DO_FORMATO_ORIGINAL


def _ordem_do_sinal(sinal: dict) -> tuple:
    # A ligação em linha primeiro e, dentro de cada fonte, por nome — com uma
    # só fonte fica a ordem de sempre (por nome).
    return (_fonte(sinal) != _FONTE_DO_FORMATO_ORIGINAL, _fonte(sinal), sinal.get("sinal", ""))
