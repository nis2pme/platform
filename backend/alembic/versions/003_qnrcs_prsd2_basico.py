"""QNRCS 2026: completar níveis em falta em 5 controlos (PR.SD-2 + 4 do Elevado)

Alinha a definição da framework com o regulamento em 5 controlos onde faltava um nível:

  - PR.SD-2 (dados em trânsito) ganha medida de nível Básico além do Elevado que já
    existia: 4 SRs novos de nível 1 (SR-001..004) e os 5 SRs de nível 3 existentes
    deslocam de código SR-001..005 → SR-005..009 (para o Básico ficar com os códigos
    baixos, seguindo a convenção); thresholds @ nível 1 nos perfis básico e substancial.
  - GR.CA-5, GR.CO-5, GR.GR-5, PR.GA-2: o threshold do perfil elevado sobe de 2 → 3.
    O GR.CA-5 já tinha SRs e locale de nível 3 (só faltava o threshold); os restantes três
    ganham SRs + locale de nível 3, acrescentados no topo (append, sem renumeração).

Como o seed só corre com a BD vazia e o importer nunca apaga, esta migração de dados é a
via de entrega on-prem: corre no `alembic upgrade head` do arranque.

Passos: renomear (in-place, mesmo UUID) os SRs de nível 3 do PR.SD-2 para libertar os
códigos baixos → reimport da definição nova (cria os SRs/thresholds/locale em falta e
sobe os thresholds do elevado) → recompute do nível dos tenants nos controlos afetados →
notificação. Os checks concluídos são preservados (referenciam o SR pelo UUID); nada é
apagado. Só o PR.SD-2 precisa de renomeação — os outros 4 são só append/threshold, que o
reimport trata de forma idempotente.

A orquestração vive em apply_upgrade(db) (testável com uma Session qualquer); upgrade()
apenas liga-a à conexão do Alembic.

Revision ID: 003_qnrcs_prsd2_basico
Revises: 002_qnrcs_change_1
Create Date: 2026-07-09
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "003_qnrcs_prsd2_basico"
down_revision = "002_qnrcs_change_1"
branch_labels = None
depends_on = None

REGISTRY_ID = "qnrcs-2026-s"
CONTROL = "PR.SD-2"
# Os 5 SRs de nível 3 do PR.SD-2 deslocam +4 para libertar SR-001..004 ao nível Básico.
SR_REMAP = {
    f"{CONTROL}.SR-001": f"{CONTROL}.SR-005",
    f"{CONTROL}.SR-002": f"{CONTROL}.SR-006",
    f"{CONTROL}.SR-003": f"{CONTROL}.SR-007",
    f"{CONTROL}.SR-004": f"{CONTROL}.SR-008",
    f"{CONTROL}.SR-005": f"{CONTROL}.SR-009",
}
# Controlos cujo threshold do perfil elevado sobe 2 → 3 (com SRs/locale de nível 3 novos
# ou já existentes). Tratados por append/threshold no reimport — sem renomeação.
CONTROLS_ELEVADO = ["GR.CA-5", "GR.CO-5", "GR.GR-5", "PR.GA-2"]
# Todos os controlos afetados (para o recompute do nível dos tenants).
CONTROLS_AFETADOS = [CONTROL] + CONTROLS_ELEVADO

NOTIF_TIPO = "framework_atualizada_022"
NOTIF_TITULO = "Framework QNRCS atualizada"
NOTIF_MENSAGEM = (
    "O QNRCS 2026 foi atualizado: o controlo PR.SD-2 (proteção dos dados em trânsito) "
    "passou a ter medida de nível Básico e os controlos GR.CA-5, GR.CO-5, GR.GR-5 e "
    "PR.GA-2 passaram a exigir o nível Elevado. O progresso anterior foi preservado, mas "
    "o nível de maturidade destes controlos foi recalculado — poderá ser necessário "
    "concluir os novos requisitos."
)


def _framework_dir():
    """Resolve o diretório de qnrcs-2026-s, funcionando na árvore docker (frameworks/)
    e na árvore dev (registry/), via env var ou procurando nos diretórios pais."""
    import os
    from pathlib import Path

    for env in ("FRAMEWORKS_DIR", "REGISTRY_DIR"):
        v = os.environ.get(env)
        if v and (Path(v) / REGISTRY_ID / "framework.json").exists():
            return Path(v) / REGISTRY_ID
    here = Path(__file__).resolve()
    for parent in here.parents:
        for name in ("frameworks", "registry"):
            cand = parent / name / REGISTRY_ID / "framework.json"
            if cand.exists():
                return cand.parent
    raise RuntimeError(f"Diretório do framework '{REGISTRY_ID}' não encontrado.")


def _load_framework_files():
    import json

    base = _framework_dir()
    data = json.loads((base / "framework.json").read_text(encoding="utf-8"))
    manifest = {k: v for k, v in data.items() if k not in ("profiles", "domains")}
    structure = {"domains": data.get("domains", [])}
    profiles_raw = data.get("profiles")
    profiles = {"profiles": profiles_raw} if profiles_raw else None
    locales: dict[str, dict] = {}
    ldir = base / "locales"
    if ldir.exists():
        for lf in ldir.glob("*.json"):
            locales[lf.stem] = json.loads(lf.read_text(encoding="utf-8"))
    return data, manifest, structure, profiles, locales


def apply_upgrade(db) -> dict:
    """Aplica a adição do nível Básico ao PR.SD-2. Idempotente. Devolve estatísticas.

    Recebe a definição nova de framework.json baked na imagem; se a BD ainda não tiver
    o framework (fresh install), não faz nada — o seed do lifespan trata da importação.
    """
    from sqlmodel import select

    from app.frameworks import update_ops
    from app.frameworks.import_service import importar_framework
    from app.frameworks.models import Framework, SubRequirement
    from app.frameworks.runtime import invalidar_cache_framework

    stats = {"aplicado": False, "srs_renomeados": 0}

    framework = db.exec(
        select(Framework).where(Framework.registry_id == REGISTRY_ID)
    ).first()
    if framework is None:
        return stats

    _, manifest, structure, profiles, locales = _load_framework_files()

    # 1. Renomear (in-place, mesmo UUID) os SRs de nível 3 → liberta SR-001..004 ao
    #    Básico e preserva os checks concluídos do Elevado. Só corre no estado ANTIGO
    #    (SR-001 ainda no nível 3): depois de aplicado, SR-001 passa a ser Básico
    #    (nível 1), pelo que o guard torna a operação idempotente e evita re-deslocar
    #    códigos que colidiriam com os já existentes.
    ctrl = update_ops.controls_por_codigo(db, framework, [CONTROL]).get(CONTROL)
    if ctrl is not None:
        sr001 = db.exec(
            select(SubRequirement).where(
                SubRequirement.control_id == ctrl.id,
                SubRequirement.code == f"{CONTROL}.SR-001",
            )
        ).first()
        if sr001 is not None and sr001.maturity_level == 3:
            stats["srs_renomeados"] = update_ops.renomear_subrequisitos(
                db, framework, CONTROL, SR_REMAP
            )

    # 2. Reimport idempotente: cria os SRs/thresholds/locale em falta (nível 1 do PR.SD-2
    #    e nível 3 dos controlos do elevado) e sobe os thresholds do perfil elevado 2 → 3;
    #    atualiza os SRs renomeados do PR.SD-2.
    importar_framework(db, manifest, structure, profiles, locales)
    framework = db.exec(
        select(Framework).where(Framework.registry_id == REGISTRY_ID)
    ).first()

    # 3. Recalcular o nível de maturidade dos tenants nos controlos afetados.
    stats["empresas"] = update_ops.recompute_empresa_controlos(
        db, framework, CONTROLS_AFETADOS
    )

    # 4. Notificar os admins das empresas (idempotente).
    _notificar(db)

    invalidar_cache_framework()
    stats["aplicado"] = True
    return stats


def _notificar(db):
    from sqlmodel import select

    from app.auth.models import RoleUtilizador, Utilizador
    from app.empresas.models import Empresa
    from app.notificacoes.models import Notificacao

    empresas = db.exec(select(Empresa).where(Empresa.deleted_at.is_(None))).all()
    for empresa in empresas:
        admins = db.exec(
            select(Utilizador).where(
                Utilizador.empresa_id == empresa.id,
                Utilizador.ativo.is_(True),
                Utilizador.role.in_([RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN]),
            )
        ).all()
        for u in admins:
            ja_existe = db.exec(
                select(Notificacao.id).where(
                    Notificacao.utilizador_id == u.id,
                    Notificacao.tipo == NOTIF_TIPO,
                )
            ).first()
            if ja_existe:
                continue
            db.add(
                Notificacao(
                    empresa_id=empresa.id,
                    utilizador_id=u.id,
                    tipo=NOTIF_TIPO,
                    titulo=NOTIF_TITULO,
                    mensagem=NOTIF_MENSAGEM,
                )
            )
    db.commit()


def upgrade():
    from sqlmodel import Session

    bind = op.get_bind()
    with Session(bind) as db:
        apply_upgrade(db)


def downgrade():
    # Atualização regulamentar de dados — não reversível. O rollback faz-se por
    # restauro da base de dados.
    pass
