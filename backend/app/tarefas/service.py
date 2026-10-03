"""
Lógica de negócio do módulo de Tarefas recorrentes (core).

Responsabilidades:
  - Catálogo de obrigações periódicas pré-definidas do QNRCS (adicionar num clique).
  - CRUD da tarefa + registo de conclusões (append-only) — a série é a evidência.
  - Cálculo do próximo prazo a partir da periodicidade e da última conclusão.
  - Notificações in-app quando um prazo se aproxima ou é ultrapassado.
  - Auditoria de todas as escritas (`Acao.TAREFA_*`).
  - Payload localizado do registo de execução (o PDF é gerado no cliente).

Tudo filtrado por `empresa_id` (multi-tenant, fail-closed).
"""
from __future__ import annotations

import calendar
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import func
from sqlmodel import Session, select

from app.auth.models import RoleUtilizador, Utilizador
from app.notificacoes.catalogo import Codigo
from app.notificacoes.service import (
    criar_notificacao,
    marcar_lidas_por_chave_prefixo,
)
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.capacidades import (
    ClasseAcao,
    dono_na_criacao,
    exigir_ambito,
    exigir_delegacao_se_muda_dono,
)
from app.shared.pii import decifrar_pii
from app.shared.validacao import e_data_futura
from app.tarefas.models import (
    Periodicidade,
    Tarefa,
    TarefaConclusao,
    TipoTarefa,
)
from app.tarefas.schemas import (
    ConclusaoSchema,
    ItemCatalogoSchema,
    ListaTarefasSchema,
    PainelTarefasSchema,
    TarefaDetalheSchema,
    TarefaSchema,
)

# Janela de aviso antes de um prazo expirar (dias).
_JANELA_AVISO_DIAS = 14
# Meses por periodicidade (a personalizada usa dias; a pontual não recorre).
_MESES = {
    Periodicidade.MENSAL: 1,
    Periodicidade.TRIMESTRAL: 3,
    Periodicidade.SEMESTRAL: 6,
    Periodicidade.ANUAL: 12,
}


