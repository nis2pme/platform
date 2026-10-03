"""
Conteúdo do dossiê — os JSON que descrevem o estado de conformidade da empresa.

Regra de ouro: os dados vêm das MESMAS funções de serviço que alimentam os
ecrãs, para que o dossiê nunca divirja do que a app mostra. Convenções:
enums como códigos estáveis (o leitor traduz), IDs da base de dados sempre
presentes (permitem correlacionar dossiês sucessivos da mesma empresa),
datas em ISO-8601 UTC.

Ficheiros produzidos:
    dados/empresa.json        perfil e classificação NIS2 da empresa
    dados/conformidade.json   framework + domínios → controlos (estados/scores)
    dados/controlos.json      detalhe por controlo: aprovações, relatórios de
                              auditoria internos, referências a evidências
    dados/evidencias.json     metadados (e texto) das evidências; os ficheiros
                              viajam como entradas cifradas ev/<uuid>.age
    dados/incidentes.json     incidentes + linha temporal + marcos e prazos legais
                              + notificações enviadas (formato "rjc-1")
    dados/tarefas.json        obrigações periódicas + histórico de conclusões
    dados/formacao.json       ações, participantes, órgão de gestão formado
    dados/governacao.json     matriz de funções/responsabilidades + utilizadores
    dados/atividade.json      extrato do registo de atividade (período à escolha)
    dados/premium/*.json      inventário/riscos/fornecedores (quando ativo)
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from sqlmodel import Session, select

from app.auth.models import Utilizador
from app.controlos.models import RelatorioAuditoria
from app.empresas.models import Empresa
from app.evidencias.models import Evidencia, EvidenciaRequisito, TipoEvidencia
from app.formacao.models import AcaoFormacao, ParticipanteFormacao
from app.frameworks.models import ControloEmpresaV2, Framework
from app.incidentes.models import Incidente, IncidenteEvento
from app.shared import audit_cadeia
from app.shared.audit import AuditLog
from app.shared.audit_catalogo import definicao, entidade_apresentacao, resolver_codigo
from app.shared.pii import decifrar_pii
from app.tarefas.models import Tarefa, TarefaConclusao

logger = logging.getLogger(__name__)

# Tampa de segurança do extrato de atividade (um ano de PME fica muito abaixo).
_ATIVIDADE_MAX_ENTRADAS = 50_000


def _json_bytes(dados: dict) -> bytes:
    """Serialização estável (chaves ordenadas) e legível dos ficheiros do dossiê."""
    return json.dumps(
        dados, sort_keys=True, ensure_ascii=False, indent=1, default=str
    ).encode("utf-8")


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _enum(valor) -> str | None:
    if valor is None:
        return None
    return valor.value if hasattr(valor, "value") else valor


def _nomes_utilizadores(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    """Nomes decifrados dos utilizadores pedidos, num só lote."""
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {
        u.id: decifrar_pii(u.nome)
        for u in db.exec(
            select(Utilizador).where(Utilizador.id.in_(list(ids)))  # type: ignore[attr-defined]
        ).all()
    }


def _registo_atividade(r: AuditLog, utilizador: str | None) -> dict:
    """
    Uma linha do extrato de atividade.

    `acao`, `entidade_tipo` e `resultado` saem tal como foram gravados: há
    leitores já distribuídos que dependem deles. O código canónico, a família, a
    severidade e a entidade apresentada vêm do mesmo catálogo do ecrã de
    registos, para quem lê o dossiê traduzir e dar peso à linha da mesma forma
    (um código gravado antes de uma correção resolve para o canónico).
    """
    catalogada = definicao(r.acao, r.entidade_tipo)
    return {
        "acao": r.acao,
        "acao_canonica": resolver_codigo(r.acao, r.entidade_tipo),
        "familia": catalogada.familia,
        "severidade": catalogada.severidade,
        "entidade_tipo": r.entidade_tipo,
        "entidade": entidade_apresentacao(r.entidade_tipo),
        "entidade_id": str(r.entidade_id) if r.entidade_id else None,
        "utilizador": utilizador,
        "resultado": _enum(r.resultado),
        "em": _iso(r.created_at),
    }


def montar_conteudo(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    request: Request | None = None,
    incluir_evidencias: bool = True,
    periodo_atividade_meses: int | None = 12,
) -> tuple[dict[str, bytes], dict[str, int], list[dict]]:
    """
    Constrói os ficheiros `dados/*.json` do dossiê.
    Devolve ({caminho relativo: bytes}, contagens, ficheiros de evidência a
    cifrar — cada um {id, path, cifrado, nome, bytes}, só os que existem em disco).
    """
    framework = db.get(Framework, empresa.framework_id)
    if not framework:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"codigo": "framework_nao_encontrado"},
        )

    # --- conformidade.json: o relatório de conformidade dos ecrãs, tal e qual —
    # sem entrada própria na auditoria (o dossiê regista o evento agregado).
    from app.relatorios.service import gerar_relatorio_conformidade

    conformidade = gerar_relatorio_conformidade(
        db, empresa, utilizador, request, auditar=False
    ).model_dump(mode="json")

    # Mapa controlo_id → código (achatado do relatório), para rotular referências.
    codigo_por_controlo: dict[str, str] = {}
    for dominio in conformidade["dominios"]:
        for controlo in dominio["controlos"]:
            codigo_por_controlo[str(controlo["controlo_id"])] = controlo["codigo"]

    # --- estado por controlo da empresa (a mesma tabela que os ecrãs usam)
    ces = db.exec(
        select(ControloEmpresaV2).where(
            ControloEmpresaV2.empresa_id == empresa.id,
            ControloEmpresaV2.framework_id == framework.id,
        )
    ).all()
    codigo_por_ce: dict[uuid.UUID, str] = {
        ce.id: codigo_por_controlo.get(str(ce.control_id), str(ce.control_id))
        for ce in ces
    }

    # --- relatórios de auditoria internos (imutáveis), agrupados por controlo
    relatorios = db.exec(
        select(RelatorioAuditoria)
        .where(RelatorioAuditoria.empresa_id == empresa.id)
        .order_by(RelatorioAuditoria.created_at)
    ).all()
    relatorios_por_ce: dict[uuid.UUID | None, list[dict]] = {}
    for rel in relatorios:
        relatorios_por_ce.setdefault(rel.controlo_empresa_v2_id, []).append({
            "id": str(rel.id),
            "decisao": _enum(rel.decisao),
            "auditor_nome": decifrar_pii(rel.auditor_nome),
            # O texto está cifrado em repouso (PII) — o dossiê leva o claro.
            "texto": decifrar_pii(rel.texto),
            "criado_em": _iso(rel.created_at),
            # Relatórios externos: vieram de um parecer assinado de auditor.
            "externo": bool(rel.externo),
            "estado_externo": rel.estado_externo,
        })

    # --- evidências ativas da empresa
    evidencias = db.exec(
        select(Evidencia).where(
            Evidencia.empresa_id == empresa.id,
            Evidencia.deleted_at.is_(None),  # type: ignore[union-attr]
        )
    ).all()
    # Uma evidência sustenta os controlos a que está LIGADA, que podem ser
    # vários. Agrupar pela coluna antiga faria uma política partilhada por três
    # controlos aparecer no dossiê num só — e o auditor veria dois controlos sem
    # a prova que os sustenta, sem nada a assinalar a falta.
    ligacoes_ativas = db.exec(
        select(EvidenciaRequisito).where(
            EvidenciaRequisito.empresa_id == empresa.id,
            EvidenciaRequisito.desligado_em.is_(None),  # type: ignore[union-attr]
        )
    ).all()
    ids_vivas = {ev.id for ev in evidencias}
    evidencias_por_ce: dict[uuid.UUID | None, list[str]] = {}
    # Âmbito por (evidência, controlo): é o que diz ao auditor ONDE olhar dentro
    # de um documento que serve vários controlos.
    ambito_por_ligacao: dict[tuple[uuid.UUID, uuid.UUID], dict] = {}
    for lig in ligacoes_ativas:
        if lig.evidencia_id not in ids_vivas:
            continue  # ligação a uma evidência apagada: não entra no dossiê
        evidencias_por_ce.setdefault(lig.requisito_id, []).append(str(lig.evidencia_id))
        if lig.nota_ambito:
            ambito_por_ligacao[(lig.evidencia_id, lig.requisito_id)] = {
                "nota": lig.nota_ambito,
                "por_confirmar": bool(lig.ambito_por_confirmar),
            }

    # Que controlos cada evidência sustenta — usado no extrato de evidências, que
    # antes dizia um só código por evidência.
    controlos_por_evidencia: dict[uuid.UUID, list[uuid.UUID]] = {}
    for ce_id, ids in evidencias_por_ce.items():
        for ev_id in ids:
            controlos_por_evidencia.setdefault(uuid.UUID(ev_id), []).append(ce_id)

    autores = _nomes_utilizadores(db, {ev.uploaded_by_id for ev in evidencias})

    # Ficheiros a incluir: só evidências-ficheiro cujo ficheiro existe mesmo em
    # disco — a lista decide as entradas ev/<uuid>.age e o campo `entrada`.
    ficheiros_evidencia: list[dict] = []
    if incluir_evidencias:
        for ev in evidencias:
            if (
                ev.tipo in (TipoEvidencia.FICHEIRO, TipoEvidencia.AMBOS)
                and ev.ficheiro_path
                and os.path.isfile(ev.ficheiro_path)
            ):
                ficheiros_evidencia.append({
                    "id": str(ev.id),
                    "path": ev.ficheiro_path,
                    "cifrado": bool(ev.ficheiro_cifrado),
                    "nome": decifrar_pii(ev.ficheiro_nome) or "evidencia",
                    "bytes": ev.ficheiro_tamanho or 0,
                })
    ids_com_ficheiro = {f["id"] for f in ficheiros_evidencia}

    # Texto das evidências: viaja dentro de dados.age (cifrado como tudo o resto).
    from app.evidencias.service import _decifrar_texto_evidencia

    def _texto_evidencia(ev: Evidencia) -> str | None:
        if ev.conteudo_texto is None:
            return None
        if not ev.conteudo_texto_cifrado:
            return ev.conteudo_texto
        try:
            return _decifrar_texto_evidencia(ev.conteudo_texto)
        except Exception:  # noqa: BLE001 — texto ilegível não pode travar o dossiê
            logger.warning("Evidência %s: texto cifrado ilegível — omitido.", ev.id)
            return None

    # --- incidentes.json: incidentes + linha temporal append-only + marcos legais
    #     + prazos calculados à data da geração + notificações enviadas (com a
    #     cópia do que se entregou). O auditor lê os prazos daqui: não os calcula.
    from app.incidentes import service as incidentes_svc

    incidentes = db.exec(
        select(Incidente).where(
            Incidente.empresa_id == empresa.id,
            Incidente.deleted_at.is_(None),  # type: ignore[union-attr]
        ).order_by(Incidente.conhecido_at)
    ).all()
    eventos = db.exec(
        select(IncidenteEvento)
        .where(IncidenteEvento.empresa_id == empresa.id)
        .order_by(IncidenteEvento.created_at)
    ).all()
    eventos_por_incidente: dict[uuid.UUID, list[IncidenteEvento]] = {}
    for evt in eventos:
        eventos_por_incidente.setdefault(evt.incidente_id, []).append(evt)

    nomes_incid = _nomes_utilizadores(
        db,
        {i.responsavel_id for i in incidentes} | {e.autor_id for e in eventos},
    )
    agora_incid = datetime.now(timezone.utc)
    ctx_incid = incidentes_svc._contexto(db, empresa.id, [i.id for i in incidentes])
    notificacoes_incid = incidentes_svc.notificacoes_para_dossie(db, empresa.id)
    incidentes_json = {
        "formato": "rjc-1",
        "incidentes": [
            {
                "id": str(i.id),
                "titulo": i.titulo,
                "descricao": i.descricao,
                "categoria": i.categoria,
                "severidade": _enum(i.severidade),
                "estado": _enum(i.estado),
                "significativo": i.significativo,
                "significativo_em": _iso(i.significativo_em),
                "responsavel": nomes_incid.get(i.responsavel_id),
                "conhecido_em": _iso(i.conhecido_at),
                "ocorrido_em": _iso(i.ocorrido_at),
                "impacto_inicio_em": _iso(i.impacto_inicio_em),
                "fim_impacto_em": _iso(i.fim_impacto_em),
                "resolvido_2h": bool(i.resolvido_2h),
                "atualizacao_necessaria": bool(i.atualizacao_necessaria),
                "excecao_24h": i.excecao_24h,
                "intercalar_pedido_em": _iso(i.intercalar_pedido_em),
                "cnpd_aplicavel": bool(i.cnpd_aplicavel),
                "representante_nome": i.representante_nome,
                "representante_telefone": i.representante_telefone,
                "representante_email": i.representante_email,
                "utilizadores_afetados": i.utilizadores_afetados,
                "utilizadores_total": i.utilizadores_total,
                "zona_geografica": i.zona_geografica,
                "transfronteirico": i.transfronteirico,
                "paises_afetados": i.paises_afetados,
                "causa": i.causa,
                "efeitos": i.efeitos,
                "medidas": i.medidas,
                "situacao_residual": i.situacao_residual,
                "tempo_recuperacao": i.tempo_recuperacao,
                # Entidade fora do âmbito do regime: marcos voluntários.
                "voluntario": ctx_incid.voluntario,
                # Marcos cumpridos (datas em que as notificações foram enviadas).
                "marcos": {
                    "notificacao_inicial_em": _iso(i.notificacao_inicial_at),
                    "atualizacao_em": _iso(i.atualizacao_at),
                    "fim_impacto_notificado_em": _iso(i.fim_impacto_notificado_at),
                    "relatorio_final_em": _iso(i.relatorio_final_at),
                    "cnpd_notificado_em": _iso(i.cnpd_notificado_at),
                },
                "prazos": [
                    incidentes_svc.prazo_para_json(p)
                    for p in incidentes_svc._calcular(i, ctx_incid, agora_incid)
                ],
                "notificacoes": notificacoes_incid.get(i.id, []),
                "licoes_aprendidas": i.licoes_aprendidas,
                "criterios_fecho": i.criterios_fecho,
                "fechado_em": _iso(i.fechado_at),
                "eventos": [
                    {
                        "id": str(e.id),
                        "tipo": _enum(e.tipo),
                        # Na língua da empresa, que é o que os leitores que já
                        # existem esperam. As linhas do sistema levam também a
                        # frase nas duas línguas, para o leitor escolher a sua.
                        "texto": incidentes_svc.texto_evento(e, empresa.locale_preferido),
                        "texto_i18n": (
                            {lg: incidentes_svc.texto_evento(e, lg) for lg in ("pt", "en")}
                            if e.codigo else None
                        ),
                        "parte": e.parte,
                        "autor": nomes_incid.get(e.autor_id),
                        "criado_em": _iso(e.created_at),
                    }
                    for e in eventos_por_incidente.get(i.id, [])
                ],
            }
            for i in incidentes
        ]
    }

    # --- tarefas.json: obrigações periódicas + histórico de conclusões
    tarefas = db.exec(
        select(Tarefa).where(
            Tarefa.empresa_id == empresa.id,
            Tarefa.deleted_at.is_(None),  # type: ignore[union-attr]
        ).order_by(Tarefa.proximo_prazo)
    ).all()
    conclusoes = db.exec(
        select(TarefaConclusao)
        .where(TarefaConclusao.empresa_id == empresa.id)
        .order_by(TarefaConclusao.concluida_em)
    ).all()
    conclusoes_por_tarefa: dict[uuid.UUID, list[TarefaConclusao]] = {}
    for c in conclusoes:
        conclusoes_por_tarefa.setdefault(c.tarefa_id, []).append(c)

    nomes_tarefas = _nomes_utilizadores(
        db,
        {t.responsavel_id for t in tarefas} | {c.autor_id for c in conclusoes},
    )
    tarefas_json = {
        "tarefas": [
            {
                "id": str(t.id),
                "chave_catalogo": t.chave_catalogo,
                "titulo": t.titulo,
                "descricao": t.descricao,
                "categoria": t.categoria,
                "tipo": _enum(t.tipo),
                "controlos": [c for c in t.controlos.split(",") if c],
                "periodicidade": _enum(t.periodicidade),
                "periodicidade_dias": t.periodicidade_dias,
                "responsavel": nomes_tarefas.get(t.responsavel_id),
                "proximo_prazo": _iso(t.proximo_prazo),
                "ultima_conclusao_em": _iso(t.ultima_conclusao_at),
                "ativa": t.ativa,
                # Tarefa nascida de um achado de um parecer de auditor: o id do
                # parecer (o mesmo que o auditor guardou ao emitir) permite ao
                # auditor fechar o ciclo achado -> tarefa -> resolvido no dossiê
                # seguinte.
                "origem_parecer_id": str(t.origem_parecer_id) if t.origem_parecer_id else None,
                "conclusoes": [
                    {
                        "id": str(c.id),
                        "concluida_em": _iso(c.concluida_em),
                        "notas": c.notas,
                        "resultado": _enum(c.resultado),
                        "licoes_aprendidas": c.licoes_aprendidas,
                        "autor": nomes_tarefas.get(c.autor_id),
                    }
                    for c in conclusoes_por_tarefa.get(t.id, [])
                ],
            }
            for t in tarefas
        ]
    }

    # --- formacao.json: ações + participantes + indicador do painel (RJC, arts. 25.º e 27.º)
    from app.formacao.service import painel as painel_formacao

    acoes = db.exec(
        select(AcaoFormacao).where(
            AcaoFormacao.empresa_id == empresa.id,
            AcaoFormacao.deleted_at.is_(None),  # type: ignore[union-attr]
        ).order_by(AcaoFormacao.data)
    ).all()
    participantes = db.exec(
        select(ParticipanteFormacao).where(ParticipanteFormacao.empresa_id == empresa.id)
    ).all()
    participantes_por_acao: dict[uuid.UUID, list[ParticipanteFormacao]] = {}
    for p in participantes:
        participantes_por_acao.setdefault(p.acao_id, []).append(p)

    nomes_formacao = _nomes_utilizadores(db, {a.responsavel_id for a in acoes})
    indicadores_formacao = painel_formacao(db, empresa.id).model_dump(mode="json")
    formacao_json = {
        "indicadores": indicadores_formacao,
        "acoes": [
            {
                "id": str(a.id),
                "titulo": a.titulo,
                "descricao": a.descricao,
                "tipo": _enum(a.tipo),
                "formato": a.formato,
                "estado": _enum(a.estado),
                "data": _iso(a.data),
                "duracao_horas": a.duracao_horas,
                "formador": a.formador,
                "orgao_gestao": a.orgao_gestao,
                "eficacia_metodo": a.eficacia_metodo,
                "eficacia_resultado": a.eficacia_resultado,
                "responsavel": nomes_formacao.get(a.responsavel_id),
                "realizada_em": _iso(a.realizada_at),
                "participantes": [
                    {
                        "nome": p.nome,
                        "interno": p.utilizador_id is not None,
                        "orgao_gestao": p.orgao_gestao,
                        "presente": p.presente,
                    }
                    for p in participantes_por_acao.get(a.id, [])
                ],
            }
            for a in acoes
        ],
    }

    # --- governacao.json: matriz de funções/responsabilidades + utilizadores
    from app.shared.capacidades import documento_matriz_capacidades

    utilizadores = db.exec(
        select(Utilizador).where(
            Utilizador.empresa_id == empresa.id,
            Utilizador.deleted_at.is_(None),  # type: ignore[attr-defined]
        )
    ).all()
    governacao_json = {
        "matriz_capacidades": documento_matriz_capacidades(
            empresa.locale_preferido, empresa.id
        ),
        "utilizadores": [
            {
                "id": str(u.id),
                "nome": decifrar_pii(u.nome),
                "email": u.email,
                "role": _enum(u.role),
                "ativo": u.ativo,
                "criado_em": _iso(u.created_at),
            }
            for u in utilizadores
        ],
    }

    # --- atividade.json: extrato do registo de atividade (append-only).
    # Minimização deliberada: sem IP/user-agent nem payloads — o auditor precisa
    # de "quem fez o quê e quando", não de dados pessoais de rede.
    filtros = [AuditLog.empresa_id == empresa.id]
    if periodo_atividade_meses is not None:
        corte = datetime.now(timezone.utc) - timedelta(days=31 * periodo_atividade_meses)
        filtros.append(AuditLog.created_at >= corte)
    registos = db.exec(
        select(AuditLog).where(*filtros)
        .order_by(AuditLog.created_at.desc())  # type: ignore[union-attr]
        .limit(_ATIVIDADE_MAX_ENTRADAS + 1)
    ).all()
    truncado = len(registos) > _ATIVIDADE_MAX_ENTRADAS
    registos = registos[:_ATIVIDADE_MAX_ENTRADAS]
    nomes_atividade = _nomes_utilizadores(db, {r.utilizador_id for r in registos})
    # O *head* da cadeia de auditoria viaja no dossiê, que já vai assinado e
    # com atestação de tempo. É isto que dá força à cadeia: dentro da base, quem a
    # controla pode reescrevê-la inteira; a partir do momento em que o *head* sai
    # da máquina num artefacto assinado, reescrever a história obriga a falsificar
    # todos os *head*s já exportados. Vai a par do extrato, não dentro dele — o
    # extrato é truncado e por período, o *head* é da cadeia toda.
    atividade_json = {
        "periodo_meses": periodo_atividade_meses,
        "truncado": truncado,
        "cadeia": audit_cadeia.head_de(db, empresa.id),
        "registos": [
            _registo_atividade(r, nomes_atividade.get(r.utilizador_id))
            for r in registos
        ],
    }

    # --- montagem -----------------------------------------------------------

    empresa_json = {
        "id": str(empresa.id),
        "nome": decifrar_pii(empresa.nome),
        "nif": decifrar_pii(empresa.nif),
        "email": decifrar_pii(empresa.email),
        "website": decifrar_pii(empresa.website),
        "setor": empresa.setor,
        "dimensao": _enum(empresa.dimensao),
        "tipo_entidade": _enum(empresa.tipo_entidade),
        "nivel_qnrcs": _enum(empresa.nivel_qnrcs),
        "framework": {
            "registry_id": framework.registry_id,
            "version": framework.version,
            "locale": empresa.locale_preferido,
        },
        "registada_em": _iso(empresa.created_at),
    }

    controlos_json = {
        "controlos": [
            {
                "controlo_empresa_id": str(ce.id),
                "controlo_id": str(ce.control_id),
                "codigo": codigo_por_ce[ce.id],
                "estado": _enum(ce.estado),
                "nivel_maturidade_atual": ce.nivel_maturidade_atual,
                "data_aprovacao": _iso(ce.data_aprovacao),
                "atualizado_em": _iso(ce.updated_at),
                # Exclusão de âmbito: o auditor vê a justificação e pode
                # contestá-la (a decisão viaja com o dossiê).
                "na_justificacao": (
                    decifrar_pii(ce.na_justificacao) if ce.na_justificacao else None
                ),
                "na_definido_em": _iso(ce.na_definido_em),
                "relatorios_auditoria": relatorios_por_ce.get(ce.id, []),
                "evidencias": evidencias_por_ce.get(ce.id, []),
            }
            for ce in ces
        ]
    }

    evidencias_json = {
        "evidencias": [
            {
                "id": str(ev.id),
                # Uma evidência pode sustentar vários controlos. O campo
                # singular mantém-se para os leitores que já existem — incluindo
                # a plataforma do auditor já distribuída, que o lê — e passa a
                # trazer o primeiro dos códigos; o plural ao lado traz todos.
                # Acrescentar sem remover é o que a tolerância de versões do
                # formato permite: um leitor antigo ignora o campo novo.
                "controlo_codigo": next(
                    (
                        codigo_por_ce.get(ce_id)
                        for ce_id in controlos_por_evidencia.get(ev.id, [])
                        if codigo_por_ce.get(ce_id)
                    ),
                    None,
                ),
                "controlos": [
                    {
                        "codigo": codigo_por_ce.get(ce_id),
                        **(ambito_por_ligacao.get((ev.id, ce_id)) or {}),
                    }
                    for ce_id in controlos_por_evidencia.get(ev.id, [])
                    if codigo_por_ce.get(ce_id)
                ],
                "titulo": ev.titulo,
                "tipo": _enum(ev.tipo),
                "conteudo_texto": _texto_evidencia(ev),
                "ficheiro_nome": decifrar_pii(ev.ficheiro_nome),
                "mime": ev.ficheiro_tipo,
                "bytes": ev.ficheiro_tamanho,
                # SHA-256 do conteúdo EM CLARO — o leitor confere-o contra o
                # ficheiro decifrado da entrada correspondente.
                "sha256": ev.conteudo_hash,
                "autor": autores.get(ev.uploaded_by_id),
                "criado_em": _iso(ev.created_at),
                "entrada": (
                    f"ev/{ev.id}.age" if str(ev.id) in ids_com_ficheiro else None
                ),
            }
            for ev in evidencias
        ]
    }

    conteudo = {
        "dados/empresa.json": _json_bytes(empresa_json),
        "dados/conformidade.json": _json_bytes(conformidade),
        "dados/controlos.json": _json_bytes(controlos_json),
        "dados/evidencias.json": _json_bytes(evidencias_json),
        "dados/incidentes.json": _json_bytes(incidentes_json),
        "dados/tarefas.json": _json_bytes(tarefas_json),
        "dados/formacao.json": _json_bytes(formacao_json),
        "dados/governacao.json": _json_bytes(governacao_json),
        "dados/atividade.json": _json_bytes(atividade_json),
    }
    conteudo.update(_montar_premium(empresa))

    contagens = {
        "controlos": len(ces),
        "evidencias": len(evidencias),
        "evidencias_ficheiros": len(ficheiros_evidencia),
        "relatorios_auditoria": len(relatorios),
        "incidentes": len(incidentes),
        "tarefas": len(tarefas),
        "formacoes": len(acoes),
        "atividade": len(registos),
        "premium": sum(1 for k in conteudo if k.startswith("dados/premium/")),
    }
    return conteudo, contagens, ficheiros_evidencia


# ---------------------------------------------------------------------------
# Secções premium (best-effort, como no backup: a indisponibilidade do módulo
# premium nunca impede o dossiê do core — a ausência fica visível no manifest)
# ---------------------------------------------------------------------------

def _montar_premium(empresa: Empresa) -> dict[str, bytes]:
    from app.config import get_settings

    settings = get_settings()
    if not settings.PREMIUM_ENABLED or not settings.PREMIUM_SIDECAR_ADDR:
        return {}

    from app.premium.client import get_premium_client
    from app.premium.fornecedor_client import get_fornecedor_client
    from app.premium.inventario_client import get_inventario_client
    from app.premium.risco_client import get_risco_client

    tenant = str(empresa.id)
    locale = empresa.locale_preferido
    out: dict[str, bytes] = {}
    try:
        premium = get_premium_client()
        if premium.has_feature(tenant, "asset_inventory") and (inv := get_inventario_client()):
            out["dados/premium/inventario.json"] = _json_bytes(
                inv.listar_ativos(tenant, "", locale, 100_000, 0)
            )
        if premium.has_feature(tenant, "risk_analysis") and (ris := get_risco_client()):
            out["dados/premium/riscos.json"] = _json_bytes(
                ris.listar(tenant, "", "", 100_000, 0)
            )
        if premium.has_feature(tenant, "supply_chain") and (forn := get_fornecedor_client()):
            out["dados/premium/fornecedores.json"] = _json_bytes(
                forn.listar(tenant, "", locale, 0, 0)
            )
    except Exception:  # noqa: BLE001 — premium indisponível não trava o core
        logger.warning(
            "Componente premium indisponível — o dossiê segue sem as secções premium.",
            exc_info=True,
        )
    return out
