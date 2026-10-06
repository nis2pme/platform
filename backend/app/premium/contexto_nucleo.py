"""
O que o núcleo sabe e o sidecar não, e que os módulos premium lhe pedem.

O sidecar nunca lê a base do núcleo. Os controlos, o estado e a maturidade de cada
um são do núcleo; quando uma verificação ou um cálculo do sidecar precisa deles,
é aqui que se montam, para todos os routers e para o ciclo agendado dos conetores
— sem que um router tenha de importar de outro.
"""
from __future__ import annotations

from sqlmodel import select

from app.frameworks.models import ControloEmpresaV2
from app.shared.enums import EstadoControlo


def declaracoes_dos_controlos(db, empresa) -> dict[str, str]:
    """Código do controlo → estado declarado, para a régua por nível e a
    deteção de contradições (declarado implementado vs. observado) no sidecar."""
    from app.frameworks.runtime import load_company_control_rows, resolver_framework_empresa

    framework = resolver_framework_empresa(db, empresa)
    rows = load_company_control_rows(db, empresa.id, framework.id)
    declaracoes: dict[str, str] = {}
    for row in rows:
        estado = getattr(row.ce, "estado", None)
        declaracoes[row.control.code] = getattr(estado, "value", "") or ""
    return declaracoes


def maturidades_dos_controlos(db, empresa_id) -> dict[str, int]:
    """Mapa {controlo_id → nível de maturidade} do tenant, para o sidecar calcular o
    risco residual (a maturidade é do núcleo; o sidecar nunca lê o core-db)."""
    rows = db.exec(
        select(
            ControloEmpresaV2.id,
            ControloEmpresaV2.control_id,
            ControloEmpresaV2.nivel_maturidade_atual,
            ControloEmpresaV2.estado,
        ).where(ControloEmpresaV2.empresa_id == empresa_id)
    ).all()
    # O tratamento guarda o id do controlo que o ecrã lhe deu, que é o do quadro
    # (o de `/controlos`); dados antigos podem ter o da empresa. Os dois servem.
    mapa: dict[str, int] = {}
    for ce_id, control_id, nivel, estado in rows:
        # Um controlo «não aplicável» saiu do âmbito: a maturidade que tinha não
        # se apaga ao marcá-lo, mas já não protege nada, por isso não reduz o
        # residual. Fora do mapa, o sidecar trata-o como um controlo sem maturidade.
        if estado == EstadoControlo.NAO_APLICAVEL:
            continue
        mapa[str(ce_id)] = int(nivel or 0)
        if control_id is not None:
            mapa.setdefault(str(control_id), int(nivel or 0))
    return mapa