# ── Catálogo de obrigações pré-definidas ─────────────────────────────────────
# Cada entrada dá evidência a um critério do QNRCS que pede execução recorrente.
# Os textos são um ponto de partida em linguagem de PME — o utilizador ajusta.
_CATALOGO = [
    {
        "chave": "revisao_acessos", "categoria": "revisao_acessos",
        "tipo": TipoTarefa.RECORRENTE, "controlos": ["PR.GA-5"],
        "periodicidade": Periodicidade.SEMESTRAL,
        "titulo": {"pt": "Rever acessos e permissões", "en": "Review access and permissions"},
        "descricao": {
            "pt": "Confirmar quem tem acesso a quê, aplicar o menor privilégio e remover acessos que já não são precisos.",
            "en": "Confirm who has access to what, apply least privilege and remove access that is no longer needed.",
        },
    },
    {
        "chave": "teste_backups", "categoria": "teste_backups",
        "tipo": TipoTarefa.TESTE_EXERCICIO, "controlos": ["PR.SD-5"],
        "periodicidade": Periodicidade.SEMESTRAL,
        "titulo": {"pt": "Testar o restauro de cópias de segurança", "en": "Test backup restoration"},
        "descricao": {
            "pt": "Restaurar uma cópia de segurança para confirmar que os dados são recuperáveis. Registar o resultado.",
            "en": "Restore a backup to confirm the data is recoverable. Record the result.",
        },
    },
    {
        "chave": "revisao_logs", "categoria": "revisao_logs",
        "tipo": TipoTarefa.RECORRENTE, "controlos": ["PR.SP-2", "DE.MC-1"],
        "periodicidade": Periodicidade.MENSAL,
        "titulo": {"pt": "Rever os registos de atividade (logs)", "en": "Review activity logs"},
        "descricao": {
            "pt": "Analisar os registos dos sistemas à procura de atividade invulgar ou sinais de incidentes.",
            "en": "Review system logs for unusual activity or signs of incidents.",
        },
    },
    {
        "chave": "formacao_anual", "categoria": "formacao",
        "tipo": TipoTarefa.RECORRENTE, "controlos": ["PR.FC-1"],
        "periodicidade": Periodicidade.ANUAL,
        "titulo": {"pt": "Formação e sensibilização em cibersegurança", "en": "Cybersecurity awareness and training"},
        "descricao": {
            "pt": "Realizar a ação anual de sensibilização/formação de todo o pessoal em cibersegurança.",
            "en": "Run the annual cybersecurity awareness/training session for all staff.",
        },
    },
    {
        "chave": "avaliacao_fornecedores", "categoria": "avaliacao_fornecedores",
        "tipo": TipoTarefa.RECORRENTE, "controlos": ["GR.CA-7"],
        "periodicidade": Periodicidade.ANUAL,
        "titulo": {"pt": "Avaliar os fornecedores críticos", "en": "Assess critical suppliers"},
        "descricao": {
            "pt": "Rever os riscos de cibersegurança dos fornecedores dos quais a atividade depende.",
            "en": "Review the cybersecurity risks of the suppliers the business depends on.",
        },
    },
    {
        "chave": "revisao_politicas", "categoria": "revisao_politicas",
        "tipo": TipoTarefa.RECORRENTE, "controlos": ["GR.PP-1"],
        "periodicidade": Periodicidade.ANUAL,
        "titulo": {"pt": "Rever as políticas e planos", "en": "Review policies and plans"},
        "descricao": {
            "pt": "Confirmar que as políticas e planos continuam atuais e refletem a realidade da organização.",
            "en": "Confirm that policies and plans are still current and reflect the organisation's reality.",
        },
    },
    {
        "chave": "revisao_risco", "categoria": "revisao_politicas",
        "tipo": TipoTarefa.RECORRENTE, "controlos": ["GR.GR-1"],
        "periodicidade": Periodicidade.ANUAL,
        "titulo": {"pt": "Rever a análise de risco", "en": "Review the risk analysis"},
        "descricao": {
            "pt": "Reavaliar os riscos de cibersegurança e confirmar que o apetite ao risco continua adequado.",
            "en": "Reassess cybersecurity risks and confirm the risk appetite is still adequate.",
        },
    },
    {
        "chave": "teste_continuidade", "categoria": "teste_exercicio",
        "tipo": TipoTarefa.TESTE_EXERCICIO, "controlos": ["GR.CO-5", "ID.MC-2"],
        "periodicidade": Periodicidade.ANUAL,
        "titulo": {"pt": "Testar o Plano de Continuidade de Negócio", "en": "Test the Business Continuity Plan"},
        "descricao": {
            "pt": "Simular uma interrupção e confirmar que os serviços críticos se mantêm ou recuperam a tempo. Registar o resultado.",
            "en": "Simulate a disruption and confirm critical services keep running or recover in time. Record the result.",
        },
    },
    {
        "chave": "teste_recuperacao", "categoria": "teste_exercicio",
        "tipo": TipoTarefa.TESTE_EXERCICIO, "controlos": ["GR.PP-3", "ID.MC-2"],
        "periodicidade": Periodicidade.ANUAL,
        "titulo": {"pt": "Testar o Plano de Recuperação de Desastres", "en": "Test the Disaster Recovery Plan"},
        "descricao": {
            "pt": "Exercitar a recuperação dos sistemas após um cenário de desastre. Registar o resultado.",
            "en": "Exercise the recovery of systems after a disaster scenario. Record the result.",
        },
    },
]


def _locale(loc: str | None) -> str:
    return "en" if (loc or "").startswith("en") else "pt"


def catalogo(locale: str | None) -> list[ItemCatalogoSchema]:
    """Obrigações pré-definidas para o utilizador adicionar num clique."""
    lang = _locale(locale)
    return [
        ItemCatalogoSchema(
            chave=e["chave"],
            titulo=e["titulo"][lang],
            descricao=e["descricao"][lang],
            categoria=e["categoria"],
            tipo=e["tipo"],
            controlos=list(e["controlos"]),
            periodicidade=e["periodicidade"],
        )
        for e in _CATALOGO
    ]


# ── Periodicidade e controlos ────────────────────────────────────────────────

def _add_meses(d: date, meses: int) -> date:
    """Soma `meses` a uma data, ajustando o dia ao fim do mês quando preciso."""
    total = d.month - 1 + meses
    ano = d.year + total // 12
    mes = total % 12 + 1
    dia = min(d.day, calendar.monthrange(ano, mes)[1])
    return date(ano, mes, dia)


