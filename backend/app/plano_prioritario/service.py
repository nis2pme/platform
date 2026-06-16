"""
Lógica de negócio do Plano de Ações Prioritárias.

Fluxo:
1. O utilizador responde a 10 perguntas de diagnóstico rápido.
2. As respostas são convertidas em scores (A=1…E=5, NS=1 pior cenário).
3. Cada pergunta mapeia a controlos QNRCS específicos.
4. O algoritmo gera o roadmap ordenado com 5 regras de priorização:
   R1 — Todos os controlos Básico antes de Substancial antes de Elevado
   R2 — Dentro de cada tier, os mapeados pelo questionário primeiro,
       ordenados por gap descendente (gap = alvo_do_nivel − nivel_atual)
   R3 — Desempate 1: ordem cronológica do domínio (GR→ID→PR→DE→RS→RC)
   R4 — Desempate 2: ID numérico do controlo
   R5 — Controlos não-mapeados no final do tier, ordenados por R3+R4
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, func
from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.empresas.models import Empresa
from app.auth.models import RoleUtilizador, Utilizador
from app.frameworks.models import (
    Control,
    ControlLocale,
    ControloEmpresaV2,
    Domain,
    DomainLocale,
    Framework,
    SubRequirement,
    Subdomain,
)
from app.frameworks.runtime import (
    load_company_control_rows,
    load_preferred_locales,
    load_thresholds_map,
)
from app.shared.utils import resolver_locale
from app.plano_prioritario.models import PlanoItem, QuestionarioResposta


# ---------------------------------------------------------------------------
# Constantes — Mapeamento Questionário → Controlos QNRCS
# ---------------------------------------------------------------------------

# Respostas válidas e conversão para score numérico
RESPOSTAS_VALIDAS = {"A", "B", "C", "D", "E", "NS"}
SCORE_MAP: dict[str, int] = {
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
    "E": 5,
    "NS": 1,  # Desconhecimento = pior cenário (equiparado a nível A)
}

# Perguntas válidas
PERGUNTAS_VALIDAS = {f"q{i}" for i in range(1, 11)}

# Cada pergunta mapeia a controlos QNRCS específicos.
# A chave é a pergunta (q1..q10), o valor é a lista de códigos de controlo.
QUESTION_CONTROL_MAP: dict[str, list[str]] = {
    # Q1: Cultura e Governação
    "q1": ["GR.CO-1", "GR.GR-2", "GR.GR-4", "GR.PP-1"],
    # Q2: Formação e Literacia
    "q2": ["PR.FC-1"],
    # Q3: Financiamento e recursos
    "q3": ["GR.FR-2"],
    # Q4: Identidades, MFA e autenticação
    "q4": ["PR.GA-1", "PR.GA-3", "PR.GA-5"],
    # Q5: Obsolescência e atualizações
    "q5": ["ID.GA-7", "PR.SP-1"],
    # Q6: Backups e continuidade
    "q6": ["GR.CO-5", "PR.SD-5"],
    # Q7: Controlo de Equipamentos e Shadow IT
    "q7": ["DE.MC-5", "ID.GA-1", "ID.GA-3", "PR.SP-3"],   
    # Q8: Cadeia de abastecimento
    "q8": ["GR.CA-7"],
    # Q9: Resposta a incidentes
    "q9": ["GR.PP-2", "ID.MC-3", "RS.GI-1", "RS.GI-3"],
    # Q10: Comunicações e acesso remoto
    "q10": ["PR.GA-7"]
    }

# Ordem cronológica dos domínios para desempate (R3)
DOMAIN_ORDER: dict[str, int] = {
    "GR": 1,
    "ID": 2,
    "PR": 3,
    "DE": 4,
    "RS": 5,
    "RC": 6,
}

# ---------------------------------------------------------------------------
# Validações
# ---------------------------------------------------------------------------

def _validar_respostas(respostas: dict[str, str]) -> None:
    """Valida que as chaves são q1..q10 e os valores são A-E ou NS."""
    for chave, valor in respostas.items():
        if chave not in PERGUNTAS_VALIDAS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Pergunta inválida: {chave}. "
                       f"Perguntas válidas: q1 a q10.",
            )
        if valor not in RESPOSTAS_VALIDAS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Resposta inválida para {chave}: {valor}. "
                       f"Valores aceites: A, B, C, D, E, NS.",
            )


def _extrair_numero_controlo(code: str) -> int:
    """Extrai o número final de um código de controlo (ex: PR.GA-7 → 7)."""
    match = re.search(r"-(\d+)$", code)
    return int(match.group(1)) if match else 0


def _load_primeiro_nivel_controlo(
    db: Session,
    control_ids: set[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Devolve o primeiro nível real em que cada controlo entra no roadmap."""
    if not control_ids:
        return {}

    control_id_column = getattr(SubRequirement, "control_id")
    maturity_level_column = getattr(SubRequirement, "maturity_level")

    rows = db.exec(
        select(
            control_id_column,
            func.min(maturity_level_column),
        )
        .where(control_id_column.in_(list(control_ids)))
        .group_by(control_id_column)
    ).all()

    return {
        control_id: int(primeiro_nivel)
        for control_id, primeiro_nivel in rows
        if primeiro_nivel is not None
    }


