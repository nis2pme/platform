"""
Prazos legais das notificações de incidentes.

Regime Jurídico da Cibersegurança (RJC, aprovado pelo Decreto-Lei n.º 125/2025),
arts. 41.º a 45.º, e RGPD, art. 33.º (violação de dados pessoais). Funções
puras: recebem os factos do incidente e o instante de referência e devolvem o
estado de cada marco. Não tocam na base de dados — o serviço, o tick de avisos
e o dossiê usam todos esta mesma conta, para a aplicação e o auditor nunca
mostrarem prazos diferentes.

Contagem:
- prazos em horas: tempo decorrido, literal (24 h são 24 h, mesmo na mudança
  de hora);
- prazos em dias úteis (CPA, art. 87.º, als. b), c) e f)): não conta o dia do
  evento; sábados, domingos e feriados nacionais não contam; a contagem faz-se
  na data de Lisboa e o prazo termina no fim desse dia.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

MARCOS = (
    "notificacao_inicial",
    "atualizacao",
    "fim_impacto",
    "relatorio_final",
    "intercalar",
    "cnpd",
)

_HORAS_INICIAL = 24        # RJC, art. 42.º, n.º 1
_HORAS_ATUALIZACAO = 72    # RJC, art. 42.º, n.º 3
_HORAS_FIM_IMPACTO = 24    # RJC, art. 43.º, n.º 1
_DIAS_UTEIS_FINAL = 30     # RJC, art. 44.º, n.º 1
_HORAS_INTERCALAR = 168    # RJC, art. 44.º, n.º 3 (periodicidade semanal)
_HORAS_CNPD = 72           # RGPD, art. 33.º, n.º 1

_FERIADOS = Path(__file__).parent / "dados" / "feriados_pt.json"


@dataclass(frozen=True)
class EntradaPrazos:
    """Os factos do incidente de que os prazos dependem. Datas naive = UTC."""

    significativo: bool | None
    conhecido_at: datetime
    significativo_em: datetime | None = None
    fim_impacto_em: datetime | None = None
    # Resolvido nas 2 h após a deteção (RJC, art. 41.º, n.º 2): confirmação
    # expressa de quem regista; nunca se infere das datas.
    resolvido_2h: bool = False
    # A notificação em 24 h era incompatível com a mitigação (art. 42.º, n.º 1),
    # com justificação registada.
    excecao_24h: bool = False
    atualizacao_necessaria: bool = False
    intercalar_pedido_em: datetime | None = None
    cnpd_aplicavel: bool = False
    # Entidade fora do âmbito do regime: as notificações são voluntárias (art. 45.º).
    voluntario: bool = False
    notificacao_inicial_at: datetime | None = None
    atualizacao_at: datetime | None = None
    fim_impacto_notificado_at: datetime | None = None
    relatorio_final_at: datetime | None = None
    intercalar_ultimo_at: datetime | None = None
    cnpd_notificado_at: datetime | None = None


@dataclass(frozen=True)
class Prazo:
    marco: str
    prazo: datetime | None       # UTC; None = a base ainda não existe
    base: str                    # campo de onde conta
    unidade: str                 # "horas" | "dias_uteis"
    quantidade: int
    obrigatorio: bool            # conta para avisos, atraso e auditor
    voluntario: bool
    provisorio: bool
    dispensado: bool
    motivo: str | None
    cumprido: bool
    cumprido_at: datetime | None
    em_atraso: bool
    horas_restantes: float | None


# ── Datas ────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _lisboa() -> ZoneInfo:
    return ZoneInfo("Europe/Lisbon")


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _pascoa(ano: int) -> date:
    """Domingo de Páscoa no calendário gregoriano (algoritmo de Meeus/Jones/Butcher)."""
    a = ano % 19
    b, c = divmod(ano, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    mes, dia = divmod(h + ll - 7 * m + 114, 31)
    return date(ano, mes, dia + 1)


@lru_cache(maxsize=1)
def _dados_feriados() -> dict:
    with _FERIADOS.open(encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=64)
def feriados(ano: int) -> frozenset[date]:
    """Feriados nacionais de um ano: as datas fixas e as que dependem da Páscoa."""
    dados = _dados_feriados()
    dias = {date(ano, int(mm), int(dd)) for mm, dd in (s.split("-") for s in dados["fixos"])}
    pascoa = _pascoa(ano)
    dias |= {pascoa + timedelta(days=int(desvio)) for desvio in dados["pascoa"].values()}
    return frozenset(dias)


def _dia_util(d: date) -> bool:
    return d.weekday() < 5 and d not in feriados(d.year)


def somar_dias_uteis(inicio: date, n: int) -> date:
    """Último dia de um prazo de `n` dias úteis contado a partir de `inicio`.

    O dia do evento não conta; conta-se a partir do dia seguinte. Como o
    resultado é sempre um dia útil, um fim de prazo em dia de serviço fechado
    já cai no dia útil seguinte."""
    if n < 1:
        raise ValueError("o prazo tem de ter pelo menos um dia útil")
    d, contados = inicio, 0
    while contados < n:
        d += timedelta(days=1)
        if _dia_util(d):
            contados += 1
    return d


def data_lisboa(instante: datetime) -> date:
    """O dia civil em Portugal continental de um instante (00:30 de Lisboa no
    verão ainda é o dia anterior em UTC)."""
    return _utc(instante).astimezone(_lisboa()).date()


def fim_do_dia_lisboa(d: date) -> datetime:
    """Último instante do dia `d` em Lisboa, em UTC."""
    meia_noite = datetime.combine(d + timedelta(days=1), time(0), tzinfo=_lisboa())
    return meia_noite.astimezone(timezone.utc) - timedelta(microseconds=1)


def _mais_dias_uteis(instante: datetime, n: int) -> datetime:
    return fim_do_dia_lisboa(somar_dias_uteis(data_lisboa(instante), n))


# ── Marcos ───────────────────────────────────────────────────────────────────

def _montar(
    marco: str,
    prazo: datetime | None,
    base: str,
    unidade: str,
    quantidade: int,
    agora: datetime,
    *,
    voluntario: bool,
    cumprido_at: datetime | None,
    dispensa: str | None = None,
    motivo: str | None = None,
    provisorio: bool = False,
    excecao: bool = False,
    recorrente: bool = False,
) -> Prazo:
    cumprido_at = _utc(cumprido_at)
    # Um marco recorrente (o intercalar) nunca fica cumprido: o prazo seguinte
    # conta a partir do último enviado.
    cumprido = cumprido_at is not None and not recorrente
    dispensado = dispensa is not None
    if dispensa is not None:
        motivo = dispensa
    elif excecao and not cumprido:
        motivo = "excecao_justificada"
    obrigatorio = not voluntario and not dispensado
    horas = None if (cumprido or prazo is None) else (prazo - agora).total_seconds() / 3600
    em_atraso = (
        obrigatorio and not cumprido and not excecao
        and prazo is not None and agora > prazo
    )
    return Prazo(
        marco=marco, prazo=prazo, base=base, unidade=unidade, quantidade=quantidade,
        obrigatorio=obrigatorio, voluntario=voluntario, provisorio=provisorio,
        dispensado=dispensado, motivo=motivo, cumprido=cumprido, cumprido_at=cumprido_at,
        em_atraso=em_atraso, horas_restantes=horas,
    )


def calcular_prazos(e: EntradaPrazos, agora: datetime) -> list[Prazo]:
    """Estado de cada marco do incidente face a `agora`, pela ordem de `MARCOS`.

    Um incidente classificado como não significativo não tem marcos do regime;
    «por avaliar» (`None`) conta como significativo, porque o relógio corre
    desde o conhecimento. O `intercalar` só aparece se a autoridade o pediu e o
    da CNPD só se houve violação de dados pessoais — esse vale mesmo para um
    incidente não significativo e para quem está fora do âmbito do regime."""
    agora = _utc(agora)
    conhecido = _utc(e.conhecido_at)
    vol = e.voluntario
    saida: list[Prazo] = []

    if e.significativo is not False:
        if e.significativo_em is not None:
            base_sig, nome_base = _utc(e.significativo_em), "significativo_em"
        else:
            base_sig, nome_base = conhecido, "conhecido_at"
        regra_2h = "regra_2h" if e.resolvido_2h else None

        saida.append(_montar(
            "notificacao_inicial", base_sig + timedelta(hours=_HORAS_INICIAL), nome_base,
            "horas", _HORAS_INICIAL, agora, voluntario=vol,
            cumprido_at=e.notificacao_inicial_at, dispensa=regra_2h, excecao=e.excecao_24h,
        ))
        saida.append(_montar(
            "atualizacao", base_sig + timedelta(hours=_HORAS_ATUALIZACAO), nome_base,
            "horas", _HORAS_ATUALIZACAO, agora, voluntario=vol,
            cumprido_at=e.atualizacao_at,
            dispensa=regra_2h or (None if e.atualizacao_necessaria else "nao_necessaria"),
        ))

        # O fim de impacto é devido sempre, também quando a regra das 2 h
        # dispensa tudo o resto (art. 41.º, n.º 2).
        fim = _utc(e.fim_impacto_em)
        saida.append(_montar(
            "fim_impacto", fim + timedelta(hours=_HORAS_FIM_IMPACTO) if fim else None,
            "fim_impacto_em", "horas", _HORAS_FIM_IMPACTO, agora, voluntario=vol,
            cumprido_at=e.fim_impacto_notificado_at,
            motivo=None if fim else "sem_fim_impacto",
        ))

        # O relatório final conta da notificação de fim de impacto (art. 44.º,
        # n.º 1). Enquanto ela não estiver marcada como enviada, o prazo é
        # provisório e conta do último momento em que ela podia ser enviada:
        # nunca dá uma data mais tarde do que a da lei.
        notificado = _utc(e.fim_impacto_notificado_at)
        if notificado is not None:
            prazo_final, base_final, provisorio = (
                _mais_dias_uteis(notificado, _DIAS_UTEIS_FINAL), "fim_impacto_notificado_at", False)
        elif fim is not None:
            prazo_final, base_final, provisorio = (
                _mais_dias_uteis(fim + timedelta(hours=_HORAS_FIM_IMPACTO), _DIAS_UTEIS_FINAL),
                "fim_impacto_em", True)
        else:
            prazo_final, base_final, provisorio = None, "fim_impacto_notificado_at", False
        saida.append(_montar(
            "relatorio_final", prazo_final, base_final, "dias_uteis", _DIAS_UTEIS_FINAL, agora,
            voluntario=vol, cumprido_at=e.relatorio_final_at, dispensa=regra_2h,
            motivo=None if prazo_final else "sem_fim_impacto", provisorio=provisorio,
        ))

        if e.intercalar_pedido_em is not None:
            pedido = _utc(e.intercalar_pedido_em)
            ultimo = _utc(e.intercalar_ultimo_at)
            if ultimo is not None and ultimo > pedido:
                base_int, nome_int = ultimo, "intercalar_ultimo_at"
            else:
                base_int, nome_int = pedido, "intercalar_pedido_em"
            saida.append(_montar(
                "intercalar", base_int + timedelta(hours=_HORAS_INTERCALAR), nome_int,
                "horas", _HORAS_INTERCALAR, agora, voluntario=vol, cumprido_at=ultimo,
                dispensa="relatorio_final_entregue" if e.relatorio_final_at else None,
                recorrente=True,
            ))

    if e.cnpd_aplicavel:
        saida.append(_montar(
            "cnpd", conhecido + timedelta(hours=_HORAS_CNPD), "conhecido_at",
            "horas", _HORAS_CNPD, agora, voluntario=False,
            cumprido_at=e.cnpd_notificado_at,
        ))

    return saida