def _proximo_prazo(base: date, periodicidade: Periodicidade, dias: int | None) -> date:
    """Próximo prazo a partir de `base`. Pontual não recorre (fica no próprio dia)."""
    if periodicidade == Periodicidade.PERSONALIZADA:
        return base + timedelta(days=dias or 30)
    if periodicidade == Periodicidade.PONTUAL:
        return base
    return _add_meses(base, _MESES.get(periodicidade, 12))


def _controlos_lista(texto: str) -> list[str]:
    return [c for c in (texto or "").split(",") if c]


def _controlos_texto(lista: list[str] | None) -> str:
    return ",".join(c.strip() for c in (lista or []) if c and c.strip())[:500]


# ── Leitura ──────────────────────────────────────────────────────────────────

def _get_tarefa(db: Session, tarefa_id: uuid.UUID, empresa_id: uuid.UUID) -> Tarefa:
    t = db.get(Tarefa, tarefa_id)
    if not t or t.empresa_id != empresa_id or t.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada.")
    return t


def _nomes(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {
        u.id: (decifrar_pii(u.nome) or "")
        for u in db.exec(select(Utilizador).where(Utilizador.id.in_(ids))).all()
    }


def _schema(t: Tarefa, hoje: date, nome: str | None, total_conclusoes: int = 0) -> TarefaSchema:
    # Os controlos são convertidos de texto para lista pelo validador do schema —
    # tem de ser antes da validação, não aqui depois dela.
    s = TarefaSchema.model_validate(t)
    s.responsavel_nome = nome
    s.total_conclusoes = total_conclusoes
    if t.ativa:
        dias = (t.proximo_prazo - hoje).days
        s.dias_restantes = dias
        s.em_atraso = dias < 0
    return s


def listar_tarefas(
    db: Session, empresa_id: uuid.UUID, incluir_inativas: bool = False
) -> ListaTarefasSchema:
    hoje = datetime.now(timezone.utc).date()
    filtros = [Tarefa.empresa_id == empresa_id, Tarefa.deleted_at.is_(None)]
    if not incluir_inativas:
        filtros.append(Tarefa.ativa.is_(True))
    tarefas = db.exec(
        select(Tarefa).where(*filtros).order_by(Tarefa.proximo_prazo.asc())
    ).all()
    nomes = _nomes(db, {t.responsavel_id for t in tarefas})
    contagens = _contar_conclusoes(db, [t.id for t in tarefas])
    return ListaTarefasSchema(
        total=len(tarefas),
        tarefas=[
            _schema(t, hoje, nomes.get(t.responsavel_id), contagens.get(t.id, 0))
            for t in tarefas
        ],
    )


def _contar_conclusoes(db: Session, tarefa_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not tarefa_ids:
        return {}
    linhas = db.exec(
        select(TarefaConclusao.tarefa_id, func.count())
        .where(TarefaConclusao.tarefa_id.in_(tarefa_ids))
        .group_by(TarefaConclusao.tarefa_id)
    ).all()
    return {tid: n for tid, n in linhas}


def obter_tarefa(
    db: Session, tarefa_id: uuid.UUID, empresa_id: uuid.UUID
) -> TarefaDetalheSchema:
    hoje = datetime.now(timezone.utc).date()
    t = _get_tarefa(db, tarefa_id, empresa_id)
    conclusoes = db.exec(
        select(TarefaConclusao)
        .where(TarefaConclusao.tarefa_id == tarefa_id)
        .order_by(TarefaConclusao.concluida_em.desc(), TarefaConclusao.created_at.desc())
    ).all()
    nomes = _nomes(db, {t.responsavel_id} | {c.autor_id for c in conclusoes})
    base = _schema(t, hoje, nomes.get(t.responsavel_id), len(conclusoes))
    detalhe = TarefaDetalheSchema(**base.model_dump())
    detalhe.conclusoes = [
        ConclusaoSchema(
            id=c.id, concluida_em=c.concluida_em, notas=c.notas,
            resultado=c.resultado, licoes_aprendidas=c.licoes_aprendidas,
            autor_id=c.autor_id, autor_nome=nomes.get(c.autor_id), created_at=c.created_at,
        )
        for c in conclusoes
    ]
    return detalhe


def painel(db: Session, empresa_id: uuid.UUID) -> PainelTarefasSchema:
    hoje = datetime.now(timezone.utc).date()
    tarefas = db.exec(
        select(Tarefa).where(
            Tarefa.empresa_id == empresa_id,
            Tarefa.deleted_at.is_(None),
            Tarefa.ativa.is_(True),
        )
    ).all()
    a_vencer = em_atraso = em_dia = 0
    for t in tarefas:
        dias = (t.proximo_prazo - hoje).days
        if dias < 0:
            em_atraso += 1
        elif dias <= _JANELA_AVISO_DIAS:
            a_vencer += 1
        else:
            em_dia += 1
    return PainelTarefasSchema(
        total=len(tarefas), em_dia=em_dia, a_vencer=a_vencer, em_atraso=em_atraso
    )


# ── Escrita ──────────────────────────────────────────────────────────────────

def _resolver_responsavel(
    db: Session, empresa_id: uuid.UUID, responsavel_id: str
) -> uuid.UUID | None:
    """Valida que o responsável é um utilizador ativo do tenant (ou None)."""
    if not responsavel_id:
        return None
    try:
        rid = uuid.UUID(responsavel_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Responsável inválido.")
    u = db.get(Utilizador, rid)
    if not u or u.empresa_id != empresa_id or not u.ativo or u.deleted_at is not None:
        raise HTTPException(status_code=400, detail="Responsável inválido.")
    return rid


def _entrada_catalogo(chave: str) -> dict | None:
    return next((e for e in _CATALOGO if e["chave"] == chave), None)


def criar_tarefa(
    db: Session, empresa_id: uuid.UUID, dados, utilizador: Utilizador,
    request: Request | None = None,
) -> TarefaSchema:
    hoje = datetime.now(timezone.utc).date()

    # A partir do catálogo: preenche os campos não fornecidos pelo utilizador.
    titulo = dados.titulo.strip()
    descricao = dados.descricao
    categoria = dados.categoria
    tipo = dados.tipo
    controlos = list(dados.controlos)
    periodicidade = dados.periodicidade
    chave_catalogo = None

    if dados.chave_catalogo:
        entrada = _entrada_catalogo(dados.chave_catalogo)
        if entrada is None:
            raise HTTPException(status_code=400, detail="Modelo de tarefa inválido.")
        # Os textos guardados são editáveis (o painel traduz rótulos, não o
        # conteúdo): nascem na língua que a empresa escolheu para a aplicação.
        from app.empresas.models import Empresa

        empresa = db.get(Empresa, empresa_id)
        lang = (getattr(empresa, "locale_preferido", None) or "pt").split("-")[0].lower()
        if lang not in entrada["titulo"]:
            lang = "pt"
        chave_catalogo = entrada["chave"]
        titulo = titulo or entrada["titulo"][lang]
        descricao = descricao or entrada["descricao"][lang]
        categoria = entrada["categoria"]
        tipo = entrada["tipo"]
        controlos = controlos or list(entrada["controlos"])
        # A escolha explícita do utilizador prevalece; sem escolha, vale o catálogo.
        periodicidade = periodicidade or entrada["periodicidade"]
    periodicidade = periodicidade or Periodicidade.ANUAL

    if not titulo:
        raise HTTPException(status_code=400, detail="O título é obrigatório.")
    if periodicidade == Periodicidade.PERSONALIZADA and not (dados.periodicidade_dias and dados.periodicidade_dias > 0):
        raise HTTPException(status_code=400, detail="Indique o intervalo em dias.")

    responsavel_id = dono_na_criacao(
        utilizador,
        "tarefas",
        _resolver_responsavel(db, empresa_id, dados.responsavel_id),
    )
    prazo = dados.primeiro_prazo or _proximo_prazo(hoje, periodicidade, dados.periodicidade_dias)

    t = Tarefa(
        empresa_id=empresa_id,
        chave_catalogo=chave_catalogo,
        titulo=titulo,
        descricao=descricao,
        categoria=categoria,
        tipo=tipo,
        controlos=_controlos_texto(controlos),
        periodicidade=periodicidade,
        periodicidade_dias=dados.periodicidade_dias,
        responsavel_id=responsavel_id,
        proximo_prazo=prazo,
    )
    db.add(t)
    db.flush()
    registar_acao(
        db, acao=Acao.TAREFA_CRIADA, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="Tarefa", entidade_id=t.id,
        dados_novos={"titulo": t.titulo, "categoria": t.categoria,
                     "periodicidade": t.periodicidade.value, "tipo": t.tipo.value},
        request=request,
    )
    db.flush()
    db.refresh(t)
    nome = decifrar_pii(db.get(Utilizador, responsavel_id).nome) if responsavel_id else None
    return _schema(t, hoje, nome, 0)


def atualizar_tarefa(
    db: Session, tarefa_id: uuid.UUID, empresa_id: uuid.UUID, dados,
    utilizador: Utilizador, request: Request | None = None,
) -> TarefaSchema:
    hoje = datetime.now(timezone.utc).date()
    t = _get_tarefa(db, tarefa_id, empresa_id)
    exigir_ambito(utilizador, "tarefas", ClasseAcao.OPERAR, t.responsavel_id)
    campos = dados.model_dump(exclude_unset=True)
    if "responsavel_id" in campos:
        novo_dono = _resolver_responsavel(db, empresa_id, campos.pop("responsavel_id") or "")
        exigir_delegacao_se_muda_dono(utilizador, "tarefas", t.responsavel_id, novo_dono)
        t.responsavel_id = novo_dono
    if "controlos" in campos:
        t.controlos = _controlos_texto(campos.pop("controlos"))
    for campo, valor in campos.items():
        setattr(t, campo, valor)
    if t.periodicidade == Periodicidade.PERSONALIZADA and not (t.periodicidade_dias and t.periodicidade_dias > 0):
        raise HTTPException(status_code=400, detail="Indique o intervalo em dias.")
    t.updated_at = datetime.now(timezone.utc)
    db.add(t)
    registar_acao(
        db, acao=Acao.TAREFA_ATUALIZADA, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="Tarefa", entidade_id=t.id,
        dados_novos={k: (v.value if hasattr(v, "value") else v) for k, v in campos.items()},
        request=request,
    )
    db.flush()
    db.refresh(t)
    total = _contar_conclusoes(db, [t.id]).get(t.id, 0)
    nome = decifrar_pii(db.get(Utilizador, t.responsavel_id).nome) if t.responsavel_id else None
    return _schema(t, hoje, nome, total)


def registar_conclusao(
    db: Session, tarefa_id: uuid.UUID, empresa_id: uuid.UUID, dados,
    utilizador: Utilizador, request: Request | None = None,
) -> TarefaSchema:
    hoje = datetime.now(timezone.utc).date()
    t = _get_tarefa(db, tarefa_id, empresa_id)
    exigir_ambito(utilizador, "tarefas", ClasseAcao.OPERAR, t.responsavel_id)
    concluida = dados.concluida_em or hoje
    if e_data_futura(concluida):
        # Uma execução que ainda não aconteceu empurrava o próximo prazo para a
        # frente e tirava a tarefa do radar.
        raise HTTPException(status_code=400, detail="Uma conclusão não pode ter data futura.")

    db.add(TarefaConclusao(
        tarefa_id=t.id, empresa_id=empresa_id, concluida_em=concluida,
        notas=dados.notas or "", resultado=dados.resultado,
        licoes_aprendidas=dados.licoes_aprendidas, autor_id=utilizador.id,
    ))
    # Uma conclusão antiga registada tarde não faz recuar a última execução nem
    # o próximo prazo: contam a partir da execução mais recente.
    mais_recente = max(concluida, t.ultima_conclusao_at) if t.ultima_conclusao_at else concluida
    t.ultima_conclusao_at = mais_recente
    if t.periodicidade == Periodicidade.PONTUAL:
        t.ativa = False   # tarefa de uma só vez: cumprida, sai do radar
    else:
        t.proximo_prazo = _proximo_prazo(mais_recente, t.periodicidade, t.periodicidade_dias)
    t.updated_at = datetime.now(timezone.utc)
    db.add(t)
    # Limpa avisos de prazo pendentes: o prazo mudou, os antigos deixam de fazer sentido.
    _marcar_avisos_lidos(db, t)
    registar_acao(
        db, acao=Acao.TAREFA_CONCLUIDA, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="Tarefa", entidade_id=t.id,
        dados_novos={"concluida_em": concluida.isoformat(),
                     "resultado": dados.resultado.value if dados.resultado else None},
        request=request,
    )
    db.flush()
    db.refresh(t)
    total = _contar_conclusoes(db, [t.id]).get(t.id, 0)
    nome = decifrar_pii(db.get(Utilizador, t.responsavel_id).nome) if t.responsavel_id else None
    return _schema(t, hoje, nome, total)


def eliminar_tarefa(
    db: Session, tarefa_id: uuid.UUID, empresa_id: uuid.UUID,
    utilizador: Utilizador, request: Request | None = None,
) -> None:
    t = _get_tarefa(db, tarefa_id, empresa_id)
    exigir_ambito(utilizador, "tarefas", ClasseAcao.ELIMINAR, t.responsavel_id)
    t.deleted_at = datetime.now(timezone.utc)
    t.ativa = False
    db.add(t)
    registar_acao(
        db, acao=Acao.TAREFA_ELIMINADA, resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id, utilizador_id=utilizador.id,
        entidade_tipo="Tarefa", entidade_id=t.id,
        dados_novos={"titulo": t.titulo}, request=request,
    )


# ── Notificações de prazos ───────────────────────────────────────────────────

def _destinatarios(db: Session, empresa_id: uuid.UUID, responsavel_id) -> list[uuid.UUID]:
    """Responsável (se houver) + administradores da empresa."""
    ids = {
        u.id
        for u in db.exec(
            select(Utilizador).where(
                Utilizador.empresa_id == empresa_id,
                Utilizador.ativo.is_(True),
                Utilizador.role.in_([RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN]),
            )
        ).all()
    }
    if responsavel_id:
        ids.add(responsavel_id)
    return list(ids)


def _marcar_avisos_lidos(db: Session, tarefa: Tarefa) -> None:
    """Dispensa os avisos de prazo de uma tarefa cujo prazo deixou de valer."""
    for codigo in (Codigo.TAREFA_PRAZO_PROXIMO, Codigo.TAREFA_PRAZO_ATRASO):
        marcar_lidas_por_chave_prefixo(
            db, prefixo=f"{codigo}:{tarefa.id}:", empresa_id=tarefa.empresa_id,
        )


def _notificar_prazo(db: Session, t: Tarefa, hoje: date) -> int:
    """Cria avisos in-app se o prazo está a vencer/em atraso.

    A chave inclui o prazo concreto, por isso um ciclo novo de uma tarefa
    recorrente volta a avisar; e o código distingue "a aproximar-se" de "em
    atraso", por isso passar o prazo gera um aviso próprio que apaga o anterior."""
    dias = (t.proximo_prazo - hoje).days
    if dias > _JANELA_AVISO_DIAS:
        return 0
    codigo = (
        Codigo.TAREFA_PRAZO_ATRASO if dias < 0 else Codigo.TAREFA_PRAZO_PROXIMO
    )
    criadas = 0
    for uid in _destinatarios(db, t.empresa_id, t.responsavel_id):
        criada = criar_notificacao(
            db,
            empresa_id=t.empresa_id,
            utilizador_id=uid,
            codigo=codigo,
            params={"titulo": t.titulo, "dias": dias},
            entidade_id=t.id,
            dedup_partes=(t.id, t.proximo_prazo.isoformat()),
        )
        if criada is not None:
            criadas += 1
    return criadas


def verificar_prazos_tarefas(db: Session) -> int:
    """Varre as tarefas ativas e notifica prazos a vencer/em atraso.
    Chamado por um tick diário no arranque (ver main.py). Idempotente por prazo."""
    hoje = datetime.now(timezone.utc).date()
    tarefas = db.exec(
        select(Tarefa).where(Tarefa.deleted_at.is_(None), Tarefa.ativa.is_(True))
    ).all()
    total = sum(_notificar_prazo(db, t, hoje) for t in tarefas)
    if total:
        db.commit()
    return total


# ── Registo de execução (payload localizado; PDF gerado no cliente) ──────────

_REL_TEXTOS = {
    "pt": {
        "titulo": "Registo de Execução — Tarefa de Conformidade",
        "subtitulo": "Histórico de execuções, como evidência de gestão contínua da cibersegurança.",
        "sec_ident": "Identificação",
        "sec_hist": "Histórico de execuções",
        "l_titulo": "Tarefa", "l_categoria": "Tipo de tarefa", "l_periodicidade": "Periodicidade",
        "l_responsavel": "Responsável", "l_controlos": "Controlos associados",
        "l_proximo": "Próximo prazo", "l_ultima": "Última execução",
        "c_data": "Data", "c_resultado": "Resultado", "c_autor": "Responsável",
        "c_notas": "Notas", "c_licoes": "Lições aprendidas",
        "nao_def": "—", "sem_hist": "Ainda sem execuções registadas.",
        "tipos": {"recorrente": "Recorrente", "teste_exercicio": "Teste/Exercício"},
        "period": {"mensal": "Mensal", "trimestral": "Trimestral", "semestral": "Semestral",
                   "anual": "Anual", "personalizada": "Personalizada", "pontual": "Pontual"},
        "resultados": {"sucesso": "Sucesso", "parcial": "Parcial", "falha": "Falha"},
    },
    "en": {
        "titulo": "Execution Record — Compliance Task",
        "subtitulo": "History of executions, as evidence of ongoing cybersecurity management.",
        "sec_ident": "Identification",
        "sec_hist": "Execution history",
        "l_titulo": "Task", "l_categoria": "Task type", "l_periodicidade": "Frequency",
        "l_responsavel": "Owner", "l_controlos": "Related controls",
        "l_proximo": "Next due", "l_ultima": "Last executed",
        "c_data": "Date", "c_resultado": "Result", "c_autor": "Owner",
        "c_notas": "Notes", "c_licoes": "Lessons learned",
        "nao_def": "—", "sem_hist": "No executions recorded yet.",
        "tipos": {"recorrente": "Recurring", "teste_exercicio": "Test/Exercise"},
        "period": {"mensal": "Monthly", "trimestral": "Quarterly", "semestral": "Half-yearly",
                   "anual": "Yearly", "personalizada": "Custom", "pontual": "One-off"},
        "resultados": {"sucesso": "Success", "parcial": "Partial", "falha": "Failure"},
    },
}


def documento_tarefa(
    db: Session, tarefa_id: uuid.UUID, empresa_id: uuid.UUID, locale: str | None
) -> dict:
    """Payload do registo de execução de uma tarefa (mesma forma dos documentos premium)."""
    t = _get_tarefa(db, tarefa_id, empresa_id)
    lang = _locale(locale)
    tx = _REL_TEXTOS[lang]
    conclusoes = db.exec(
        select(TarefaConclusao)
        .where(TarefaConclusao.tarefa_id == tarefa_id)
        .order_by(TarefaConclusao.concluida_em.desc(), TarefaConclusao.created_at.desc())
    ).all()
    nomes = _nomes(db, {t.responsavel_id} | {c.autor_id for c in conclusoes})
    controlos = _controlos_lista(t.controlos)
    e_teste = t.tipo == TipoTarefa.TESTE_EXERCICIO

    ident = "\n".join([
        f"{tx['l_titulo']}: {t.titulo}",
        f"{tx['l_categoria']}: {tx['tipos'].get(t.tipo.value, t.tipo.value)}",
        f"{tx['l_periodicidade']}: {tx['period'].get(t.periodicidade.value, t.periodicidade.value)}",
        f"{tx['l_responsavel']}: {nomes.get(t.responsavel_id) or tx['nao_def']}",
        f"{tx['l_controlos']}: {', '.join(controlos) or tx['nao_def']}",
        f"{tx['l_proximo']}: {t.proximo_prazo.isoformat() if t.ativa else tx['nao_def']}",
        f"{tx['l_ultima']}: {t.ultima_conclusao_at.isoformat() if t.ultima_conclusao_at else tx['nao_def']}",
    ])

    # Tabela de histórico: acrescenta as colunas de teste só quando fazem sentido.
    cabecalho = [tx["c_data"]]
    if e_teste:
        cabecalho.append(tx["c_resultado"])
    cabecalho += [tx["c_autor"], tx["c_notas"]]
    if e_teste:
        cabecalho.append(tx["c_licoes"])

    linhas = []
    for c in conclusoes:
        linha = [c.concluida_em.isoformat()]
        if e_teste:
            linha.append(tx["resultados"].get(c.resultado.value, tx["nao_def"]) if c.resultado else tx["nao_def"])
        linha += [nomes.get(c.autor_id) or tx["nao_def"], c.notas or tx["nao_def"]]
        if e_teste:
            linha.append(c.licoes_aprendidas or tx["nao_def"])
        linhas.append(linha)

    secoes = [
        {"titulo": tx["sec_ident"], "texto": ident, "cabecalho": [], "linhas": []},
    ]
    if linhas:
        secoes.append({"titulo": tx["sec_hist"], "texto": "", "cabecalho": cabecalho, "linhas": linhas})
    else:
        secoes.append({"titulo": tx["sec_hist"], "texto": tx["sem_hist"], "cabecalho": [], "linhas": []})

    return {
        "titulo": tx["titulo"],
        "subtitulo": tx["subtitulo"],
        "data_geracao": datetime.now(timezone.utc).isoformat(),
        "secoes": secoes,
        "controlos": controlos,
    }