def _get_nivel_roadmap(
    control_id: uuid.UUID,
    primeiro_nivel_map: dict[uuid.UUID, int],
    framework: Framework,
) -> int:
    """Nível macro do controlo no roadmap: primeiro nível real do controlo."""
    primeiro_nivel = primeiro_nivel_map.get(control_id)
    if primeiro_nivel is None:
        return framework.maturity_scale_min

    return max(
        framework.maturity_scale_min,
        min(int(primeiro_nivel), framework.maturity_scale_max),
    )


def _calcular_gap_roadmap(nivel_alvo: int, nivel_atual: int) -> int:
    """Gap do roadmap = alvo do nível macro - nota atual do controlo."""
    return max(0, nivel_alvo - nivel_atual)


def plano_existe(db: Session, empresa_id: uuid.UUID) -> bool:
    """Indica se já existe um plano persistido para a empresa."""
    return db.exec(
        select(PlanoItem.id).where(PlanoItem.empresa_id == empresa_id).limit(1)
    ).first() is not None


# ---------------------------------------------------------------------------
# Guardar / Obter respostas ao questionário
# ---------------------------------------------------------------------------

def guardar_respostas(
    db: Session,
    empresa: Empresa,
    utilizador: Utilizador,
    respostas: dict[str, str],
) -> QuestionarioResposta:
    """Guarda (ou atualiza) as respostas ao questionário de diagnóstico."""
    _validar_respostas(respostas)

    existente = db.exec(
        select(QuestionarioResposta).where(
            QuestionarioResposta.empresa_id == empresa.id,
        )
    ).first()

    if existente:
        existente.respostas = respostas
        existente.respondido_por_id = utilizador.id
        existente.updated_at = datetime.now(timezone.utc)
        db.add(existente)
    else:
        existente = QuestionarioResposta(
            empresa_id=empresa.id,
            respostas=respostas,
            respondido_por_id=utilizador.id,
        )
        db.add(existente)

    db.commit()
    db.refresh(existente)
    return existente


def obter_respostas(
    db: Session,
    empresa_id: uuid.UUID,
) -> QuestionarioResposta | None:
    """Devolve as respostas actuais (ou None se não preenchido)."""
    return db.exec(
        select(QuestionarioResposta).where(
            QuestionarioResposta.empresa_id == empresa_id,
        )
    ).first()


# ---------------------------------------------------------------------------
# Algoritmo de priorização — Gerar plano
# ---------------------------------------------------------------------------

def _build_questionnaire_scores_by_control(
    respostas: dict[str, str],
) -> dict[str, int]:
    """
    Devolve o pior score observado por controlo a partir das respostas.

    Score mais baixo = resposta pior = maior urgência no plano.
    Se um controlo for referenciado por várias perguntas, fica com o pior
    score dessas perguntas para refletir o maior risco percebido.
    """
    scores_por_controlo: dict[str, int] = {}
    for pergunta, valor in respostas.items():
        if pergunta not in PERGUNTAS_VALIDAS:
            continue

        score = SCORE_MAP.get(valor)
        if score is None:
            continue

        for code in QUESTION_CONTROL_MAP.get(pergunta, []):
            atual = scores_por_controlo.get(code)
            if atual is None or score < atual:
                scores_por_controlo[code] = score

    return scores_por_controlo


