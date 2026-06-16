"""
Router do módulo de evidências.
"""
from __future__ import annotations

import uuid

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlmodel import Session

from app.database import get_session
from app.evidencias import service
from app.evidencias.schemas import (
    EvidenciaSchema,
    ListaEvidenciasSchema,
    ListaTodasEvidenciasSchema,
)
from app.auth.models import RoleUtilizador
from app.shared.dependencies import CurrentUserDep, get_empresa_ativa, require_role

router = APIRouter(tags=["Evidências"])


# ---------------------------------------------------------------------------
# GET /evidencias  (listagem global da empresa)
# ---------------------------------------------------------------------------

@router.get(
    "/evidencias",
    response_model=ListaTodasEvidenciasSchema,
    summary="Listar todas as evidências da empresa",
)
async def listar_todas_evidencias(
    utilizador: CurrentUserDep,
    incluir_texto: bool = False,
    db: Session = Depends(get_session),
):
    """Devolve todas as evidências activas da empresa, enriquecidas com código/domínio do controlo."""
    empresa = get_empresa_ativa(db, utilizador)
    return service.listar_todas_evidencias(
        db,
        empresa.id,
        utilizador,
        incluir_texto=incluir_texto,
    )



# ---------------------------------------------------------------------------
# GET /controlos/{controlo_empresa_id}/evidencias
# ---------------------------------------------------------------------------

@router.get(
    "/controlos/{controlo_empresa_id}/evidencias",
    response_model=ListaEvidenciasSchema,
    summary="Listar evidências de um controlo",
)
async def listar_evidencias(
    controlo_empresa_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """Lista todas as evidências activas de um controlo."""
    empresa = get_empresa_ativa(db, utilizador)
    return service.listar_evidencias(db, controlo_empresa_id, empresa.id, utilizador)


# ---------------------------------------------------------------------------
# POST /controlos/{controlo_empresa_id}/evidencias  (unificado)
# ---------------------------------------------------------------------------

@router.post(
    "/controlos/{controlo_empresa_id}/evidencias",
    response_model=EvidenciaSchema,
    status_code=status.HTTP_201_CREATED,
    summary="Adicionar evidência (texto, ficheiro ou ambos)",
)
async def criar_evidencia(
    controlo_empresa_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    titulo: str = Form(...),
    conteudo_texto: Optional[str] = Form(None),
    ficheiro: Optional[UploadFile] = File(None),
    db: Session = Depends(get_session),
):
    """
    Cria uma evidência para o controlo indicado.
    Modes suportados:
    - Nota de texto: preencher `conteudo_texto`
    - Ficheiro: enviar `ficheiro` (PDF, Word, Excel, PNG, JPEG; max 10 MB)
    - Ambos: preencher `conteudo_texto` E enviar `ficheiro`
    O campo `titulo` é obrigatório e identifica a evidência.
    """
    empresa = get_empresa_ativa(db, utilizador)
    return await service.criar_evidencia(
        db,
        controlo_empresa_id,
        empresa.id,
        titulo=titulo,
        conteudo_texto=conteudo_texto,
        ficheiro=ficheiro if (ficheiro and ficheiro.filename) else None,
        utilizador=utilizador,
        request=request,
    )


# ---------------------------------------------------------------------------
# GET /evidencias/{evidencia_id}
# ---------------------------------------------------------------------------

@router.get(
    "/evidencias/{evidencia_id}",
    response_model=EvidenciaSchema,
    summary="Obter evidência",
)
async def get_evidencia(
    evidencia_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """Devolve os metadados de uma evidência."""
    empresa = get_empresa_ativa(db, utilizador)
    return service.get_evidencia(db, evidencia_id, empresa.id, utilizador)


# ---------------------------------------------------------------------------
# GET /evidencias/{evidencia_id}/download
# ---------------------------------------------------------------------------

@router.get(
    "/evidencias/{evidencia_id}/download",
    summary="Download de ficheiro de evidência",
    response_class=FileResponse,
)
async def download_evidencia(
    evidencia_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """
    Serve o ficheiro de uma evidência para download.
    O ficheiro é servido directamente pelo servidor (não via URL pública).
    """
    empresa = get_empresa_ativa(db, utilizador)
    path, nome, mime, cifrado = service.get_evidencia_ficheiro_path(
        db, evidencia_id, empresa.id, utilizador
    )
    if cifrado:
        # Desencripta em memória e serve via StreamingResponse
        from app.config import get_settings
        from cryptography.fernet import Fernet
        import io
        fernet = Fernet(get_settings().EVIDENCE_ENCRYPTION_KEY.encode())
        with open(path, "rb") as f:
            dados_cifrados = f.read()
        dados_limpos = fernet.decrypt(dados_cifrados)
        return StreamingResponse(
            io.BytesIO(dados_limpos),
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{nome}"'},
        )
    return FileResponse(
        path=path,
        media_type=mime,
        filename=nome,
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


# ---------------------------------------------------------------------------
# DELETE /evidencias/{evidencia_id} (admin only)
# ---------------------------------------------------------------------------

@router.delete(
    "/evidencias/{evidencia_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar evidência (soft delete)",
    dependencies=[Depends(require_role(RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN))],
)
async def eliminar_evidencia(
    evidencia_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session),
):
    """
    Soft delete de uma evidência. Apenas administradores e sub-administradores.
    O ficheiro permanece no disco mas fica inacessível pela API.
    """
    empresa = get_empresa_ativa(db, utilizador)
    service.eliminar_evidencia(db, evidencia_id, empresa.id, utilizador, request)
