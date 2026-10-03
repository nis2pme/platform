"""Quando é que uma evidência é prova que se tem de guardar.

Uma regra só, usada por todos os caminhos por onde uma evidência pode sair — a
reciclagem, o apagar definitivamente, a substituição por uma versão nova — e pelo
ecrã. Enquanto cada caminho tinha a sua, um deles apagava prova que os outros
guardavam.

Motivos de retenção:
  * `historico` — consta da fotografia de provas de uma transição de estado de um
    controlo. Um controlo foi dado como implementado COM ela; apagá-la deixaria a
    história a apontar para o nada.
  * `dossie`    — existia quando se exportou um dossiê com ficheiros. O registo do
    dossiê diz *se* levou evidências, não *quais*, por isso retém-se tudo o que
    existia à data. Errar para o lado de guardar é barato; para o lado de apagar
    não tem volta.
  * `aprovado`  — sustenta hoje um controlo aprovado.

Ser uma versão substituída deixou de ser motivo por si: a cadeia de versões serve
para responder "que documento sustentava isto em março", e uma versão que nunca
sustentou nada não responde a nada — guardá-la para sempre era só guardar um
engano. Se foi prova, já está retida por um dos motivos acima.

Prazo de conservação (`EVIDENCIA_RETENCAO_ANOS`, 0 = sem prazo): uma prova sem
ligações ativas cujo motivo mais recente tenha mais do que o prazo deixa de estar
retida e segue o caminho normal da reciclagem. Uma prova que ainda sustenta um
controlo nunca expira.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlmodel import Session, select

from app.config import get_settings
from app.controlos.models import ControloEstadoHistorico
from app.dossie.models import DossieGerado
from app.evidencias.models import Evidencia, EvidenciaRequisito
from app.frameworks.models import ControloEmpresaV2
from app.shared.enums import EstadoControlo

MOTIVO_HISTORICO = "historico"
MOTIVO_DOSSIE = "dossie"
MOTIVO_APROVADO = "aprovado"

# Acima disto, filtrar a história em SQL por cada identificador custa mais do que
# ler a história da empresa inteira uma vez.
_FILTRO_SQL_ATE = 50


def _com_fuso(momento: datetime) -> datetime:
    # As datas guardadas em SQLite voltam sem fuso; compará-las com datas com fuso
    # levantaria TypeError.
    return momento if momento.tzinfo else momento.replace(tzinfo=timezone.utc)


def levou_ficheiros(ambito: str | None) -> bool:
    """Se um dossiê exportado levou ficheiros de evidências lá dentro."""
    try:
        return bool(json.loads(ambito or "{}").get("evidencias_ficheiros"))
    except (ValueError, TypeError, AttributeError):
        return False


def dossies_com_ficheiros(db: Session, empresa_id: uuid.UUID) -> list[DossieGerado]:
    return [
        dossie
        for dossie in db.exec(
            select(DossieGerado).where(DossieGerado.empresa_id == empresa_id)
        ).all()
        if levou_ficheiros(dossie.ambito)
    ]


def _datas_historico(
    db: Session, empresa_id: uuid.UUID, ids: set[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    """`{evidencia_id: transição mais recente que a lista}`."""
    consulta = select(
        ControloEstadoHistorico.evidencias, ControloEstadoHistorico.ocorrido_em
    ).where(
        ControloEstadoHistorico.empresa_id == empresa_id,
        ControloEstadoHistorico.evidencias.is_not(None),
    )
    if len(ids) <= _FILTRO_SQL_ATE:
        consulta = consulta.where(
            or_(*[ControloEstadoHistorico.evidencias.contains(str(i)) for i in ids])
        )

    datas: dict[uuid.UUID, datetime] = {}
    for bruto, ocorrido_em in db.exec(consulta).all():
        try:
            listadas = {uuid.UUID(str(item)) for item in json.loads(bruto or "[]")}
        except (ValueError, TypeError):
            continue
        for evidencia_id in listadas & ids:
            momento = _com_fuso(ocorrido_em)
            if evidencia_id not in datas or momento > datas[evidencia_id]:
                datas[evidencia_id] = momento
    return datas


def _ligadas_a_aprovados(db: Session, ids: set[uuid.UUID]) -> set[uuid.UUID]:
    return set(
        db.exec(
            select(EvidenciaRequisito.evidencia_id)
            .join(
                ControloEmpresaV2,
                ControloEmpresaV2.id == EvidenciaRequisito.requisito_id,
            )
            .where(
                EvidenciaRequisito.evidencia_id.in_(list(ids)),
                EvidenciaRequisito.desligado_em.is_(None),
                ControloEmpresaV2.estado == EstadoControlo.APROVADO,
            )
        ).all()
    )


def _com_ligacao_ativa(db: Session, ids: set[uuid.UUID]) -> set[uuid.UUID]:
    return set(
        db.exec(
            select(EvidenciaRequisito.evidencia_id).where(
                EvidenciaRequisito.evidencia_id.in_(list(ids)),
                EvidenciaRequisito.desligado_em.is_(None),
            )
        ).all()
    )


def motivos_em_lote(
    db: Session,
    empresa_id: uuid.UUID,
    ids: Iterable[uuid.UUID],
    anos: int | None = None,
) -> dict[uuid.UUID, set[str]]:
    """Motivos de retenção de cada evidência. Só aparecem as que têm algum.

    `anos` é o prazo de conservação; por omissão, o da configuração.
    """
    alvo = set(ids)
    if not alvo:
        return {}
    if anos is None:
        anos = get_settings().EVIDENCIA_RETENCAO_ANOS

    evidencias = {
        ev.id: ev
        for ev in db.exec(
            select(Evidencia).where(
                Evidencia.empresa_id == empresa_id, Evidencia.id.in_(list(alvo))
            )
        ).all()
    }
    alvo &= set(evidencias)
    if not alvo:
        return {}

    motivos: dict[uuid.UUID, set[str]] = {}
    datas: dict[uuid.UUID, datetime] = {}

    def _anotar(evidencia_id: uuid.UUID, motivo: str, momento: datetime) -> None:
        motivos.setdefault(evidencia_id, set()).add(motivo)
        if evidencia_id not in datas or momento > datas[evidencia_id]:
            datas[evidencia_id] = momento

    for evidencia_id, momento in _datas_historico(db, empresa_id, alvo).items():
        _anotar(evidencia_id, MOTIVO_HISTORICO, momento)

    datas_dossies = sorted(
        _com_fuso(d.criado_em) for d in dossies_com_ficheiros(db, empresa_id)
    )
    if datas_dossies:
        for evidencia_id in alvo:
            criada = _com_fuso(evidencias[evidencia_id].created_at)
            posteriores = [d for d in datas_dossies if d >= criada]
            if posteriores:
                _anotar(evidencia_id, MOTIVO_DOSSIE, posteriores[-1])

    agora = datetime.now(timezone.utc)
    for evidencia_id in _ligadas_a_aprovados(db, alvo):
        _anotar(evidencia_id, MOTIVO_APROVADO, agora)

    if anos and anos > 0 and motivos:
        limite = agora - timedelta(days=365 * anos)
        ligadas = _com_ligacao_ativa(db, set(motivos))
        for evidencia_id in list(motivos):
            if evidencia_id not in ligadas and datas[evidencia_id] < limite:
                del motivos[evidencia_id]

    return motivos


def motivos_retencao(db: Session, evidencia: Evidencia) -> set[str]:
    return motivos_em_lote(db, evidencia.empresa_id, [evidencia.id]).get(
        evidencia.id, set()
    )


def ids_retidos(
    db: Session, empresa_id: uuid.UUID, ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    return set(motivos_em_lote(db, empresa_id, ids))
