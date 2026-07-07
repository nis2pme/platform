"""Lógica de negócio do módulo de evidências V2-only."""

from __future__ import annotations

import io
import os
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request, UploadFile, status
from sqlalchemy import func
from sqlmodel import Session, select

from app.auth.models import RoleUtilizador, Utilizador
from app.config import get_settings
from app.empresas.models import Empresa
from app.shared.pii import cifrar_pii, decifrar_pii
from app.evidencias.models import Evidencia, TipoEvidencia
from app.evidencias.schemas import (
    EvidenciaComControloSchema,
    EvidenciaSchema,
    ListaEvidenciasSchema,
    ListaTodasEvidenciasSchema,
)
from app.frameworks.models import (
    ControlLocale,
    ControloEmpresaV2,
    DomainLocale,
    Framework,
)
from app.frameworks.runtime import (
    load_company_control_rows,
    load_preferred_locales,
    resolver_framework_empresa,
)
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.i18n import MsgsI18n, locale_de_request, traduzir
from app.shared.utils import resolver_locale

settings = get_settings()

_MAX_BYTES = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024

_MIB = 1024 * 1024


def _quota_bytes(tenant_id: str) -> int:
    """Quota de armazenamento da empresa, em bytes (0 = ilimitado).

    Base: settings.EVIDENCE_QUOTA_MB (env — fixa o valor do saas-trial). No SaaS
    com premium ligado, um entitlement 'evidence_storage' (limits.total_mb) por-plano
    sobrepõe-se. Qualquer falha na consulta cai no default do env — nunca bloqueia o
    upload por indisponibilidade do sidecar. No open-core (sem premium) usa só o env.
    """
    quota_mb = settings.EVIDENCE_QUOTA_MB
    try:
        from app.premium.client import get_premium_client

        client = get_premium_client()
        if client.enabled:
            ent = client.check_entitlement(tenant_id, "evidence_storage")
            total_mb = ent.limits.get("total_mb") if ent.enabled else None
            if total_mb:
                quota_mb = int(total_mb)
    except Exception:
        pass
    return max(0, quota_mb) * _MIB


# ---------------------------------------------------------------------------
# Cifra/decifra conteúdo de texto de evidências (Fernet)
# ---------------------------------------------------------------------------

def _cifrar_texto_evidencia(texto: str) -> str:
    """Cifra conteúdo de texto com Fernet (se EVIDENCE_ENCRYPTION_KEY configurada)."""
    from cryptography.fernet import Fernet

    fernet = Fernet(settings.EVIDENCE_ENCRYPTION_KEY.encode())
    return fernet.encrypt(texto.encode("utf-8")).decode("utf-8")


def _decifrar_texto_evidencia(cifrado: str) -> str:
    """Decifra conteúdo de texto cifrado com Fernet."""
    from cryptography.fernet import Fernet

    fernet = Fernet(settings.EVIDENCE_ENCRYPTION_KEY.encode())
    return fernet.decrypt(cifrado.encode("utf-8")).decode("utf-8")

# Mapeamento de magic bytes para MIME type (sem dependências externas).
# Cobre todos os tipos aceites em ALLOWED_UPLOAD_MIME_TYPES.
# Um ficheiro malicioso com Content-Type falsificado fica bloqueado aqui (CWE-434).
_MAGIC_SIGNATURES: list[tuple[bytes, str]] = [
    (b"%PDF",       "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a",     "image/gif"),
    (b"GIF89a",     "image/gif"),
    # ZIP = base de DOCX, XLSX, PPTX — distinguido depois pela extensão
    (b"PK\x03\x04", "application/zip"),
    # texto puro: sem magic bytes fixos — deixar passar se content-type for text/plain
]

# MIME types baseados em ZIP (Office Open XML): tratados como ZIP nos magic bytes
_ZIP_BASED_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/zip",
}

