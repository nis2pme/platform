"""
Seed automático do framework na startup (on-prem).
Se não existir nenhum framework na DB, importa todos os frameworks do diretório frameworks/.
"""
import json
import logging
import os
from pathlib import Path

from sqlmodel import Session, select

from app.frameworks.models import Framework

logger = logging.getLogger(__name__)

# Caminho para os frameworks — configurável via env var FRAMEWORKS_DIR.
# Local dev: parents[3] = raiz do projeto  →  <raiz>/frameworks/
# Docker:    FRAMEWORKS_DIR=/app/frameworks  (incorporado na imagem)
_FRAMEWORKS_DIR = Path(
    os.environ.get("FRAMEWORKS_DIR")
    or str(Path(__file__).resolve().parents[3] / "frameworks")
)


def _carregar_framework_dir(db: Session, framework_dir: Path) -> bool:
    """
    Carrega um único framework a partir de um diretório que contém framework.json.
    Devolve True se teve sucesso, False em caso de erro.
    """
    framework_json = framework_dir / "framework.json"
    if not framework_json.exists():
        return False

    try:
        data = json.loads(framework_json.read_text(encoding="utf-8"))

        # Separar manifest, structure e profiles (mesmo padrão do importador de frameworks)
        manifest = {k: v for k, v in data.items() if k not in ("profiles", "domains")}
        structure = {"domains": data.get("domains", [])}
        profiles_raw = data.get("profiles")
        profiles = {"profiles": profiles_raw} if profiles_raw else None

        # Ler locales
        locales: dict[str, dict] = {}
        locales_dir = framework_dir / "locales"
        if locales_dir.exists():
            for locale_file in locales_dir.glob("*.json"):
                locale_code = locale_file.stem  # ex: "pt", "en"
                try:
                    locales[locale_code] = json.loads(
                        locale_file.read_text(encoding="utf-8")
                    )
                except Exception:
                    logger.warning(
                        "Falha ao ler locale '%s' do framework '%s'.",
                        locale_code,
                        framework_dir.name,
                    )

        from app.frameworks.import_service import importar_framework

        stats = importar_framework(db, manifest, structure, profiles, locales)
        logger.info(
            "Framework '%s' v%s importado: %s",
            manifest.get("registry_id", framework_dir.name),
            manifest.get("version", "?"),
            stats,
        )
        return True

    except Exception:
        logger.exception(
            "Falha ao importar framework '%s'.",
            framework_dir.name,
        )
        return False


def seed_framework_se_necessario(db: Session) -> None:
    """
    Verifica se existe algum framework na DB.
    Se não existir, importa todos os frameworks disponíveis no diretório frameworks/.
    Chamado no lifespan da app, em qualquer DEPLOYMENT_MODE (idempotente —
    só semeia se a tabela estiver vazia).
    """
    count = db.exec(select(Framework).limit(1)).first()

    if count is not None:
        logger.debug("Framework já existe na DB — seed ignorado.")
        return

    if not _FRAMEWORKS_DIR.exists():
        logger.error(
            "Diretório frameworks/ não encontrado em %s. "
            "Não é possível fazer seed automático do framework.",
            _FRAMEWORKS_DIR,
        )
        return

    logger.info("Nenhum framework na DB — iniciando seed a partir de %s", _FRAMEWORKS_DIR)

    importados = 0
    for candidato in sorted(_FRAMEWORKS_DIR.iterdir()):
        if not candidato.is_dir():
            continue
        if (candidato / "framework.json").exists():
            if _carregar_framework_dir(db, candidato):
                importados += 1

    if importados == 0:
        logger.warning(
            "Nenhum framework importado. Verifique se o diretório frameworks/ "
            "contém subdirectórios com framework.json."
        )
    else:
        logger.info("Seed concluído: %d framework(s) importado(s).", importados)
