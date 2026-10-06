"""
A cancela do núcleo para os ficheiros que seguem para o sidecar.

Faz o que o núcleo já sabe fazer bem — limitar o tamanho, recusar o que
claramente não é um ficheiro de texto — e deixa a interpretação ao sidecar. É a
mesma para a importação e para o ficheiro do coletor do Active Directory.

Nota sobre validação de tipo: CSV e JSON **não têm assinatura binária**. Uma
lista de assinaturas "permitidas" não existe para eles, por isso o que se faz é
o inverso — recusar as assinaturas de formatos que não aceitamos (Excel, PDF,
comprimidos, executáveis) e exigir que o resto seja texto legível. Um ZIP nunca
chega ao analisador, e é isso que interessa.
"""
from __future__ import annotations

from fastapi import HTTPException, status

from app.config import get_settings

settings = get_settings()
MAX_BYTES = settings.IMPORT_MAX_SIZE_MB * 1024 * 1024

# Assinaturas de formatos que NÃO aceitamos. Recusar aqui, com nome, é o que
# permite ao wizard dizer "isto é um Excel — guarde como CSV" em vez de deixar o
# utilizador a olhar para um erro de leitura.
_ASSINATURAS_RECUSADAS: list[tuple[bytes, str]] = [
    (b"PK\x03\x04", "xlsx_ou_zip"),
    (b"%PDF", "pdf"),
    (b"\xd0\xcf\x11\xe0", "xls_antigo"),
    (b"\x7fELF", "binario"),
    (b"MZ", "binario"),
    (b"\x1f\x8b", "gzip"),
    (b"\x89PNG", "imagem"),
    (b"\xff\xd8\xff", "imagem"),
]


# O nome do 413 mudou nas versões recentes do Starlette; usa-se o atual quando
# existe para não semear avisos de depreciação no arranque.
_HTTP_413 = getattr(status, "HTTP_413_CONTENT_TOO_LARGE", 413)


def _recusar(codigo: str, http: int = status.HTTP_400_BAD_REQUEST, **extra):
    raise HTTPException(status_code=http, detail={"codigo": codigo, **extra})


def validar_conteudo(conteudo: bytes, content_type: str) -> None:
    """Cancela do core: tamanho, tipo declarado, assinatura e legibilidade."""
    if not conteudo:
        _recusar("ficheiro_vazio")
    if len(conteudo) > MAX_BYTES:
        _recusar(
            "ficheiro_grande",
            http=_HTTP_413,
            limite_mb=settings.IMPORT_MAX_SIZE_MB,
        )
    if (content_type or "") not in settings.IMPORT_ALLOWED_MIME_TYPES:
        _recusar("tipo_nao_permitido", tipo=content_type)

    for assinatura, nome in _ASSINATURAS_RECUSADAS:
        if conteudo.startswith(assinatura):
            _recusar("formato_nao_suportado", detetado=nome)

    # Bytes nulos no início não aparecem em texto — exceto em UTF-16, que tem
    # marca própria e é legítimo. Sem isto, qualquer binário sem assinatura
    # conhecida passava a cancela.
    cabeca = conteudo[:512]
    if b"\x00" in cabeca and not conteudo.startswith((b"\xff\xfe", b"\xfe\xff")):
        _recusar("formato_nao_suportado", detetado="binario")