# Pasta de topo obrigatória em cada contentor OOXML (Open Packaging Conventions).
_OOXML_PASTA = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word/",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xl/",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "ppt/",
}


def _validar_zip_office(conteudo: bytes, claimed_mime: str) -> bool:
    """
    Confirma que um upload com assinatura ZIP é realmente do tipo declarado (CWE-434).

    Para os MIME OOXML exige a estrutura mínima: [Content_Types].xml na raiz + a
    pasta de topo correspondente (word/ | xl/ | ppt/). Para application/zip basta
    ser um ZIP válido.

    Lê apenas o índice central do ZIP (namelist) — NÃO descomprime — pelo que não é
    vulnerável a zip bombs; o servidor nunca expande o conteúdo do upload.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
            nomes = zf.namelist()
    except zipfile.BadZipFile:
        return False

    if claimed_mime == "application/zip":
        return True

    pasta = _OOXML_PASTA.get(claimed_mime)
    if pasta is None:
        return False
    return "[Content_Types].xml" in nomes and any(n.startswith(pasta) for n in nomes)


def _validar_magic_bytes(conteudo: bytes, claimed_mime: str) -> bool:
    """
    Verifica se os magic bytes do ficheiro são coerentes com o MIME type declarado.
    Retorna False se a assinatura não corresponder ao tipo reivindicado.
    text/plain não tem magic bytes fixos — aceite directamente se o header está correcto.
    """
    if claimed_mime == "text/plain":
        return True  # sem assinatura binária definível

    header = conteudo[:8]

    for signature, detected_mime in _MAGIC_SIGNATURES:
        if header.startswith(signature):
            # ZIP serve também para todos os formatos Office Open XML
            if detected_mime == "application/zip":
                return claimed_mime in _ZIP_BASED_MIMES
            return detected_mime == claimed_mime

    return False  # não reconhecido


def _get_empresa(db: Session, empresa_id: uuid.UUID) -> Empresa:
    empresa = db.get(Empresa, empresa_id)
    if not empresa:
        raise HTTPException(status_code=404, detail="Empresa não encontrada.")
    return empresa


def _ensure_framework(db: Session, empresa: Empresa) -> Framework:
    return resolver_framework_empresa(db, empresa)


def _get_ce(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    empresa_id: uuid.UUID,
) -> ControloEmpresaV2:
    ce = db.get(ControloEmpresaV2, controlo_empresa_id)
    if not ce or ce.empresa_id != empresa_id:
        raise HTTPException(status_code=404, detail="Controlo não encontrado.")
    return ce


def _verificar_acesso_leitura_evidencias(
    ce: ControloEmpresaV2,
    utilizador: Utilizador,
) -> None:
    if utilizador.role in (
        RoleUtilizador.ADMIN,
        RoleUtilizador.AUDITOR,
        RoleUtilizador.CEO,
    ):
        return
    if utilizador.role == RoleUtilizador.IMPLEMENTADOR and ce.implementador_id == utilizador.id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Sem acesso às evidências deste controlo.",
    )


def _verificar_acesso_escrita_evidencias(
    ce: ControloEmpresaV2,
    utilizador: Utilizador,
) -> None:
    if utilizador.role == RoleUtilizador.ADMIN:
        return
    if utilizador.role == RoleUtilizador.IMPLEMENTADOR and ce.implementador_id == utilizador.id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Sem permissão para adicionar evidências.",
    )


def _resumir_texto_evidencia(texto: str, limite: int = 140) -> str:
    texto_limpo = " ".join(texto.split())
    if len(texto_limpo) <= limite:
        return texto_limpo
    return f"{texto_limpo[: limite - 1].rstrip()}…"


def _schema_from_evidencia(
    ev: Evidencia,
    uploader_nome: str | None = None,
    include_text: bool = True,
    include_summary: bool = False,
) -> EvidenciaSchema:
    texto = None
    resumo = None

    if ev.conteudo_texto and (include_text or include_summary):
        texto_descifrado = ev.conteudo_texto
        if ev.conteudo_texto_cifrado and settings.EVIDENCE_ENCRYPTION_KEY:
            texto_descifrado = _decifrar_texto_evidencia(texto_descifrado)

        if include_text:
            texto = texto_descifrado
        if include_summary:
            resumo = _resumir_texto_evidencia(texto_descifrado)

    return EvidenciaSchema(
        id=ev.id,
        controlo_empresa_id=ev.controlo_empresa_v2_id,
        empresa_id=ev.empresa_id,
        tipo=ev.tipo,
        titulo=ev.titulo,
        conteudo_texto=texto,
        conteudo_resumo=resumo,
        ficheiro_nome=decifrar_pii(ev.ficheiro_nome),
        ficheiro_tipo=ev.ficheiro_tipo,
        ficheiro_tamanho=ev.ficheiro_tamanho,
        uploaded_by_id=ev.uploaded_by_id,
        uploaded_by_nome=uploader_nome,
        created_at=ev.created_at,
        deleted_at=ev.deleted_at,
    )


def listar_evidencias(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
) -> ListaEvidenciasSchema:
    ce = _get_ce(db, controlo_empresa_id, empresa_id)
    _verificar_acesso_leitura_evidencias(ce, utilizador)

    evidencias = db.exec(
        select(Evidencia)
        .where(
            Evidencia.controlo_empresa_v2_id == controlo_empresa_id,
            Evidencia.empresa_id == empresa_id,
            Evidencia.deleted_at.is_(None),
        )
        .order_by(Evidencia.created_at.desc())
    ).all()
    uploader_ids = [ev.uploaded_by_id for ev in evidencias]
    uploaders = (
        {
            utilizador_item.id: decifrar_pii(utilizador_item.nome)
            for utilizador_item in db.exec(
                select(Utilizador).where(Utilizador.id.in_(uploader_ids))
            ).all()
        }
        if uploader_ids
        else {}
    )

    return ListaEvidenciasSchema(
        total=len(evidencias),
        evidencias=[
            _schema_from_evidencia(ev, uploaders.get(ev.uploaded_by_id))
            for ev in evidencias
        ],
    )


def listar_todas_evidencias(
    db: Session,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
    incluir_texto: bool = False,
) -> ListaTodasEvidenciasSchema:
    empresa = _get_empresa(db, empresa_id)
    framework = _ensure_framework(db, empresa)
    locale = resolver_locale(empresa, framework)

    evidencias = db.exec(
        select(Evidencia)
        .where(
            Evidencia.empresa_id == empresa_id,
            Evidencia.deleted_at.is_(None),
            Evidencia.controlo_empresa_v2_id.is_not(None),
        )
        .order_by(Evidencia.created_at.desc())
    ).all()

    if not evidencias:
        return ListaTodasEvidenciasSchema(total=0, evidencias=[])

    rows = load_company_control_rows(db, empresa_id, framework.id)
    rows_map = {row.ce.id: row for row in rows}

    evidencias_com_contexto = []
    for evidencia in evidencias:
        ce_id = evidencia.controlo_empresa_v2_id
        if ce_id is None:
            continue
        row = rows_map.get(ce_id)
        if not row:
            continue
        if (
            utilizador.role == RoleUtilizador.IMPLEMENTADOR
            and row.ce.implementador_id != utilizador.id
        ):
            continue
        evidencias_com_contexto.append((evidencia, row))

    if not evidencias_com_contexto:
        return ListaTodasEvidenciasSchema(total=0, evidencias=[])

    control_locales = load_preferred_locales(
        db,
        ControlLocale,
        "control_id",
        {row.control.id for _, row in evidencias_com_contexto},
        locale,
        framework.default_locale,
    )
    domain_locales = load_preferred_locales(
        db,
        DomainLocale,
        "domain_id",
        {row.domain.id for _, row in evidencias_com_contexto},
        locale,
        framework.default_locale,
    )
    uploader_ids = [ev.uploaded_by_id for ev, _ in evidencias_com_contexto]
    uploaders = {
        utilizador_item.id: decifrar_pii(utilizador_item.nome)
        for utilizador_item in db.exec(
            select(Utilizador).where(Utilizador.id.in_(uploader_ids))
        ).all()
    }

    resultado = [
        EvidenciaComControloSchema(
            **_schema_from_evidencia(
                evidencia,
                uploaders.get(evidencia.uploaded_by_id),
                include_text=incluir_texto,
                include_summary=(
                    not incluir_texto and not bool(evidencia.titulo)
                ),
            ).model_dump(),
            controlo_codigo=row.control.code,
            controlo_titulo=(
                control_locales[row.control.id].title
                if row.control.id in control_locales
                else row.control.code
            ),
            controlo_estado=row.ce.estado.value,
            dominio_codigo=row.domain.code,
            dominio_nome=(
                domain_locales[row.domain.id].name
                if row.domain.id in domain_locales
                else row.domain.code
            ),
        )
        for evidencia, row in evidencias_com_contexto
    ]

    return ListaTodasEvidenciasSchema(total=len(resultado), evidencias=resultado)


async def criar_evidencia(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    empresa_id: uuid.UUID,
    titulo: str | None,
    conteudo_texto: str | None,
    ficheiro: UploadFile | None,
    utilizador: Utilizador,
    request: Request | None = None,
) -> EvidenciaSchema:
    ce = _get_ce(db, controlo_empresa_id, empresa_id)
    _verificar_acesso_escrita_evidencias(ce, utilizador)

    titulo_limpo = titulo.strip() if titulo else None
    if not titulo_limpo:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="O título da evidência é obrigatório.",
        )

    texto_limpo = conteudo_texto.strip() if conteudo_texto else None
    tem_texto = bool(texto_limpo)
    tem_ficheiro = bool(ficheiro and getattr(ficheiro, "filename", None))
    if not tem_texto and not tem_ficheiro:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A evidência deve ter pelo menos uma nota de texto ou um ficheiro.",
        )

    if tem_texto and tem_ficheiro:
        tipo = TipoEvidencia.AMBOS
    elif tem_ficheiro:
        tipo = TipoEvidencia.FICHEIRO
    else:
        tipo = TipoEvidencia.TEXTO

    ficheiro_path = None
    ficheiro_nome = None
    ficheiro_tipo_mime = None
    ficheiro_tamanho = None
    ficheiro_cifrado = False

    if tem_ficheiro and ficheiro is not None:
        content_type = ficheiro.content_type or ""
        if content_type not in settings.ALLOWED_UPLOAD_MIME_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Tipo de ficheiro não permitido ({content_type}). "
                    f"Tipos aceites: PDF, imagens, documentos Office, texto."
                ),
            )

        conteudo_bytes = await ficheiro.read()
        if len(conteudo_bytes) > _MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"Ficheiro demasiado grande. Máximo: {settings.MAX_UPLOAD_SIZE_MB} MB."
                ),
            )
        if len(conteudo_bytes) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="O ficheiro não pode estar vazio.",
            )

        # Quota de armazenamento por empresa (0 = ilimitado). Verificada ANTES de
        # gravar. Uso = soma dos tamanhos lógicos das evidências não-eliminadas.
        quota = _quota_bytes(str(empresa_id))
        if quota:
            usado: int = db.exec(
                select(func.coalesce(func.sum(Evidencia.ficheiro_tamanho), 0)).where(
                    Evidencia.empresa_id == empresa_id,
                    Evidencia.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            ).one()
            if usado + len(conteudo_bytes) > quota:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=traduzir(
                        MsgsI18n.STORAGE_QUOTA_EXCEDIDA,
                        locale_de_request(request),
                        usado=usado // _MIB,
                        total=quota // _MIB,
                    ),
                )

        # Validar magic bytes reais do conteúdo — impede Content-Type falsificado (CWE-434)
        if not _validar_magic_bytes(conteudo_bytes, content_type):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "O conteúdo do ficheiro não corresponde ao tipo declarado. "
                    "Verifique se o ficheiro não está corrompido ou adulterado."
                ),
            )

        # Para contentores ZIP/Office, confirmar a estrutura interna real (CWE-434):
        # um ZIP arbitrário renomeado para .docx/.xlsx/.pptx é rejeitado aqui.
        if content_type in _ZIP_BASED_MIMES and not _validar_zip_office(
            conteudo_bytes, content_type
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="O ficheiro não tem a estrutura interna esperada para o tipo declarado.",
            )

        nome_original = Path(ficheiro.filename or "ficheiro").name
        nome_original = "".join(
            c for c in nome_original if c.isalnum() or c in (".", "-", "_", " ")
        ).strip() or "evidencia"

        destino_dir = (
            Path(settings.UPLOADS_DIR)
            / str(empresa_id)
            / str(controlo_empresa_id)
        )
        destino_dir.mkdir(parents=True, exist_ok=True)
        destino_path = destino_dir / f"{uuid.uuid4()}_{nome_original}"

        # Defence-in-depth: garantir que o caminho resolvido está dentro de UPLOADS_DIR.
        # Previne path traversal residual se UPLOADS_DIR for relativo e o cwd mudar (CWE-22).
        _uploads_root = Path(settings.UPLOADS_DIR).resolve()
        if not destino_path.resolve().is_relative_to(_uploads_root):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Caminho de destino inválido.",
            )

        bytes_a_escrever = conteudo_bytes
        if settings.EVIDENCE_ENCRYPTION_KEY:
            from cryptography.fernet import Fernet

            fernet = Fernet(settings.EVIDENCE_ENCRYPTION_KEY.encode())
            bytes_a_escrever = fernet.encrypt(conteudo_bytes)
            ficheiro_cifrado = True

        try:
            with open(destino_path, "wb") as file_handle:
                file_handle.write(bytes_a_escrever)
        except OSError as exc:
            # Disco cheio (ENOSPC) ou falha de escrita: limpa o ficheiro parcial e
            # devolve um erro claro (507) em vez de rebentar num 500 genérico.
            try:
                destino_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise HTTPException(
                status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                detail=traduzir(MsgsI18n.DISCO_SEM_ESPACO, locale_de_request(request)),
            ) from exc

        ficheiro_path = str(destino_path)
        ficheiro_nome = cifrar_pii(nome_original)
        ficheiro_tipo_mime = content_type
        ficheiro_tamanho = len(conteudo_bytes)

    # Cifrar conteúdo de texto em repouso (se chave configurada)
    texto_a_guardar = texto_limpo
    conteudo_texto_cifrado = False
    if texto_limpo and settings.EVIDENCE_ENCRYPTION_KEY:
        texto_a_guardar = _cifrar_texto_evidencia(texto_limpo)
        conteudo_texto_cifrado = True

    evidencia = Evidencia(
        controlo_empresa_v2_id=controlo_empresa_id,
        empresa_id=empresa_id,
        tipo=tipo,
        titulo=titulo_limpo,
        conteudo_texto=texto_a_guardar,
        ficheiro_path=ficheiro_path,
        ficheiro_nome=ficheiro_nome,
        ficheiro_tipo=ficheiro_tipo_mime,
        ficheiro_tamanho=ficheiro_tamanho,
        ficheiro_cifrado=ficheiro_cifrado,
        conteudo_texto_cifrado=conteudo_texto_cifrado,
        uploaded_by_id=utilizador.id,
    )
    db.add(evidencia)
    db.flush()

    registar_acao(
        db,
        acao=Acao.EVIDENCIA_UPLOAD,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Evidencia",
        entidade_id=evidencia.id,
        dados_novos={
            "tipo": tipo.value,
            "titulo": evidencia.titulo,
            "controlo_empresa_id": str(controlo_empresa_id),
            "tem_ficheiro": tem_ficheiro,
            "tem_texto": tem_texto,
        },
        request=request,
    )
    db.flush()
    db.refresh(evidencia)
    return _schema_from_evidencia(evidencia, decifrar_pii(utilizador.nome))


def _get_evidencia_or_404(
    db: Session,
    evidencia_id: uuid.UUID,
    empresa_id: uuid.UUID,
) -> Evidencia:
    evidencia = db.exec(
        select(Evidencia).where(
            Evidencia.id == evidencia_id,
            Evidencia.empresa_id == empresa_id,
            Evidencia.deleted_at.is_(None),
        )
    ).first()
    if not evidencia:
        raise HTTPException(status_code=404, detail="Evidência não encontrada.")
    return evidencia


def _verificar_acesso_evidencia(
    db: Session,
    evidencia: Evidencia,
    utilizador: Utilizador,
) -> ControloEmpresaV2:
    if evidencia.controlo_empresa_v2_id is None:
        raise HTTPException(status_code=404, detail="Controlo não encontrado.")

    ce = db.get(ControloEmpresaV2, evidencia.controlo_empresa_v2_id)
    if not ce:
        raise HTTPException(status_code=404, detail="Controlo não encontrado.")

    _verificar_acesso_leitura_evidencias(ce, utilizador)
    return ce


def get_evidencia(
    db: Session,
    evidencia_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
) -> EvidenciaSchema:
    evidencia = _get_evidencia_or_404(db, evidencia_id, empresa_id)
    _verificar_acesso_evidencia(db, evidencia, utilizador)
    uploader = db.get(Utilizador, evidencia.uploaded_by_id)
    return _schema_from_evidencia(
        evidencia,
        decifrar_pii(uploader.nome) if uploader else None,
    )


def get_evidencia_ficheiro_path(
    db: Session,
    evidencia_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
) -> tuple[str, str, str, bool]:
    evidencia = _get_evidencia_or_404(db, evidencia_id, empresa_id)
    _verificar_acesso_evidencia(db, evidencia, utilizador)

    if evidencia.tipo not in (TipoEvidencia.FICHEIRO, TipoEvidencia.AMBOS) or not evidencia.ficheiro_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Esta evidência não é um ficheiro.",
        )

    if not os.path.isfile(evidencia.ficheiro_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ficheiro não encontrado no servidor.",
        )

    return (
        evidencia.ficheiro_path,
        decifrar_pii(evidencia.ficheiro_nome) or "evidencia",
        evidencia.ficheiro_tipo or "application/octet-stream",
        bool(evidencia.ficheiro_cifrado),
    )


def eliminar_evidencia(
    db: Session,
    evidencia_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
    request: Request | None = None,
) -> None:
    if utilizador.role not in (RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas administradores e sub-administradores podem eliminar evidências.",
        )

    evidencia = _get_evidencia_or_404(db, evidencia_id, empresa_id)
    evidencia.deleted_at = datetime.now(timezone.utc)
    db.add(evidencia)

    # Liberta o disco: o soft-delete mantém a linha (auditoria/retenção) mas o
    # ficheiro físico é removido — deixa de contar na quota e não enche o volume.
    if evidencia.ficheiro_path:
        try:
            os.remove(evidencia.ficheiro_path)
        except OSError:
            pass

    registar_acao(
        db,
        acao=Acao.EVIDENCIA_ELIMINADA,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Evidencia",
        entidade_id=evidencia.id,
        dados_novos={
            "tipo": evidencia.tipo.value,
            "titulo": evidencia.titulo,
            "controlo_empresa_id": str(evidencia.controlo_empresa_v2_id),
        },
        request=request,
    )
    db.commit()
