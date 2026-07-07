"""QNRCS 2026 change_1: reclassificação de 8 controlos + substituição de ID.GA-4

Aplica a atualização regulamentar do QNRCS a uma instalação já existente (a definição
nova vem baked na imagem, em frameworks/qnrcs-2026-s). Como o seed só corre com a BD
vazia e o importer nunca apaga, esta migração de dados é a via de entrega on-prem:
corre automaticamente no `alembic upgrade head` do arranque.

Passos: pré-limpeza dos controlos recriados/substituídos → reimport da definição nova →
prune de órfãos (thresholds/SRs removidos) → reset de estado dos recriados →
soft-delete das evidências do ID.GA-4 → recompute do nível dos tenants → notificação.

A orquestração vive em apply_upgrade(db) (testável com uma Session qualquer); upgrade()
apenas liga-a à conexão do Alembic.

Revision ID: 002_qnrcs_change_1
Revises: 001_schema_completo
Create Date: 2026-07-07
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "002_qnrcs_change_1"
down_revision = "001_schema_completo"
branch_labels = None
depends_on = None

REGISTRY_ID = "qnrcs-2026-s"
AFFECTED = [
    "GR.FR-1", "GR.CA-3", "GR.CO-6", "ID.GA-4", "ID.GA-6",
    "ID.GA-7", "PR.RI-2", "PR.RI-3", "RC.PR-3",
]
# Controlos cujo conteúdo do nível mudou (recriar/substituir) → força re-verificação.
RECREATE = ["GR.CA-3", "GR.CO-6", "ID.GA-7", "PR.RI-3", "ID.GA-4"]
# Controlos substituídos cujas evidências carregadas deixaram de ser válidas.
SUBSTITUTE_EVIDENCE = ["ID.GA-4"]

NOTIF_TIPO = "framework_atualizada"
NOTIF_TITULO = "Framework QNRCS atualizada"
NOTIF_MENSAGEM = (
    "O QNRCS 2026 foi atualizado na sequência da atualização do regulamento. "
    "9 controlos foram reclassificados ou substituídos (GR.FR-1, GR.CA-3, GR.CO-6, "
    "ID.GA-4, ID.GA-6, ID.GA-7, PR.RI-2, PR.RI-3, RC.PR-3). O nível de maturidade foi "
    "recalculado; alguns controlos podem necessitar de nova verificação."
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
    """Aplica o change_1 a uma sessão de BD. Idempotente. Devolve estatísticas.

    Recebe a definição nova de framework.json baked na imagem; se a BD ainda não tiver
    o framework (fresh install), não faz nada — o seed do lifespan trata da importação.
    """
    from datetime import datetime, timezone

    from sqlmodel import select

    from app.evidencias.models import Evidencia
    from app.frameworks import update_ops
    from app.frameworks.import_service import importar_framework
    from app.frameworks.models import ControloEmpresaV2, Framework
    from app.frameworks.runtime import invalidar_cache_framework

    stats = {"aplicado": False}

    framework = db.exec(
        select(Framework).where(Framework.registry_id == REGISTRY_ID)
    ).first()
    if framework is None:
        return stats

    data, manifest, structure, profiles, locales = _load_framework_files()

    # Conjuntos válidos da definição nova (para o prune).
    valid_sr_codes: dict[str, set[str]] = {}

    def _walk(o):
        if isinstance(o, dict):
            if "sub_requirements" in o and "code" in o:
                valid_sr_codes[o["code"]] = {s["code"] for s in o["sub_requirements"]}
            for v in o.values():
                _walk(v)
        elif isinstance(o, list):
            for v in o:
                _walk(v)

    _walk(data)
    valid_threshold_pairs: set[tuple[str, str]] = set()
    for p in (profiles or {}).get("profiles", []):
        for th in p.get("thresholds", []):
            valid_threshold_pairs.add((p["code"], th["control_code"]))

    # 1. Pré-limpeza: apaga SRs+checks dos controlos recriados/substituídos.
    update_ops.apagar_subrequisitos_controlos(db, framework, RECREATE)

    # 2. Reimport idempotente da definição nova (updates + adds).
    importar_framework(db, manifest, structure, profiles, locales)
    framework = db.exec(
        select(Framework).where(Framework.registry_id == REGISTRY_ID)
    ).first()

    # 3. Prune de órfãos (thresholds de níveis removidos; SRs remanescentes).
    stats["prune"] = update_ops.prune_framework_orphans(
        db, framework, AFFECTED, valid_sr_codes, valid_threshold_pairs
    )

    # 4. Reset de estado dos controlos recriados/substituídos.
    update_ops.reset_estado_controlos(db, framework, RECREATE)

    # 5. Soft-delete das evidências dos controlos substituídos (ID.GA-4).
    now = datetime.now(timezone.utc)
    subs = update_ops.controls_por_codigo(db, framework, SUBSTITUTE_EVIDENCE)
    for c in subs.values():
        ce_ids = [
            ce.id
            for ce in db.exec(
                select(ControloEmpresaV2).where(ControloEmpresaV2.control_id == c.id)
            ).all()
        ]
        if not ce_ids:
            continue
        for ev in db.exec(
            select(Evidencia).where(
                Evidencia.controlo_empresa_v2_id.in_(ce_ids),
                Evidencia.deleted_at.is_(None),
            )
        ).all():
            ev.deleted_at = now
            db.add(ev)
    db.commit()

    # 6. Recompute do nível de maturidade dos tenants (+ snapshot de histórico).
    stats["empresas"] = update_ops.recompute_empresa_controlos(db, framework, AFFECTED)

    # 7. Notificar os admins das empresas afetadas (idempotente).
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
    # Atualização regulamentar de dados — não reversível (o conteúdo antigo deixou
    # de ser válido). O rollback faz-se por restauro da base de dados.
    pass
