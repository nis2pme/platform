"""
Router do módulo de Documentos — templates de políticas para download.

Todos os utilizadores autenticados podem listar e descarregar templates.
Sem restrição de role — a informação é pública dentro da empresa.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse

from app.auth.models import Utilizador
from app.shared.dependencies import get_current_user
from app.shared.i18n import Msgs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documentos", tags=["Documentos"])

# Diretório raiz dos templates: backend/data/templates/
_TEMPLATES_ROOT = (Path(__file__).resolve().parents[2] / "data" / "templates").resolve()
_CATALOGO_FILE = _TEMPLATES_ROOT / "catalogo.json"
_LOCALES_DISPONIVEIS = frozenset({"pt", "en"})
_LOCALE_FALLBACK = "pt"


def _ler_catalogo() -> list[dict]:
    """Lê o catálogo de templates do disco. Devolve lista vazia se não existir."""
    if not _CATALOGO_FILE.exists():
        logger.error("Catálogo de templates não encontrado: %s", _CATALOGO_FILE)
        return []
    with _CATALOGO_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def _locale_efectivo(lang: str) -> str:
    """Devolve o locale pedido se disponível, com fallback para 'pt'."""
    return lang if lang in _LOCALES_DISPONIVEIS else _LOCALE_FALLBACK


def _resolver_caminho_seguro(item: dict, locale: str) -> Path | None:
    """
    Resolve o caminho absoluto do ficheiro para um template e locale.
    Inclui proteção contra path traversal (CWE-22): verifica que o caminho
    resolvido está dentro de _TEMPLATES_ROOT.
    """
    ficheiro_relativo = item.get("ficheiros", {}).get(locale)
    if not ficheiro_relativo:
        return None
    # Canonicalize e valida confinamento ao diretório root
    caminho = (_TEMPLATES_ROOT / ficheiro_relativo).resolve()
    try:
        caminho.relative_to(_TEMPLATES_ROOT)
    except ValueError:
        logger.warning(
            "Tentativa de path traversal detectada (doc_id=%s, locale=%s, caminho=%s)",
            item.get("id"),
            locale,
            ficheiro_relativo,
        )
        return None
    return caminho


# ---------------------------------------------------------------------------
# GET /documentos
# ---------------------------------------------------------------------------

@router.get(
    "",
    summary="Listar templates de documentos disponíveis",
)
async def listar_documentos(
    lang: str = Query(default="pt", max_length=10, description="Locale pretendido (pt/en)"),
    utilizador: Utilizador = Depends(get_current_user),
):
    """
    Devolve o catálogo de templates com indicação de disponibilidade no servidor.
    O campo `disponivel` indica se o ficheiro DOCX está presente em disco.
    """
    locale = _locale_efectivo(lang)
    catalogo = _ler_catalogo()

    resultado = []
    for item in catalogo:
        caminho = _resolver_caminho_seguro(item, locale)
        resultado.append({
            "id": item["id"],
            "titulo": item["titulo"].get(locale, item["titulo"].get(_LOCALE_FALLBACK, "")),
            "descricao": item["descricao"].get(locale, item["descricao"].get(_LOCALE_FALLBACK, "")),
            "disponivel": caminho is not None and caminho.exists(),
            "locale": locale,
        })
    return resultado


# ---------------------------------------------------------------------------
# GET /documentos/{doc_id}/download
# ---------------------------------------------------------------------------

_DOC_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


@router.get(
    "/{doc_id}/download",
    summary="Fazer download de um template de documento",
)
async def download_documento(
    doc_id: str,
    lang: str = Query(default="pt", max_length=10, description="Locale pretendido (pt/en)"),
    utilizador: Utilizador = Depends(get_current_user),
):
    """
    Serve o ficheiro DOCX do template pedido.
    - 404 se o ID não existe no catálogo.
    - 503 se o ficheiro não estiver disponível em disco.
    """
    # Validação de doc_id: apenas letras minúsculas, algarismos, _ e - (previne path traversal)
    if not all(c in _DOC_ID_CHARS for c in doc_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=Msgs.DOCUMENTO_NAO_ENCONTRADO,
        )

    locale = _locale_efectivo(lang)
    catalogo = _ler_catalogo()

    item = next((i for i in catalogo if i["id"] == doc_id), None)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=Msgs.DOCUMENTO_NAO_ENCONTRADO,
        )

    caminho = _resolver_caminho_seguro(item, locale)
    if caminho is None or not caminho.exists():
        logger.warning(
            "Ficheiro de template indisponível no disco (doc_id=%s, locale=%s)",
            doc_id,
            locale,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=Msgs.DOCUMENTO_FICHEIRO_INDISPONIVEL,
        )

    return FileResponse(
        path=str(caminho),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=caminho.name,
    )