def gerar_plano(
    db: Session,
    empresa: Empresa,
) -> list[PlanoItem]:
    """
    Gera (ou regenera) o plano de ações prioritárias para a empresa.
    Aplica o algoritmo de 5 regras de priorização:
      R1: Agrupar por nível de conformidade (Básico→Substancial→Elevado)
        R2: Dentro do grupo, mapeados pelo questionário primeiro;
            entre mapeados, respostas piores primeiro e gap DESC
      R3: Desempate por ordem do domínio
      R4: Desempate por ID numérico do controlo
      R5: Não-mapeados no fim do tier, ordenados por R3+R4
    """
    if not empresa.framework_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empresa sem framework V2 associado.",
        )

    framework = db.get(Framework, empresa.framework_id)
    if not framework:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Framework não encontrado.",
        )

    rows = load_company_control_rows(db, empresa.id, framework.id)
    primeiro_nivel_map = _load_primeiro_nivel_controlo(
        db,
        {row.control.id for row in rows},
    )
    # Threshold do perfil da empresa (Básico=1, Substancial=2, Elevado=3)
    # Usado como nivel_conformidade de cada controlo no roadmap.
    # Fallback para primeiro_nivel se o controlo não estiver no perfil.
    thresholds_map = load_thresholds_map(db, framework, empresa)

    # Obter respostas do questionário
    qr = obter_respostas(db, empresa.id)
    questionnaire_scores = _build_questionnaire_scores_by_control(
        qr.respostas if qr else {}
    )

    items_raw: list[dict] = []
    for row in rows:
        # Usar o threshold do perfil como nível alvo; fallback para primeiro_nivel
        threshold = thresholds_map.get(row.control.id)
        nivel_roadmap = (
            threshold
            if threshold is not None
            else _get_nivel_roadmap(row.control.id, primeiro_nivel_map, framework)
        )
        gap = _calcular_gap_roadmap(
            nivel_roadmap,
            row.ce.nivel_maturidade_atual,
        )

        items_raw.append({
            "control_id": row.control.id,
            "code": row.control.code,
            "nivel_conformidade": nivel_roadmap,
            "gap": gap,
            "mapeado": row.control.code in questionnaire_scores,
            "score_questionario": questionnaire_scores.get(row.control.code),
            "dominio_codigo": row.domain.code,
            "dominio_ordem": DOMAIN_ORDER.get(row.domain.code, 99),
            "controlo_num": _extrair_numero_controlo(row.control.code),
            "nivel_atual": row.ce.nivel_maturidade_atual,
        })

    # -----------------------------------------------------------------------
    # Algoritmo de ordenação
    # -----------------------------------------------------------------------
    # Cada item recebe uma sort key tuplo:
    # (nivel_conformidade, not mapeado, mapped_score, mapped_gap,
    #  dominio_ordem, controlo_num)
    #
    # R1: nivel_conformidade ASC → Básico(1) antes de Substancial(2) antes de Elevado(3)
    # R2+R5: not mapeado → False(0) antes de True(1) → mapeados primeiro
    #         score_questionario ASC só nos mapeados: A/NS(1) antes de B(2), etc.
    #         gap DESC apenas dentro dos mapeados; não-mapeados empatam aqui
    # R3: dominio_ordem ASC
    # R4: controlo_num ASC

    items_raw.sort(key=lambda x: (
        x["nivel_conformidade"],   # R1
        not x["mapeado"],          # R2/R5: 0=mapeado (primeiro), 1=não-mapeado (depois)
        x["score_questionario"] if x["mapeado"] else 99,  # R2: pior resposta primeiro
        -x["gap"] if x["mapeado"] else 0,  # R2: gap descendente só nos mapeados
        x["dominio_ordem"],        # R3: ordem domínio
        x["controlo_num"],         # R4: ID numérico controlo
    ))

    # Apagar plano anterior desta empresa
    itens_antigos = db.exec(
        select(PlanoItem).where(PlanoItem.empresa_id == empresa.id)
    ).all()
    for item in itens_antigos:
        db.delete(item)

    # Criar novos PlanoItems
    novos: list[PlanoItem] = []
    for pos, raw in enumerate(items_raw, start=1):
        pi = PlanoItem(
            empresa_id=empresa.id,
            control_id=raw["control_id"],
            posicao=pos,
            nivel_conformidade=raw["nivel_conformidade"],
            gap=raw["gap"],
            mapeado_questionario=raw["mapeado"],
            dominio_codigo=raw["dominio_codigo"],
            dominio_ordem=raw["dominio_ordem"],
        )
        novos.append(pi)
        db.add(pi)

    db.commit()
    for pi in novos:
        db.refresh(pi)

    return novos


# ---------------------------------------------------------------------------
# Obter plano com dados enriquecidos
# ---------------------------------------------------------------------------

