"""
Lógica de negócio do Plano de Ações Prioritárias.

Fluxo:
1. O utilizador responde a 10 perguntas de diagnóstico rápido.
2. As respostas são convertidas em scores (A=1…E=5, NS=1 pior cenário).
3. Cada pergunta mapeia a controlos QNRCS específicos.
4. O algoritmo gera o roadmap ordenado com 7 regras de priorização:
   R1 — Todos os controlos Básico antes de Substancial antes de Elevado
   R2 — Dentro de cada tier, os mapeados pelo questionário primeiro
   R3 — Entre mapeados, a pior resposta primeiro
   R4 — Gap descendente (gap = alvo_do_nivel − nivel_atual), para todos
   R5 — Ordem do domínio, como o referencial a declara
   R6 — Ordem do subdomínio, como o referencial a declara
   R7 — Número do controlo
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, case, func, or_
from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.empresas.models import Empresa
from app.auth.models import Utilizador
from app.shared.capacidades import ClasseAcao, so_atribuidos
from app.shared.enums import EstadoControlo
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

    As regras de priorização estão junto à ordenação, mais abaixo — escritas uma
    só vez, porque uma cópia num sítio e outra noutro divergem à primeira
    alteração e deixa de se saber qual delas descreve o código.
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
    # Controlos excluídos do âmbito não entram no plano de ação.
    from app.shared.scoring import controlo_aplicavel
    rows = [row for row in rows if controlo_aplicavel(row.ce)]
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
            # A ordem vem do referencial, que a declara ao importar o catálogo.
            # Uma lista de domínios escrita aqui seria uma segunda versão da
            # mesma verdade, e divergiria do referencial à primeira revisão dele.
            "dominio_ordem": row.domain.order,
            "subdominio_ordem": row.subdomain.order,
            "controlo_num": _extrair_numero_controlo(row.control.code),
            "nivel_atual": row.ce.nivel_maturidade_atual,
        })

    # -----------------------------------------------------------------------
    # Algoritmo de ordenação
    # -----------------------------------------------------------------------
    # R1: nivel_conformidade ASC → Básico(1) antes de Substancial(2) antes de Elevado(3)
    # R2: not mapeado → False(0) antes de True(1) → mapeados primeiro
    # R3: score_questionario ASC só nos mapeados: A/NS(1) antes de B(2), etc.
    # R4: gap DESC — quem está mais longe do alvo primeiro. Vale para todos e não
    #     só para os mapeados: entre dois controlos que o questionário não
    #     distingue, o que exige mais trabalho é o que exige mais antecedência.
    #
    # Esgotado o que distingue os controlos entre si, a fila segue a sequência do
    # próprio referencial — domínio, subdomínio, número —, que é como ele está
    # escrito. Quem o conhece reconhece a lista.
    # R5: dominio_ordem ASC
    # R6: subdominio_ordem ASC — sem ela os controlos saíam intercalados entre
    #     subdomínios, porque o número final do código não diz a que família o
    #     controlo pertence: GR.FR-1 vinha à frente de GR.CO-6 por ser "1".
    # R7: controlo_num ASC

    items_raw.sort(key=lambda x: (
        x["nivel_conformidade"],   # R1
        not x["mapeado"],          # R2: 0=mapeado (primeiro), 1=não-mapeado (depois)
        x["score_questionario"] if x["mapeado"] else 99,  # R3: pior resposta primeiro
        -x["gap"],                 # R4: mais longe do alvo primeiro
        x["dominio_ordem"],        # R5: ordem do domínio no referencial
        x["subdominio_ordem"],     # R6: ordem do subdomínio no referencial
        x["controlo_num"],         # R7: número do controlo
    ))

    # Apagar plano anterior desta empresa
    itens_antigos = db.exec(
        select(PlanoItem).where(PlanoItem.empresa_id == empresa.id)
    ).all()
    for item in itens_antigos:
        db.delete(item)

    # O flush aqui não é opcional. Dentro do mesmo flush o SQLAlchemy agrupa por
    # tipo de operação e emite os INSERT antes dos DELETE, independentemente da
    # ordem por que foram pedidos. Como (empresa_id, control_id) é único, as
    # linhas novas colidiriam com as antigas e a regeneração falharia — sempre
    # que já existisse plano, ou seja, em todas as vezes menos a primeira.
    db.flush()

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

    O que conta como pendente decide-se aqui e em mais lado nenhum: quem consome
    esta lista desenha-a como ela vem. Duas definições de "feito" — uma no
    servidor, outra em cada ecrã — divergem, e a lista encolhe sem explicação.
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
    ce_estado = getattr(ControloEmpresaV2, "estado")
    control_id_column = getattr(Control, "id")
    control_subdomain_id = getattr(Control, "subdomain_id")
    subdomain_id_column = getattr(Subdomain, "id")
    subdomain_domain_id = getattr(Subdomain, "domain_id")
    domain_id_column = getattr(Domain, "id")
    domain_framework_id = getattr(Domain, "framework_id")

    # O que conta como pendente, escrito uma só vez.
    filtros = [
        plano_empresa_id == empresa.id,
        # Pendente = ainda não chegou ao nível exigido. O controlo devolvido pelo
        # aprovador volta à lista mesmo com o nível já cumprido: o trabalho foi
        # recusado e há quem esteja à espera dele.
        or_(
            ce_nivel_atual < plano_nivel_conformidade,
            ce_estado == EstadoControlo.NAO_APROVADO,
        ),
        # Excluído do âmbito sai da lista mesmo quando o plano é anterior à
        # exclusão — marcar um controlo como não aplicável não regenera o plano.
        ce_estado != EstadoControlo.NAO_APLICAVEL,
    ]

    # Mesmo critério de alcance dos controlos: quem só alcança o que lhe está
    # atribuído vê no plano apenas os controlos que lhe foram delegados.
    if utilizador is not None and so_atribuidos(
        utilizador, "controlos", ClasseAcao.VER
    ):
        ce_implementador_id = getattr(ControloEmpresaV2, "implementador_id")
        filtros.append(ce_implementador_id == utilizador.id)

    juncao_controlo = and_(
        ce_empresa_id == empresa.id,
        ce_framework_id == framework.id,
        ce_control_id == plano_control_id,
    )

    stmt = (
        select(PlanoItem, ControloEmpresaV2, Control, Domain)
        .join(ControloEmpresaV2, juncao_controlo)
        .join(Control, control_id_column == plano_control_id)
        .join(Subdomain, subdomain_id_column == control_subdomain_id)
        .join(
            Domain,
            and_(
                domain_id_column == subdomain_domain_id,
                domain_framework_id == framework.id,
            ),
        )
        .where(*filtros)
    )

    # Itens pendentes por ordem de prioridade. O limite é só uma salvaguarda de
    # performance: o dashboard usa apenas os primeiros `limite` do tier mais baixo,
    # que estão sempre no início desta lista — logo o cap não altera o resultado.
    # (O framework QNRCS tem 107 controlos.)
    #
    # O tier vem à frente de tudo: um controlo devolvido pelo aprovador sobe ao
    # topo do SEU tier, nunca à frente de um tier mais baixo. A ordem em camadas
    # é o próprio critério do plano — não se abre exceção a ela por retrabalho.
    retrabalho_primeiro = case(
        (ce_estado == EstadoControlo.NAO_APROVADO, 0), else_=1
    )
    todos_pendentes = db.exec(
        stmt.order_by(
            plano_nivel_conformidade, retrabalho_primeiro, plano_posicao
        ).limit(50)
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

    # Enriquecer apenas os itens mínimos usados no dashboard.
    #
    # A ordem numerada é a desta lista, não a posição do controlo na fila
    # completa: como só se mostram os pendentes, as posições da fila saem com
    # buracos (1, 2, 4) e leem-se como um erro. Quem consome isto está a
    # perguntar "o que faço a seguir", e a resposta é primeiro, segundo, terceiro.
    itens_out: list[dict] = []
    for ordem, (item, controlo_empresa, control, domain) in enumerate(resultados, 1):
        loc_ctrl = control_locales.get(control.id)
        loc_dom = domain_locales.get(domain.id)
        estado = (
            controlo_empresa.estado.value
            if hasattr(controlo_empresa.estado, "value")
            else controlo_empresa.estado
        )

        itens_out.append({
            "ordem": ordem,
            "control_id": item.control_id,
            "codigo": control.code,
            "titulo": loc_ctrl.title if loc_ctrl else control.code,
            "descricao": loc_ctrl.description if loc_ctrl else "",
            "dominio_codigo": domain.code,
            "dominio_nome": loc_dom.name if loc_dom else domain.code,
            "mapeado_questionario": item.mapeado_questionario,
            "estado": estado,
            # O nível é a medida objetiva do que está feito; o estado é o que
            # alguém declarou. Ambos vão para o cliente porque é a divergência
            # entre eles que explica um controlo declarado feito continuar aqui.
            "nivel_atual": controlo_empresa.nivel_maturidade_atual,
            "nivel_alvo": item.nivel_conformidade,
        })

    return {
        "questionario_preenchido": questionario_preenchido,
        "itens": itens_out,
    }
