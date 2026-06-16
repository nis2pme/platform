"""
Motor de pontuação UCF (Universal Control Framework).
Implementa o modelo Gated Maturity (inspirado no CMMI) com 5 níveis.

REGRA: O scoring é SEMPRE calculado no backend — nunca no frontend.
Estes helpers são chamados em service.py cada vez que um check é alterado.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# Nível de maturidade de um controlo
# ---------------------------------------------------------------------------

def calcular_nivel_controlo(
    checks_concluidos: set,
    checks_definidos: list,
    max_nivel: int = 5,
) -> int:
    """
    Calcula o nível de maturidade atual de um controlo (0–max_nivel).

    Modelo Gated: a progressão é cumulativa e bloqueada por gating.
    Não é possível atingir o nível N sem ter todos os níveis anteriores.

    Para passar de nível N para N+1:
      1. Todos os checks obrigatórios do nível N devem estar concluídos.
      2. >= 80% de todos os checks do nível N devem estar concluídos.

    Args:
        checks_concluidos: set de IDs (UUID) dos checks que a empresa concluiu,
                           filtrado para este controlo.
        checks_definidos:  lista de objectos com atributos `nivel`/`maturity_level`,
                           `obrigatorio`/`mandatory` e `id`
                           (suporta V1 ControloNivelCheck e V2 SubRequirement).
        max_nivel:         nível máximo do framework (default 5; QNRCS usa 4).

    Returns:
        int de 0 a max_nivel. Devolve 0 se nem o nível 1 estiver completo.
    """
    # Suporte duck-typing: V1 usa .nivel/.obrigatorio; V2 usa .maturity_level/.mandatory
    def _nivel(c: object) -> int:
        return getattr(c, "nivel", None) or getattr(c, "maturity_level", 0)

    def _obrigatorio(c: object) -> bool:
        v = getattr(c, "obrigatorio", None)
        if v is not None:
            return bool(v)
        return bool(getattr(c, "mandatory", True))

    # Rastreia o último nível que teve SRs E foi passado com sucesso.
    # Corrige dois bugs em cadeia:
    #   Bug A: controlos sem SRs no nível 1 (começam no 2) recebiam nível 1
    #          "grátis" porque `nivel - 1` ao falhar no nível 2 devolvia 1.
    #   Bug B: controlos sem SRs no nível 4 ("Suplementar" vazio) recebiam
    #          nível 4 automaticamente porque o loop terminava sem falhar.
    ultimo_nivel_passado = 0

    for nivel in range(1, max_nivel + 1):
        checks_nivel = [c for c in checks_definidos if _nivel(c) == nivel]

        if not checks_nivel:
            # Nível sem SRs definidos — salta sem alterar o score acumulado.
            # Não avança nem recua: aguarda que o próximo nível com SRs decida.
            continue

        obrigatorios = [c for c in checks_nivel if _obrigatorio(c)]
        total_nivel = len(checks_nivel)
        total_concluidos = sum(1 for c in checks_nivel if c.id in checks_concluidos)

        todos_obrig_ok = all(c.id in checks_concluidos for c in obrigatorios)
        percentagem_ok = (total_concluidos / total_nivel) >= 0.8

        if not (todos_obrig_ok and percentagem_ok):
            # Falhou este nível — devolve o último nível real que passou
            return ultimo_nivel_passado

        # Passou este nível com SRs — actualiza o último nível passado
        ultimo_nivel_passado = nivel

    # Todos os níveis com SRs foram passados — devolve o último real (não max_nivel)
    return ultimo_nivel_passado


# ---------------------------------------------------------------------------
# Score de um domínio
# ---------------------------------------------------------------------------

def calcular_score_dominio(controlos_empresa: list) -> int:
    """
    Score do domínio = nível do controlo mais baixo dentro desse domínio.

    Um domínio não pode ser considerado seguro se qualquer controlo estiver a falhar.
    Devolve 0 se não houver controlos.

    Args:
        controlos_empresa: lista de objectos ControloEmpresa com atributo
                           `nivel_maturidade_atual` (int).
    """
    if not controlos_empresa:
        return 0
    return min(ce.nivel_maturidade_atual for ce in controlos_empresa)


# ---------------------------------------------------------------------------
# Score global da empresa
# ---------------------------------------------------------------------------

def calcular_score_global(
    scores_dominio: dict[str, int],
    controlos_criticos: list,
) -> int:
    """
    Score global = mínimo entre:
      - média dos scores de domínio (arredondada para baixo)
      - nível mínimo de todos os controlos críticos

    Controlos com `critico=True` bloqueiam o score global
    independentemente dos scores de domínio.

    Args:
        scores_dominio:     dict {codigo_dominio: score_int} para todos os domínios.
        controlos_criticos: lista de ControloEmpresa onde controlo.critico=True,
                            com atributo `nivel_maturidade_atual`.

    Returns:
        int 0–5.
    """
    if not scores_dominio:
        return 0

    media = sum(scores_dominio.values()) / len(scores_dominio)
    nivel_criticos = min(
        (c.nivel_maturidade_atual for c in controlos_criticos),
        default=5,
    )
    return min(int(media), nivel_criticos)