def obter_plano(
    db: Session,
    empresa: Empresa,
    locale: str | None = None,
    limite: int = 3,
    utilizador: Utilizador | None = None,
) -> dict:
    """
    Devolve apenas os primeiros controlos não conformes necessários ao dashboard.
    Retorna dict compatível com PlanoOut.
    """
    if not empresa.framework_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empresa sem framework V2 associado.",
        )

    framework = db.get(Framework, empresa.framework_id)
    if not framework:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Framework não encontrado.",
        )

    if locale is None:
        locale = resolver_locale(empresa, framework)

    # Verificar se questionário foi preenchido
    qr = obter_respostas(db, empresa.id)
    questionario_preenchido = qr is not None

    plano_control_id = getattr(PlanoItem, "control_id")
    plano_empresa_id = getattr(PlanoItem, "empresa_id")
    plano_posicao = getattr(PlanoItem, "posicao")
    plano_nivel_conformidade = getattr(PlanoItem, "nivel_conformidade")
    ce_empresa_id = getattr(ControloEmpresaV2, "empresa_id")
    ce_framework_id = getattr(ControloEmpresaV2, "framework_id")
    ce_control_id = getattr(ControloEmpresaV2, "control_id")
    ce_nivel_atual = getattr(ControloEmpresaV2, "nivel_maturidade_atual")
    control_id_column = getattr(Control, "id")
    control_subdomain_id = getattr(Control, "subdomain_id")
    subdomain_id_column = getattr(Subdomain, "id")
    subdomain_domain_id = getattr(Subdomain, "domain_id")
    domain_id_column = getattr(Domain, "id")
    domain_framework_id = getattr(Domain, "framework_id")

    stmt = (
        select(PlanoItem, ControloEmpresaV2, Control, Domain)
        .join(
            ControloEmpresaV2,
            and_(
                ce_empresa_id == empresa.id,
                ce_framework_id == framework.id,
                ce_control_id == plano_control_id,
            ),
        )
        .join(Control, control_id_column == plano_control_id)
        .join(Subdomain, subdomain_id_column == control_subdomain_id)
        .join(
            Domain,
            and_(
                domain_id_column == subdomain_domain_id,
                domain_framework_id == framework.id,
            ),
        )
        .where(plano_empresa_id == empresa.id)
        .where(ce_nivel_atual < plano_nivel_conformidade)
    )

    # Isolamento por implementador: só mostrar controlos delegados a este utilizador
    if utilizador is not None and utilizador.role == RoleUtilizador.IMPLEMENTADOR:
        ce_implementador_id = getattr(ControloEmpresaV2, "implementador_id")
        stmt = stmt.where(ce_implementador_id == utilizador.id)

    # Itens pendentes por ordem de prioridade. O limite é só uma salvaguarda de
    # performance: o dashboard usa apenas os primeiros `limite` do tier mais baixo,
    # que estão sempre no início desta lista — logo o cap não altera o resultado.
    # (O framework QNRCS tem 107 controlos.)
    todos_pendentes = db.exec(
        stmt.order_by(plano_posicao).limit(50)
    ).all()

    # Filtrar apenas o tier mínimo com itens pendentes.
    # Exemplo: enquanto há Básico por fazer, só aparece Básico. Quando Básico termina,
    # o tier avança automaticamente para Substancial, sem regenerar o plano.
    if todos_pendentes:
        min_tier = min(r[0].nivel_conformidade for r in todos_pendentes)
        resultados = [
            r for r in todos_pendentes if r[0].nivel_conformidade == min_tier
        ][:max(1, limite)]
    else:
        resultados = []

    domain_locales = load_preferred_locales(
        db,
        DomainLocale,
        "domain_id",
        {domain.id for _, _, _, domain in resultados},
        locale,
        framework.default_locale,
    )
    control_locales = load_preferred_locales(
        db,
        ControlLocale,
        "control_id",
        {control.id for _, _, control, _ in resultados},
        locale,
        framework.default_locale,
    )

    # Enriquecer apenas os itens mínimos usados no dashboard
    itens_out: list[dict] = []
    for item, controlo_empresa, control, domain in resultados:
        loc_ctrl = control_locales.get(control.id)
        loc_dom = domain_locales.get(domain.id)
        estado = (
            controlo_empresa.estado.value
            if hasattr(controlo_empresa.estado, "value")
            else controlo_empresa.estado
        )

        itens_out.append({
            "posicao": item.posicao,
            "control_id": item.control_id,
            "codigo": control.code,
            "titulo": loc_ctrl.title if loc_ctrl else control.code,
            "descricao": loc_ctrl.description if loc_ctrl else "",
            "dominio_codigo": domain.code,
            "dominio_nome": loc_dom.name if loc_dom else domain.code,
            "mapeado_questionario": item.mapeado_questionario,
            "estado": estado,
        })

    return {
        "questionario_preenchido": questionario_preenchido,
        "itens": itens_out,
    }
