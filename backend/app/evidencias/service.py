"""Lógica de negócio do módulo de evidências V2-only."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from fastapi import HTTPException, Request, UploadFile, status
from sqlalchemy import func, text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from app.auth.models import RoleUtilizador, Utilizador
from app.config import get_settings
from app.empresas.models import Empresa
from app.shared.pii import cifrar_pii, decifrar_pii
from app.evidencias import apagamentos, ligacoes, reciclagem, retencao
from app.evidencias.models import Evidencia, EvidenciaRequisito, TipoEvidencia
from app.evidencias.schemas import (
    EvidenciaComControloSchema,
    EvidenciaSchema,
    LigacaoSchema,
    ListaEvidenciasSchema,
    ListaLigacoesSchema,
    ListaOrfasSchema,
    ListaTodasEvidenciasSchema,
    ListaVersoesSchema,
    NaoRestauradoSchema,
    OrfaSchema,
    OrigemSchema,
    RecusaApagamentoSchema,
    RestauroSchema,
    ResultadoApagamentoSchema,
    VersaoSchema,
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
from app.shared import politica_seguranca
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.capacidades import Ambito, ClasseAcao, ambito_de, exigir_ambito
from app.shared.enums import EstadoControlo
from app.shared.hashes import selo_documento
from app.shared.i18n import MsgsI18n, locale_de_request, traduzir
from app.shared.utils import resolver_locale

logger = logging.getLogger(__name__)

# Quanto se espera pela tranca de deduplicação antes de desistir dela. Curto de
# propósito: a secção que ela protege é uma consulta, um `rename` e os INSERT.
# Uma espera desta ordem só acontece se a tranca estiver noutro processo, e
# desistir é melhor do que reter o pedido do utilizador — a única coisa que se
# perde é a serialização, e o que volta é a cópia duplicada rara.
_DEDUP_ESPERA_MS = 3000

settings = get_settings()

_MAX_BYTES = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024

_MIB = 1024 * 1024
_TITULO_MAX = 255  # o tamanho da coluna `evidencias.titulo`


def _quota_bytes(tenant_id: str) -> int:
    """Quota de armazenamento da empresa, em bytes (0 = ilimitado).

    Base: settings.EVIDENCE_QUOTA_MB (env — fixa o valor do saas-trial). No SaaS
    com premium ligado, um entitlement 'evidence_storage' (limits.total_mb) por-plano
    sobrepõe-se. Qualquer falha na consulta cai no default do env — nunca bloqueia o
    upload por indisponibilidade do sidecar. No open-core (sem premium) usa só o env.
    """
    quota_mb = settings.EVIDENCE_QUOTA_MB
    if settings.DEPLOYMENT_MODE == "saas" and quota_mb <= 0:
        quota_mb = settings.EVIDENCE_QUOTA_MB_SAAS_OMISSAO
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

class EvidenciaIlegivel(Exception):
    """O conteúdo guardado não abre com a EVIDENCE_ENCRYPTION_KEY desta instalação."""


MSG_EVIDENCIA_ILEGIVEL = (
    "O ficheiro desta evidência não abre com a chave de cifra desta instalação. "
    "Contacte o administrador."
)


def _cifra_evidencias():
    from app.shared.chaves import fernet_obrigatorio

    return fernet_obrigatorio("EVIDENCE_ENCRYPTION_KEY")


def cifrar_bytes_evidencia(dados: bytes) -> bytes:
    """Cifra o conteúdo de um ficheiro de evidência (Fernet, chave atual)."""
    return _cifra_evidencias().encrypt(dados)


def decifrar_bytes_evidencia(dados: bytes) -> bytes:
    """Decifra um ficheiro de evidência; com a chave anterior durante uma rotação.

    Levanta `EvidenciaIlegivel` quando não abre — chave de outra instalação ou
    ficheiro corrompido — para quem chama decidir o que mostrar.
    """
    from cryptography.fernet import InvalidToken

    try:
        return _cifra_evidencias().decrypt(dados)
    except InvalidToken as erro:
        raise EvidenciaIlegivel() from erro


def _cifrar_texto_evidencia(texto: str) -> str:
    """Cifra conteúdo de texto com Fernet (se EVIDENCE_ENCRYPTION_KEY configurada)."""
    return cifrar_bytes_evidencia(texto.encode("utf-8")).decode("utf-8")


def _decifrar_texto_evidencia(cifrado: str) -> str:
    """Decifra conteúdo de texto cifrado com Fernet.

    Um texto que não abre sai vazio, com o erro no log — como a PII: uma linha
    ilegível não pode deitar abaixo a listagem inteira que a contém.
    """
    try:
        return decifrar_bytes_evidencia(cifrado.encode("utf-8")).decode("utf-8")
    except EvidenciaIlegivel:
        logger.error(
            "Texto de evidência não decifra com a EVIDENCE_ENCRYPTION_KEY desta "
            "instalação (chave trocada ou dados corrompidos)."
        )
        return ""

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


def _hash_conteudo(texto: str | None, ficheiro_bytes: bytes | None) -> str | None:
    """Impressão digital SHA-256 do conteúdo em claro de uma evidência.

    Calculada sobre o texto e/ou os bytes do ficheiro ANTES de qualquer cifra — o
    Fernet usa um IV aleatório, logo o texto cifrado nunca serviria para comparar.
    Duas evidências com o mesmo conteúdo dão o mesmo hash; é isso que permite
    detetar repetições. Devolve None se não houver conteúdo (nunca acontece na
    prática — o chamador já garante texto ou ficheiro).
    """
    if not texto and ficheiro_bytes is None:
        return None
    h = hashlib.sha256()
    if texto:
        h.update(texto.encode("utf-8"))
    h.update(b"\x00")  # separa texto de ficheiro (evita colisões por concatenação)
    if ficheiro_bytes is not None:
        h.update(ficheiro_bytes)
    return h.hexdigest()


def _duplicado_ativo(
    db: Session,
    empresa_id: uuid.UUID,
    controlo_empresa_id: uuid.UUID,
    conteudo_hash: str,
) -> Evidencia | None:
    """Evidência ativa com o mesmo conteúdo **na empresa** (ou None).

    O âmbito da comparação passou de controlo para empresa com o N:N.
    Com uma evidência a poder servir vários controlos, comparar só dentro do
    controlo faria o segundo upload do mesmo ficheiro parecer novo — e o produto
    guardaria duas cópias do mesmo PDF, cobrando a quota duas vezes, quando a
    resposta certa é *"já tens este ficheiro; queres ligá-lo também a este
    controlo?"*.

    Uma cópia em disco, várias ligações, quota consumida uma vez.
    """
    return db.exec(
        select(Evidencia).where(
            Evidencia.empresa_id == empresa_id,
            Evidencia.conteudo_hash == conteudo_hash,
            Evidencia.deleted_at.is_(None),
        )
    ).first()


def _duplicado_mais_recente(
    db: Session,
    empresa_id: uuid.UUID,
    controlo_empresa_id: uuid.UUID,
    conteudo_hash: str,
) -> Evidencia | None:
    """A evidência MAIS RECENTE do controlo, se tiver o mesmo conteúdo (senão None).

    Usado no fluxo automático "anexar como evidência": só é no-op quando nada mudou
    desde a última versão. Voltar a um conteúdo antigo conta como versão nova
    (o histórico preserva-se; só repetições consecutivas são evitadas)."""
    # Continua a ser POR CONTROLO, ao contrário do `_duplicado_ativo`: aqui a
    # pergunta é "mudou alguma coisa neste controlo desde a última vez?", e a
    # última evidência de outro controlo não responde a isso.
    ligadas = ligacoes.evidencias_de(
        db, requisito_id=controlo_empresa_id, empresa_id=empresa_id
    )
    ultima = ligadas[0] if ligadas else None
    if ultima is not None and ultima.conteudo_hash == conteudo_hash:
        return ultima
    return None


def _trancar_dedup(db: Session, empresa_id: uuid.UUID, controlo_empresa_id: uuid.UUID) -> None:
    """Serializa a decisão de duplicado deste controlo até ao fim da transação.

    A tranca é por `(empresa, controlo)`: dois controlos diferentes nunca se
    esperam. É de TRANSAÇÃO por necessidade, não por conveniência — a linha só
    fica visível ao pedido concorrente depois do commit, por isso libertá-la
    antes disso deixaria o segundo a ler uma base onde a primeira ainda não
    existe, que é exatamente a corrida que se quer fechar.

    O que torna isto seguro é **onde** é chamada: depois de o ficheiro já estar
    escrito num caminho temporário. Dentro da tranca só ficam uma consulta, um
    `rename` e os INSERT. Uma versão anterior tomava-a antes da escrita, ficava
    retida durante a cifra e o I/O do ficheiro, e cada pedido em espera segurava
    uma ligação do pool — com a aplicação inteira a parar.

    Em SQLite não há tranca (não existem locks consultivos) e esta função não
    faz nada: os testes de unidade não provam serialização nenhuma. A prova é
    contra Postgres.

    **Quem chama isto tem de correr fora do event loop.** A tranca só se larga no
    commit, e o commit da sessão acontece depois de o pedido devolver — quem
    espera fica retido todo esse tempo. Num handler `async def` essa espera é no
    próprio loop: bloqueia o processo inteiro, e quem tem a tranca deixa de poder
    chegar ao commit que a largaria. Por isso os endpoints deste caminho são
    `def` e o FastAPI corre-os no threadpool, como já acontece com o bloqueio da
    cadeia de auditoria, que é pego dentro do commit.

    O limite de espera é a segunda rede, para o caso de a tranca estar noutro
    processo que ficou preso: ao esgotar-se, desfaz-se só a tentativa e segue-se
    sem tranca. O pior caso volta a ser a cópia duplicada rara — nunca uma
    paragem, que é o mau desfecho que não se pode admitir.
    """
    try:
        dialeto = db.get_bind().dialect.name
    except Exception:  # noqa: BLE001 — sessão sem bind (testes com duplos)
        return
    if dialeto != "postgresql":
        return
    # Dois inteiros de 32 bits em vez de um de 64: mantém as duas dimensões
    # separadas no espaço de chaves, e é a forma canónica de trancar um par.
    chave_empresa = int.from_bytes(empresa_id.bytes[:4], "big", signed=True)
    chave_controlo = int.from_bytes(controlo_empresa_id.bytes[:4], "big", signed=True)
    try:
        # O savepoint é o que torna a desistência barata: no Postgres um tempo de
        # bloqueio esgotado aborta a transação, e essa transação é o trabalho que
        # o utilizador pediu. Com savepoint desfaz-se a tentativa e mais nada.
        with db.begin_nested():
            db.execute(
                text(f"SET LOCAL lock_timeout = '{int(_DEDUP_ESPERA_MS)}ms'")
            )
            db.execute(
                text("SELECT pg_advisory_xact_lock(:a, :b)"),
                {"a": chave_empresa, "b": chave_controlo},
            )
        # O `SET LOCAL` sobrevive ao fecho do savepoint e valeria até ao fim da
        # transação — passando a impor este limite ao bloqueio da cadeia de
        # auditoria, que é tomado mais à frente no mesmo pedido. Ela sabe
        # desistir, mas mudar-lhe o comportamento por efeito lateral daqui seria
        # decidir por ela. Reposto: o limite é só desta tranca.
        db.execute(text("SET LOCAL lock_timeout = DEFAULT"))
    except OperationalError:
        logger.warning(
            "Tranca de deduplicação não obtida em %sms (empresa=%s, controlo=%s); "
            "a criação segue sem serialização.",
            _DEDUP_ESPERA_MS,
            empresa_id,
            controlo_empresa_id,
        )


class _Duplicado(NamedTuple):
    """Resultado da decisão de duplicado.

    `resposta` preenchida = não se cria nada, devolve-se aquilo tal e qual.
    `resposta` a None = seguir para a criação, com `deliberado` a dizer se a
    cópia é uma decisão consciente do utilizador.
    """

    resposta: EvidenciaSchema | None
    deliberado: bool


def _resolver_duplicado(
    db: Session,
    *,
    empresa_id: uuid.UUID,
    controlo_empresa_id: uuid.UUID,
    conteudo_hash: str | None,
    evitar_duplicado: bool,
    ligar_existente: bool | None,
    utilizador: Utilizador,
    request: Request | None,
) -> _Duplicado:
    """Decide o que fazer perante conteúdo repetido.

    Vive numa função própria porque é chamada DUAS vezes: uma sem tranca, para
    evitar escrever no disco no caso comum, e outra sob tranca, que é a que
    garante. As duas TÊM de responder o mesmo perante a mesma base — se
    divergissem, o desfecho de um pedido passaria a depender de ter havido
    concorrência ou não.
    """
    if not conteudo_hash:
        return _Duplicado(None, False)

    if evitar_duplicado:
        # Fluxo automático ("anexar como evidência"): só é no-op quando nada
        # mudou desde a ÚLTIMA versão — devolve-a sem criar cópia nem ficheiro.
        existente = _duplicado_mais_recente(db, empresa_id, controlo_empresa_id, conteudo_hash)
        if existente is not None:
            schema = _schema_from_evidencia(existente, decifrar_pii(utilizador.nome))
            schema.duplicado = True
            schema.criado = False
            return _Duplicado(schema, False)
        return _Duplicado(None, False)

    existente = _duplicado_ativo(db, empresa_id, controlo_empresa_id, conteudo_hash)
    if existente is None:
        return _Duplicado(None, False)

    # A comparação é por empresa, e é isso que cruza donos: a existente pode ser
    # de um controlo que quem envia não alcança. Ligá-la é dar-lhe acesso a ela —
    # pede a mesma porta que ligar uma evidência pelo ecrã (e a recusa fica na
    # trilha). Sem pedir para ligar, quem não a alcança nem sabe que existe: o
    # 409 diria o título dela, e a marca de cópia diria que há outra. Guarda a
    # sua, como se não houvesse repetição.
    if ligar_existente:
        _verificar_acesso_ou_orfa(db, existente, utilizador)
    elif not _alcanca_evidencia(db, existente, utilizador):
        return _Duplicado(None, False)

    # Upload manual do mesmo conteúdo que já existe NA EMPRESA.
    #
    # Com uma prova a poder sustentar vários controlos, a resposta certa deixou
    # de ser "guarda outra cópia": é perguntar. Guardar em silêncio gastava disco
    # e quota duas vezes pelo mesmo ficheiro, e deixava o cliente com duas
    # linhagens do mesmo documento para manter.
    #
    # Três caminhos, e o utilizador escolhe:
    #   ligar_existente=None  → 409, o ecrã pergunta (caso normal)
    #   ligar_existente=True  → liga a esta e não cria nada
    #   ligar_existente=False → cria mesmo a segunda cópia, assinalada
    if ligar_existente is None:
        ja_ligada = ligacoes.ligacao_ativa(
            db, evidencia_id=existente.id, requisito_id=controlo_empresa_id
        ) is not None
        catalogo = (
            MsgsI18n.EVIDENCIA_JA_LIGADA if ja_ligada
            else MsgsI18n.EVIDENCIA_JA_EXISTE
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": "evidencia_duplicada",
                "evidencia_id": str(existente.id),
                "titulo": existente.titulo,
                "ja_ligada": ja_ligada,
                "mensagem": traduzir(
                    catalogo,
                    locale_de_request(request),
                    titulo=existente.titulo or "",
                ),
            },
        )

    if ligar_existente:
        ligacoes.ligar(
            db,
            evidencia_id=existente.id,
            requisito_id=controlo_empresa_id,
            empresa_id=empresa_id,
            ligado_por_id=utilizador.id,
        )
        registar_acao(
            db,
            acao=Acao.EVIDENCIA_LIGADA,
            resultado=ResultadoAcao.SUCESSO,
            empresa_id=empresa_id,
            utilizador_id=utilizador.id,
            entidade_tipo="Evidencia",
            entidade_id=existente.id,
            dados_novos={
                "conteudo_hash": existente.conteudo_hash,
                "controlo_empresa_id": str(controlo_empresa_id),
                "por_conteudo_repetido": True,
            },
            request=request,
        )
        db.commit()
        uploader = db.get(Utilizador, existente.uploaded_by_id)
        schema = _schema_from_evidencia(
            existente,
            decifrar_pii(uploader.nome) if uploader else None,
            controlo_empresa_id=controlo_empresa_id,
        )
        schema.duplicado = True
        schema.criado = False
        return _Duplicado(schema, False)

    # Cópia deliberada: cria, mas assinala.
    return _Duplicado(None, True)


def _mensagem_selo(empresa_id, controlo_empresa_id, conteudo_hash: str) -> str:
    return f"{empresa_id}:{controlo_empresa_id}:{conteudo_hash}"


def selar_hash_documento(empresa_id, controlo_empresa_id, conteudo_hash: str) -> str:
    """A impressão estável de um documento gerado pelo núcleo, selada para voltar
    no upload do «anexar como evidência».

    O documento é gerado aqui mas desenhado em PDF no browser, com a data lá
    dentro: os bytes mudam a cada geração, e a deduplicação usa esta impressão
    do conteúdo em vez da dos bytes. O browser devolve-a tal e qual. O selo
    (HMAC do núcleo, preso à empresa e ao controlo) é o que impede um cliente de
    mandar a impressão que quiser — a de uma evidência alheia, por exemplo."""
    selo = selo_documento(_mensagem_selo(empresa_id, controlo_empresa_id, conteudo_hash))
    return f"{conteudo_hash}.{selo}" if selo else conteudo_hash


def _hash_ext_valido(
    valor: str | None, empresa_id: uuid.UUID, controlo_empresa_id: uuid.UUID
) -> str | None:
    """A impressão vinda do cliente, só se trouxer o selo do núcleo para esta
    empresa e este controlo (ver `selar_hash_documento`). Senão None, e a
    impressão calcula-se dos bytes: um hash que o cliente escolhe ficava gravado
    como impressão da evidência, e era com ele que a deduplicação ligava — ou a
    lápide arrastava — evidências de outras pessoas."""
    if not valor:
        return None
    conteudo_hash, _, selo = valor.strip().lower().partition(".")
    if len(conteudo_hash) != 64 or not all(c in "0123456789abcdef" for c in conteudo_hash):
        return None
    esperado = selo_documento(_mensagem_selo(empresa_id, controlo_empresa_id, conteudo_hash))
    if not selo or esperado is None or not hmac.compare_digest(selo, esperado):
        logger.warning(
            "Impressão do cliente sem selo válido (empresa=%s, controlo=%s): "
            "calculada a partir do conteúdo.", empresa_id, controlo_empresa_id,
        )
        return None
    return conteudo_hash


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
    """As evidências seguem o dono do controlo a que pertencem."""
    exigir_ambito(utilizador, "evidencias", ClasseAcao.VER, ce.implementador_id)


def _verificar_acesso_escrita_evidencias(
    ce: ControloEmpresaV2,
    utilizador: Utilizador,
) -> None:
    exigir_ambito(utilizador, "evidencias", ClasseAcao.OPERAR, ce.implementador_id)


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
    controlo_empresa_id: uuid.UUID | None = None,
) -> EvidenciaSchema:
    """Serializa uma evidência.

    `controlo_empresa_id` é o controlo **em cujo contexto** a evidência está a ser
    mostrada. Com o N:N a mesma prova sustenta vários controlos, e o campo
    deixou de poder sair da evidência: depende de por onde se lá chegou. Quem não
    o passa fica com a ligação de origem, que é o comportamento antigo."""
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
        controlo_empresa_id=controlo_empresa_id or ev.controlo_empresa_v2_id,
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

    # A verdade de "que evidências sustentam este controlo" passou a ser a tabela
    # de ligação: a coluna `controlo_empresa_v2_id` já não decide nada.
    evidencias = ligacoes.evidencias_de(
        db, requisito_id=controlo_empresa_id, empresa_id=empresa_id
    )
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

    # A nota de âmbito é da LIGAÇÃO a este controlo, não da evidência: a mesma
    # prova serve controlos diferentes por sítios diferentes do documento.
    notas = {
        ligacao.evidencia_id: ligacao
        for ligacao in db.exec(
            select(EvidenciaRequisito).where(
                EvidenciaRequisito.requisito_id == controlo_empresa_id,
                EvidenciaRequisito.desligado_em.is_(None),
            )
        ).all()
    }

    # Quantos controlos cada evidência sustenta — numa só consulta agrupada, e
    # não uma por evidência (a listagem custava N+1).
    totais_ligacoes = ligacoes.contar_ligacoes_ativas_em_lote(db, [ev.id for ev in evidencias])
    motivos = retencao.motivos_em_lote(db, empresa_id, [ev.id for ev in evidencias])

    itens = []
    for ev in evidencias:
        # Sem o texto das notas: a listagem é de metadados. O texto lê-se no
        # detalhe, que fica na trilha — trazê-lo aqui era lê-lo sem rasto.
        schema = _schema_from_evidencia(
            ev,
            uploaders.get(ev.uploaded_by_id),
            include_text=False,
            controlo_empresa_id=controlo_empresa_id,
        )
        ligacao = notas.get(ev.id)
        if ligacao is not None:
            schema.nota_ambito = ligacao.nota_ambito
            schema.ambito_por_confirmar = ligacao.ambito_por_confirmar
        schema.total_ligacoes = totais_ligacoes.get(ev.id, 0)
        schema.motivos_retencao = sorted(motivos.get(ev.id, ()))
        itens.append(schema)

    return ListaEvidenciasSchema(total=len(itens), evidencias=itens)


def listar_todas_evidencias(
    db: Session,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
) -> ListaTodasEvidenciasSchema:
    empresa = _get_empresa(db, empresa_id)
    framework = _ensure_framework(db, empresa)
    locale = resolver_locale(empresa, framework)

    # Uma linha por LIGAÇÃO, não por evidência: a mesma prova a sustentar
    # três controlos aparece nos três, porque a pergunta desta listagem é "que
    # provas sustentam o quê" e não "que ficheiros existem". Agrupar pela coluna
    # antiga mostrava-a uma vez só, no controlo onde por acaso foi carregada.
    pares = db.exec(
        select(Evidencia, EvidenciaRequisito)
        .join(EvidenciaRequisito, EvidenciaRequisito.evidencia_id == Evidencia.id)
        .where(
            Evidencia.empresa_id == empresa_id,
            Evidencia.deleted_at.is_(None),
            EvidenciaRequisito.desligado_em.is_(None),
        )
        .order_by(Evidencia.created_at.desc())
    ).all()

    if not pares:
        return ListaTodasEvidenciasSchema(total=0, evidencias=[])

    rows = load_company_control_rows(db, empresa_id, framework.id)
    rows_map = {row.ce.id: row for row in rows}

    # A listagem tem de coincidir com o gate do detalhe: quem só alcança o que
    # lhe está atribuído não pode ver na lista o que não consegue abrir.
    so_atribuidas = (
        ambito_de(utilizador, "evidencias", ClasseAcao.VER) is Ambito.ATRIBUIDO
    )

    evidencias_com_contexto = []
    for evidencia, ligacao in pares:
        ce_id = ligacao.requisito_id
        row = rows_map.get(ce_id)
        if not row:
            continue
        if so_atribuidas and row.ce.implementador_id != utilizador.id:
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
                # Nunca o texto inteiro (lê-se no detalhe, que fica na trilha); só
                # o resumo, e só para as notas sem título, que sem ele não teriam
                # nada que as identificasse na lista.
                include_text=False,
                include_summary=not bool(evidencia.titulo),
                # A linha é uma LIGAÇÃO: o controlo é o desta linha, não o da
                # coluna de origem da evidência.
                controlo_empresa_id=row.ce.id,
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


def criar_evidencia(
    db: Session,
    controlo_empresa_id: uuid.UUID,
    empresa_id: uuid.UUID,
    titulo: str | None,
    conteudo_texto: str | None,
    ficheiro: UploadFile | None,
    utilizador: Utilizador,
    request: Request | None = None,
    evitar_duplicado: bool = False,
    conteudo_hash_ext: str | None = None,
    ligar_existente: bool | None = None,
) -> EvidenciaSchema:
    ce = _get_ce(db, controlo_empresa_id, empresa_id)
    _verificar_acesso_escrita_evidencias(ce, utilizador)

    titulo_limpo = titulo.strip() if titulo else None
    if not titulo_limpo:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="O título da evidência é obrigatório.",
        )
    if len(titulo_limpo) > _TITULO_MAX:
        # A coluna tem 255: acima disso o INSERT falhava depois de o ficheiro
        # já estar no disco.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="O título da evidência não pode ter mais de 255 caracteres.",
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
    ficheiro_bytes: bytes | None = None
    content_type = ""

    # ── 1. Ler e validar o ficheiro (ainda SEM escrever no disco) ────────────────
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

        # Leitura pelo objeto de ficheiro em vez do `await`: esta função corre
        # no threadpool (ver o cabeçalho de `_trancar_dedup`) e o `UploadFile`
        # expõe o ficheiro real por baixo, já em memória ou em disco temporário.
        ficheiro.file.seek(0)
        ficheiro_bytes = ficheiro.file.read()
        # Teto do tenant, com o da instalação como escalão seguinte. O clamp
        # da política segura-o nos 14 MB que o nginx à frente aceita: deixar
        # configurar acima disso daria um erro do proxy, longe deste ecrã, e
        # ninguém ligaria as duas coisas.
        max_mb = politica_seguranca.politica(db, empresa_id).max_upload_mb
        if len(ficheiro_bytes) > max_mb * 1024 * 1024:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Ficheiro demasiado grande. Máximo: {max_mb} MB.",
            )
        if len(ficheiro_bytes) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="O ficheiro não pode estar vazio.",
            )

        # Validar magic bytes reais do conteúdo — impede Content-Type falsificado (CWE-434)
        if not _validar_magic_bytes(ficheiro_bytes, content_type):
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
            ficheiro_bytes, content_type
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="O ficheiro não tem a estrutura interna esperada para o tipo declarado.",
            )

    # ── 2. Impressão digital do conteúdo em claro + pré-consulta de duplicado ───
    # Para documentos gerados (PDF), o hash do ficheiro variaria a cada geração
    # (traz a data lá dentro), por isso o core passa um hash estável do conteúdo
    # (`conteudo_hash_ext`) que se usa em vez disso.
    conteudo_hash = (
        _hash_ext_valido(conteudo_hash_ext, empresa_id, controlo_empresa_id)
        or _hash_conteudo(texto_limpo, ficheiro_bytes)
    )

    # Consulta OTIMISTA, sem tranca. Não garante nada — entre ela e a escrita cabe
    # outro pedido — e existe só para não gastar disco nem processamento no caso
    # comum, que é a repetição em sequência. Quem garante é a re-consulta do
    # passo 4, sob tranca. As duas chamam a MESMA função de propósito: têm de
    # responder o mesmo, senão o desfecho passaria a depender de ter havido
    # concorrência ou não.
    decisao = _resolver_duplicado(
        db,
        empresa_id=empresa_id,
        controlo_empresa_id=controlo_empresa_id,
        conteudo_hash=conteudo_hash,
        evitar_duplicado=evitar_duplicado,
        ligar_existente=ligar_existente,
        utilizador=utilizador,
        request=request,
    )
    if decisao.resposta is not None:
        return decisao.resposta
    duplicado = decisao.deliberado

    # ── 3. Escrever o ficheiro num caminho TEMPORÁRIO, FORA da secção crítica ───
    # A cifra e a escrita são a parte lenta do pedido. Ficam aqui, sem tranca
    # nenhuma tomada, para que a serialização do passo 4 dure o que dura uma
    # consulta e um `rename` — e não o que demora escrever 10 MB.
    temporario_path: Path | None = None
    destino_path: Path | None = None
    if ficheiro_bytes is not None and ficheiro is not None:
        # Quota de armazenamento por empresa (0 = ilimitado). Uso = soma dos tamanhos
        # lógicos das evidências não-eliminadas.
        quota = _quota_bytes(str(empresa_id))
        if quota:
            usado: int = db.exec(
                select(func.coalesce(func.sum(Evidencia.ficheiro_tamanho), 0)).where(
                    Evidencia.empresa_id == empresa_id,
                    Evidencia.deleted_at.is_(None),  # type: ignore[union-attr]
                )
            ).one()
            if usado + len(ficheiro_bytes) > quota:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=traduzir(
                        MsgsI18n.STORAGE_QUOTA_EXCEDIDA,
                        locale_de_request(request),
                        usado=usado // _MIB,
                        total=quota // _MIB,
                    ),
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
        # No disco só o UUID: o nome original vai cifrado em `ficheiro_nome`, e um
        # caminho com ele («despedimento-joao-silva.pdf») anulava essa cifra para
        # quem visse o volume, o `ficheiro_path` ou uma listagem do backup.
        destino_path = destino_dir / str(uuid.uuid4())
        # O prefixo distingue-o de um órfão verdadeiro: se o processo morrer entre
        # a escrita e o `rename`, fica um `.parcial-` sem linha na base — o lado
        # seguro, porque a reconciliação do restauro só REPORTA ficheiros órfãos
        # (podem ser a única cópia de uma prova) e nunca os apaga.
        temporario_path = destino_dir / f".parcial-{destino_path.name}"

        # Defence-in-depth: garantir que o caminho resolvido está dentro de UPLOADS_DIR.
        # Previne path traversal residual se UPLOADS_DIR for relativo e o cwd mudar (CWE-22).
        _uploads_root = Path(settings.UPLOADS_DIR).resolve()
        if not destino_path.resolve().is_relative_to(_uploads_root):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Caminho de destino inválido.",
            )

        bytes_a_escrever = ficheiro_bytes
        if settings.EVIDENCE_ENCRYPTION_KEY:
            bytes_a_escrever = cifrar_bytes_evidencia(ficheiro_bytes)
            ficheiro_cifrado = True

        try:
            with open(temporario_path, "wb") as file_handle:
                file_handle.write(bytes_a_escrever)
        except OSError as exc:
            # Disco cheio (ENOSPC) ou falha de escrita: limpa o ficheiro parcial e
            # devolve um erro claro (507) em vez de rebentar num 500 genérico.
            try:
                temporario_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise HTTPException(
                status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                detail=traduzir(MsgsI18n.DISCO_SEM_ESPACO, locale_de_request(request)),
            ) from exc

        ficheiro_nome = cifrar_pii(nome_original)
        ficheiro_tipo_mime = content_type
        ficheiro_tamanho = len(ficheiro_bytes)

    # ── 4. Secção crítica: tranca, re-consulta e publicação do ficheiro ─────────
    # Aqui dentro só há uma consulta, um `rename` e (a seguir) os INSERT. É esta
    # brevidade que torna a tranca viável: a tentativa anterior tomava-a antes da
    # escrita e a aplicação parava à espera de I/O.
    #
    # A re-consulta repete a do passo 2 de propósito. A primeira é conforto — evita
    # escrever no disco no caso comum; esta é a garantia, porque só ela corre com
    # a certeza de que mais ninguém está a decidir o mesmo ao mesmo tempo.
    try:
        _trancar_dedup(db, empresa_id, controlo_empresa_id)
        decisao = _resolver_duplicado(
            db,
            empresa_id=empresa_id,
            controlo_empresa_id=controlo_empresa_id,
            conteudo_hash=conteudo_hash,
            evitar_duplicado=evitar_duplicado,
            ligar_existente=ligar_existente,
            utilizador=utilizador,
            request=request,
        )
        if decisao.resposta is not None:
            # Perder esta corrida é um desfecho PREVISTO, não uma avaria: quem
            # chega em segundo recebe exatamente o que receberia em sequência.
            return decisao.resposta
        duplicado = decisao.deliberado

        if temporario_path is not None and destino_path is not None:
            # `os.replace` é atómico dentro do mesmo sistema de ficheiros, e a
            # origem e o destino são a mesma pasta.
            os.replace(temporario_path, destino_path)
            ficheiro_path = str(destino_path)
            temporario_path = None
    finally:
        # Sai por onde sair — no-op, 409 ou exceção — o temporário não fica.
        if temporario_path is not None:
            try:
                temporario_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Não foi possível remover o temporário %s", temporario_path)

    # ── 5. Cifrar o texto em repouso (se chave configurada) e persistir ──────────
    # Ainda sob a tranca do passo 4: só o commit no fim do pedido a liberta, e é
    # isso que garante que o concorrente só volta a decidir com esta linha visível.
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
        conteudo_hash=conteudo_hash,
        uploaded_by_id=utilizador.id,
    )
    db.add(evidencia)
    try:
        db.flush()
    except Exception:
        # O ficheiro já foi publicado no caminho final: sem a linha na base
        # ficava no disco, fora do alcance do apagamento.
        if ficheiro_path:
            try:
                Path(ficheiro_path).unlink(missing_ok=True)
            except OSError:
                logger.warning("Não foi possível remover o ficheiro órfão %s", ficheiro_path)
        raise

    # A ligação é o que faz a evidência sustentar o controlo. A coluna
    # `controlo_empresa_v2_id` continua preenchida acima como marca da ligação de
    # origem, mas quem decide o que aparece no controlo é esta linha.
    ligacoes.ligar(
        db,
        evidencia_id=evidencia.id,
        requisito_id=controlo_empresa_id,
        empresa_id=empresa_id,
        ligado_por_id=utilizador.id,
    )

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
            # A impressão digital, e não o título: é ela que liga esta entrada à
            # que um dia registar a saída, e um título pode ser ele próprio um
            # dado pessoal que a trilha — que não se reescreve — nunca largaria.
            "conteudo_hash": conteudo_hash,
            "controlo_empresa_id": str(controlo_empresa_id),
            "tem_ficheiro": tem_ficheiro,
            "tem_texto": tem_texto,
            "duplicado": duplicado,
        },
        request=request,
    )
    db.flush()
    db.refresh(evidencia)
    schema = _schema_from_evidencia(evidencia, decifrar_pii(utilizador.nome))
    schema.duplicado = duplicado
    schema.criado = True
    return schema


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
    """Autoriza o acesso a uma evidência através dos controlos a que está ligada.

    Com o N:N, a mesma prova pode sustentar controlos com donos diferentes. A
    regra é: **basta poder ver UM dos controlos ligados**. Exigir todos tornaria
    a evidência partilhada inacessível a toda a gente assim que fosse ligada a um
    controlo de outra pessoa — e o efeito prático seria as pessoas voltarem a
    carregar cópias privadas, que é o problema que o N:N veio resolver.

    Não é um alargamento: quem alcança o controlo A já via esta prova quando ela
    era só de A. Ligá-la também a B não tira nada a ninguém, e o dono de B não
    ganha acesso nenhum por esta via.

    Devolve o controlo por onde o acesso foi concedido — é esse o contexto em que
    o utilizador está a ver a prova.
    """
    requisitos = [lig.requisito_id for lig in ligacoes.requisitos_de(db, evidencia.id)]
    # Uma evidência órfã (sem ligações ativas) não é alcançável por controlo
    # nenhum. Fica no 404 em vez de num 403: quem não tem por onde lá chegar não
    # deve sequer saber que existe.
    if not requisitos:
        raise HTTPException(status_code=404, detail="Controlo não encontrado.")

    negado: HTTPException | None = None
    for requisito_id in requisitos:
        ce = db.get(ControloEmpresaV2, requisito_id)
        if not ce:
            continue
        try:
            _verificar_acesso_leitura_evidencias(ce, utilizador)
            return ce
        except HTTPException as recusa:
            negado = recusa

    # Havia ligações, mas nenhuma alcançável por este utilizador.
    raise negado or HTTPException(status_code=404, detail="Controlo não encontrado.")


def get_evidencia(
    db: Session,
    evidencia_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
) -> EvidenciaSchema:
    evidencia = _get_evidencia_or_404(db, evidencia_id, empresa_id)
    # Uma órfã abre-se a quem a pode ver na reciclagem: decidir apagá-la de vez
    # sem a poder abrir seria apagar às cegas.
    _verificar_acesso_ou_orfa(db, evidencia, utilizador)
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
    # Uma órfã abre-se a quem a pode ver na reciclagem: decidir apagá-la de vez
    # sem a poder abrir seria apagar às cegas.
    _verificar_acesso_ou_orfa(db, evidencia, utilizador)

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


def registar_acesso_conteudo(
    db: Session,
    evidencia_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
    *,
    descarregada: bool,
    parte: str,
    request: Request | None = None,
) -> None:
    """Deixa na trilha que o conteúdo de uma evidência saiu para alguém.

    Chama-se depois de o acesso ter sido decidido e antes de o conteúdo seguir;
    quem chama faz o commit. A linha leva a impressão digital do conteúdo (a
    mesma das outras entradas desta evidência) e a parte vista — nunca o texto,
    o título nem o nome do ficheiro: o nome pode ser um dado pessoal, e a trilha
    não se reescreve."""
    evidencia = db.get(Evidencia, evidencia_id)
    registar_acao(
        db,
        acao=Acao.EVIDENCIA_DESCARREGADA if descarregada else Acao.EVIDENCIA_VISUALIZADA,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Evidencia",
        entidade_id=evidencia_id,
        dados_novos={
            "conteudo_hash": evidencia.conteudo_hash if evidencia is not None else None,
            "parte": parte,
        },
        request=request,
    )


# ---------------------------------------------------------------------------
# Ligações evidência ↔ controlo
# ---------------------------------------------------------------------------

# Estados em que o controlo está a dizer que o trabalho está feito. Tirar-lhe a
# única prova não se impede — quem a retira pode ter razão —, mas avisa-se antes,
# porque a alternativa é a conformidade degradar-se sem ninguém dar por isso.
_ESTADOS_DECLARADOS = (EstadoControlo.IMPLEMENTADO, EstadoControlo.APROVADO)


def _titulos_dos_controlos(
    db: Session, empresa_id: uuid.UUID, ce_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str, str]]:
    """`{ce_id: (codigo, titulo, estado)}` para as ligações que se vão mostrar.

    Um UUID não diz a ninguém que controlo é. Resolve-se aqui, com o mesmo
    caminho que a listagem global usa, para o texto sair na língua da empresa.
    """
    if not ce_ids:
        return {}
    try:
        empresa = _get_empresa(db, empresa_id)
        framework = _ensure_framework(db, empresa)
        locale = resolver_locale(empresa, framework)
        todas = load_company_control_rows(db, empresa_id, framework.id)
    except HTTPException:
        # O rótulo é decoração; a ligação é o dado. Uma empresa sem framework
        # resolvido, ou um catálogo em falta, não pode fazer desaparecer a lista
        # de controlos que uma prova sustenta — sai sem código nem título.
        logger.warning(
            "Sem catálogo para rotular ligações da empresa %s; devolvidas sem título.",
            empresa_id,
        )
        return {}

    rows = [row for row in todas if row.ce.id in ce_ids]
    if not rows:
        return {}

    control_locales = load_preferred_locales(
        db, ControlLocale, "control_id", {row.control.id for row in rows},
        locale, framework.default_locale,
    )
    return {
        row.ce.id: (
            row.control.code,
            control_locales[row.control.id].title
            if row.control.id in control_locales
            else row.control.code,
            row.ce.estado.value,
        )
        for row in rows
    }


def _ligacoes_visiveis(
    db: Session, evidencia: Evidencia, utilizador: Utilizador
) -> list[EvidenciaRequisito]:
    """Ligações ativas que este utilizador pode ver.

    Quem só alcança o que lhe está atribuído não pode descobrir, pela lista de
    ligações de uma prova partilhada, que controlos existem fora do seu âmbito.
    """
    ligadas = ligacoes.requisitos_de(db, evidencia.id)
    if ambito_de(utilizador, "evidencias", ClasseAcao.VER) is not Ambito.ATRIBUIDO:
        return ligadas
    visiveis = []
    for ligacao in ligadas:
        ce = db.get(ControloEmpresaV2, ligacao.requisito_id)
        if ce is not None and ce.implementador_id == utilizador.id:
            visiveis.append(ligacao)
    return visiveis


def listar_ligacoes(
    db: Session,
    evidencia_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
) -> ListaLigacoesSchema:
    evidencia = _get_evidencia_or_404(db, evidencia_id, empresa_id)
    _verificar_acesso_evidencia(db, evidencia, utilizador)

    ligadas = _ligacoes_visiveis(db, evidencia, utilizador)
    contexto = _titulos_dos_controlos(db, empresa_id, {l.requisito_id for l in ligadas})
    autores = {
        u.id: decifrar_pii(u.nome)
        for u in db.exec(
            select(Utilizador).where(
                Utilizador.id.in_([l.ligado_por_id for l in ligadas if l.ligado_por_id])
            )
        ).all()
    } if any(l.ligado_por_id for l in ligadas) else {}

    itens = []
    for ligacao in ligadas:
        codigo, titulo, estado = contexto.get(ligacao.requisito_id, (None, None, None))
        itens.append(
            LigacaoSchema(
                requisito_id=ligacao.requisito_id,
                controlo_codigo=codigo,
                controlo_titulo=titulo,
                controlo_estado=estado,
                nota_ambito=ligacao.nota_ambito,
                ambito_por_confirmar=ligacao.ambito_por_confirmar,
                ligado_em=ligacao.ligado_em,
                ligado_por_nome=autores.get(ligacao.ligado_por_id),
            )
        )
    return ListaLigacoesSchema(total=len(itens), ligacoes=itens)


def listar_orfas(
    db: Session, empresa_id: uuid.UUID, utilizador: Utilizador
) -> ListaOrfasSchema:
    """A reciclagem: provas que não sustentam controlo nenhum.

    Uma órfã não está ligada a controlo nenhum, logo não há controlo por quem
    decidir o acesso. Quem alcança tudo vê todas; quem só alcança o que lhe está
    atribuído vê as que carregou e as que saíram de um controlo seu — é por aqui
    que desfaz o próprio engano (restaurar, religar, ou apagar de vez as suas).
    """
    dias = settings.EVIDENCIA_ORFA_DIAS
    itens = reciclagem.listar_orfas(db, empresa_id, dias)
    if ambito_de(utilizador, "evidencias", ClasseAcao.VER) is Ambito.ATRIBUIDO:
        de_controlos_meus = _controlos_de_origem_meus(
            db, utilizador, [i["id"] for i in itens if i["uploaded_by_id"] != utilizador.id]
        )
        itens = [
            i for i in itens
            if i["uploaded_by_id"] == utilizador.id or i["id"] in de_controlos_meus
        ]

    contexto = _titulos_dos_controlos(
        db, empresa_id, {ce_id for item in itens for ce_id in item["saiu_de"]}
    )
    retiradas_por = _quem_retirou(db, [i["id"] for i in itens])
    nomes = _nomes_de(
        db, {i["uploaded_by_id"] for i in itens} | set(retiradas_por.values())
    )
    for item in itens:
        item["pode_apagar"] = not item["retida"] and _pode_apagar_orfa(
            utilizador, item["uploaded_by_id"]
        )
        item["saiu_de"] = [
            OrigemSchema(
                requisito_id=ce_id,
                controlo_codigo=contexto.get(ce_id, (None, None, None))[0],
                controlo_titulo=contexto.get(ce_id, (None, None, None))[1],
            )
            for ce_id in item["saiu_de"]
        ]
        item["carregada_por_nome"] = nomes.get(item["uploaded_by_id"])
        item["retirada_por_nome"] = nomes.get(retiradas_por.get(item["id"]))
    return ListaOrfasSchema(
        total=len(itens),
        dias_reciclagem=dias,
        retencao_anos=max(0, settings.EVIDENCIA_RETENCAO_ANOS),
        orfas=[OrfaSchema(**item) for item in itens],
    )


def _nomes_de(db: Session, ids: set) -> dict[uuid.UUID, str | None]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {
        u.id: decifrar_pii(u.nome)
        for u in db.exec(select(Utilizador).where(Utilizador.id.in_(list(ids)))).all()
    }


def _quem_retirou(db: Session, evidencia_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """`{evidencia_id: utilizador}` do último "retirar" de cada uma, pela trilha.

    A ligação não guarda quem a fechou; a trilha guarda. Se a entrada já tiver
    sido arquivada, fica sem nome — é informação para o ecrã, não uma garantia.
    """
    if not evidencia_ids:
        return {}
    from app.shared.audit import AuditLog

    ultimo: dict[uuid.UUID, tuple] = {}
    for linha in db.exec(
        select(AuditLog).where(
            AuditLog.entidade_id.in_(evidencia_ids),
            AuditLog.acao == Acao.EVIDENCIA_DESLIGADA,
        )
    ).all():
        if linha.utilizador_id is None:
            continue
        atual = ultimo.get(linha.entidade_id)
        if atual is None or linha.created_at > atual[0]:
            ultimo[linha.entidade_id] = (linha.created_at, linha.utilizador_id)
    return {ev_id: quem for ev_id, (_, quem) in ultimo.items()}


def _pode_apagar_orfa(utilizador: Utilizador, carregada_por: uuid.UUID | None) -> bool:
    ambito = ambito_de(utilizador, "evidencias", ClasseAcao.ELIMINAR)
    if ambito is Ambito.TOTAL:
        return True
    return ambito is Ambito.ATRIBUIDO and carregada_por == utilizador.id


def _controlos_de_origem_meus(
    db: Session, utilizador: Utilizador, evidencia_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """Das órfãs indicadas, as que saíram de um controlo atribuído a este utilizador."""
    origem = {ev_id: ligacoes.ultimas_desligadas(db, ev_id) for ev_id in evidencia_ids}
    ce_ids = {l.requisito_id for lista in origem.values() for l in lista}
    if not ce_ids:
        return set()
    meus = {
        ce.id
        for ce in db.exec(
            select(ControloEmpresaV2).where(ControloEmpresaV2.id.in_(list(ce_ids)))
        ).all()
        if ce.implementador_id is not None and str(ce.implementador_id) == str(utilizador.id)
    }
    return {ev_id for ev_id, lista in origem.items() if any(l.requisito_id in meus for l in lista)}


def _exigir_acesso_orfa(
    db: Session, utilizador: Utilizador, evidencia: Evidencia
) -> None:
    """Acesso a uma órfã (ver `listar_orfas`).

    Sem controlo ligado, não há dono de controlo por quem decidir. Chega-lhe quem
    alcança tudo, quem a carregou, e quem trabalha num controlo de onde ela
    saiu — até há pouco era prova do trabalho dessa pessoa, e é ela quem precisa
    de a desfazer. Apagá-la de vez continua a ser só de quem a carregou.
    """
    if ambito_de(utilizador, "evidencias", ClasseAcao.VER) is Ambito.ATRIBUIDO and (
        evidencia.id in _controlos_de_origem_meus(db, utilizador, [evidencia.id])
    ):
        return
    exigir_ambito(utilizador, "evidencias", ClasseAcao.VER, evidencia.uploaded_by_id)


def _verificar_acesso_ou_orfa(
    db: Session, evidencia: Evidencia, utilizador: Utilizador
) -> None:
    if ligacoes.esta_orfa(db, evidencia.id):
        _exigir_acesso_orfa(db, utilizador, evidencia)
    else:
        _verificar_acesso_evidencia(db, evidencia, utilizador)


def _alcanca_evidencia(db: Session, evidencia: Evidencia, utilizador: Utilizador) -> bool:
    """A regra de `_verificar_acesso_ou_orfa`, sem recusar nem registar recusa.

    Serve para decidir o que se mostra (a deduplicação não fala de uma evidência
    que quem envia não alcança), não para negar um pedido — aí usa-se a porta,
    que regista. Há um teste que confere as duas caso a caso."""
    ver = ambito_de(utilizador, "evidencias", ClasseAcao.VER)
    if ver is Ambito.TOTAL:
        return True
    if ver is not Ambito.ATRIBUIDO:
        return False
    eu = str(utilizador.id)
    requisitos = ligacoes.requisitos_de(db, evidencia.id)
    if not requisitos:
        if evidencia.id in _controlos_de_origem_meus(db, utilizador, [evidencia.id]):
            return True
        return evidencia.uploaded_by_id is not None and str(evidencia.uploaded_by_id) == eu
    for ligacao in requisitos:
        ce = db.get(ControloEmpresaV2, ligacao.requisito_id)
        if ce is not None and ce.implementador_id is not None and str(ce.implementador_id) == eu:
            return True
    return False


def ligar_evidencia(
    db: Session,
    evidencia_id: uuid.UUID,
    requisito_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
    nota_ambito: str | None = None,
    request: Request | None = None,
) -> LigacaoSchema:
    """Liga uma prova que já existe a mais um controlo.

    A autorização é **do destino**: quem liga está a mexer no controlo que
    recebe a prova, e é sobre esse que tem de ter direito de operar. Ver a
    evidência não chega — senão bastava alcançá-la por um controlo qualquer para
    a enfiar em controlos de outra pessoa.

    Uma órfã não tem controlo por onde se lhe chegue; religá-la a partir da
    reciclagem pede o acesso de órfã (tudo, ou ser quem a carregou).
    """
    evidencia = _get_evidencia_or_404(db, evidencia_id, empresa_id)
    _verificar_acesso_ou_orfa(db, evidencia, utilizador)

    ce = _get_ce(db, requisito_id, empresa_id)
    _verificar_acesso_escrita_evidencias(ce, utilizador)

    ja_existia = ligacoes.ligacao_ativa(
        db, evidencia_id=evidencia.id, requisito_id=requisito_id
    ) is not None

    nota = (nota_ambito or "").strip() or None
    ligacao = ligacoes.ligar(
        db,
        evidencia_id=evidencia.id,
        requisito_id=requisito_id,
        empresa_id=empresa_id,
        ligado_por_id=utilizador.id,
        nota_ambito=nota,
    )

    # Religar o que já estava ligado é um no-op: não se regista, para a trilha
    # não encher de linhas que não descrevem mudança nenhuma.
    if not ja_existia:
        registar_acao(
            db,
            acao=Acao.EVIDENCIA_LIGADA,
            resultado=ResultadoAcao.SUCESSO,
            empresa_id=empresa_id,
            utilizador_id=utilizador.id,
            entidade_tipo="Evidencia",
            entidade_id=evidencia.id,
            dados_novos={
                "conteudo_hash": evidencia.conteudo_hash,
                "controlo_empresa_id": str(requisito_id),
                "tem_nota_ambito": bool(nota),
            },
            request=request,
        )
    db.commit()

    codigo, titulo, estado = _titulos_dos_controlos(
        db, empresa_id, {requisito_id}
    ).get(requisito_id, (None, None, None))
    return LigacaoSchema(
        requisito_id=requisito_id,
        controlo_codigo=codigo,
        controlo_titulo=titulo,
        controlo_estado=estado,
        nota_ambito=ligacao.nota_ambito,
        ambito_por_confirmar=ligacao.ambito_por_confirmar,
        ligado_em=ligacao.ligado_em,
        ligado_por_nome=decifrar_pii(utilizador.nome),
    )


def desligar_evidencia(
    db: Session,
    evidencia_id: uuid.UUID,
    requisito_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
    confirmado: bool = False,
    request: Request | None = None,
) -> None:
    """Tira a prova de UM controlo. Os outros ficam como estavam.

    Não apaga nada: a evidência continua a existir e, se ficar sem ligação
    nenhuma, passa ao estado de órfã — que a reciclagem trata mais tarde, com
    tempo para desfazer.
    """
    evidencia = _get_evidencia_or_404(db, evidencia_id, empresa_id)
    _verificar_acesso_evidencia(db, evidencia, utilizador)

    ce = _get_ce(db, requisito_id, empresa_id)
    _verificar_acesso_escrita_evidencias(ce, utilizador)

    if ligacoes.ligacao_ativa(db, evidencia_id=evidencia.id, requisito_id=requisito_id) is None:
        raise HTTPException(
            status_code=404, detail="Esta evidência não está ligada a este controlo."
        )

    # O controlo fica sem prova E está dado como feito: pergunta-se antes.
    restantes = len(
        ligacoes.evidencias_de(db, requisito_id=requisito_id, empresa_id=empresa_id)
    )
    if restantes <= 1 and ce.estado in _ESTADOS_DECLARADOS and not confirmado:
        contexto = _titulos_dos_controlos(db, empresa_id, {requisito_id})
        codigo = contexto.get(requisito_id, ("", "", ""))[0] or str(requisito_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": "ultima_prova",
                "mensagem": traduzir(
                    MsgsI18n.EVIDENCIA_ULTIMA_PROVA,
                    locale_de_request(request),
                    controlo=codigo,
                    estado=ce.estado.value,
                ),
            },
        )

    ligacoes.desligar(db, evidencia_id=evidencia.id, requisito_id=requisito_id)
    orfa = ligacoes.esta_orfa(db, evidencia.id)

    registar_acao(
        db,
        acao=Acao.EVIDENCIA_DESLIGADA,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Evidencia",
        entidade_id=evidencia.id,
        dados_novos={
            "conteudo_hash": evidencia.conteudo_hash,
            "controlo_empresa_id": str(requisito_id),
            # Sem isto, quem lê a trilha não distingue "saiu de um dos seis
            # controlos" de "deixou de sustentar seja o que for".
            "ficou_orfa": orfa,
        },
        request=request,
    )
    db.commit()


def atualizar_nota_ambito(
    db: Session,
    evidencia_id: uuid.UUID,
    requisito_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
    nota_ambito: str | None,
    request: Request | None = None,
) -> LigacaoSchema:
    """Onde olhar dentro do documento, para este controlo.

    Escrever a nota confirma-a: um `ambito_por_confirmar` herdado de uma versão
    anterior deixa de fazer sentido no momento em que alguém a reescreve.
    """
    evidencia = _get_evidencia_or_404(db, evidencia_id, empresa_id)
    _verificar_acesso_evidencia(db, evidencia, utilizador)

    ce = _get_ce(db, requisito_id, empresa_id)
    _verificar_acesso_escrita_evidencias(ce, utilizador)

    ligacao = ligacoes.ligacao_ativa(
        db, evidencia_id=evidencia.id, requisito_id=requisito_id
    )
    if ligacao is None:
        raise HTTPException(
            status_code=404, detail="Esta evidência não está ligada a este controlo."
        )

    anterior = ligacao.nota_ambito
    ligacao.nota_ambito = (nota_ambito or "").strip() or None
    ligacao.ambito_por_confirmar = False
    db.add(ligacao)
    db.flush()

    registar_acao(
        db,
        acao=Acao.EVIDENCIA_AMBITO_ALTERADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Evidencia",
        entidade_id=evidencia.id,
        dados_anteriores={"nota_ambito": anterior},
        dados_novos={
            "nota_ambito": ligacao.nota_ambito,
            "controlo_empresa_id": str(requisito_id),
        },
        request=request,
    )
    db.commit()

    codigo, titulo, estado = _titulos_dos_controlos(
        db, empresa_id, {requisito_id}
    ).get(requisito_id, (None, None, None))
    return LigacaoSchema(
        requisito_id=requisito_id,
        controlo_codigo=codigo,
        controlo_titulo=titulo,
        controlo_estado=estado,
        nota_ambito=ligacao.nota_ambito,
        ambito_por_confirmar=False,
        ligado_em=ligacao.ligado_em,
        ligado_por_nome=decifrar_pii(utilizador.nome),
    )


# ---------------------------------------------------------------------------
# Metadados e versões
#
# Regra de ouro: **a versão é do documento; a adequação é da ligação.** O
# conteúdo é partilhado por todos os controlos que o usam; o juízo sobre se
# aquele conteúdo serve aquele controlo é de cada ligação, uma a uma.
# ---------------------------------------------------------------------------

def _exigir_escrita_por_ligacao(
    db: Session, evidencia: Evidencia, utilizador: Utilizador
) -> list[uuid.UUID]:
    """Direito de operar em pelo menos um controlo que esta prova sustenta.

    Devolve os controlos onde o utilizador pode mesmo escrever — é sobre esses
    que uma revisão pode propagar. Basta um para poder rever o documento (o
    conteúdo é partilhado); mas propagar a revisão a um controlo alheio, não.
    """
    permitidos = []
    for ligacao in ligacoes.requisitos_de(db, evidencia.id):
        ce = db.get(ControloEmpresaV2, ligacao.requisito_id)
        if ce is None:
            continue
        try:
            _verificar_acesso_escrita_evidencias(ce, utilizador)
            permitidos.append(ligacao.requisito_id)
        except HTTPException:
            continue
    if not permitidos:
        from app.shared.audit import registar_negacao

        registar_negacao(utilizador, modulo="evidencias", acao="operar", codigo="sem_permissao_recurso")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sem permissão para alterar esta evidência.",
        )
    return permitidos


def atualizar_metadados(
    db: Session,
    evidencia_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
    titulo: str | None = None,
    valido_ate: datetime | None = None,
    request: Request | None = None,
) -> EvidenciaSchema:
    """Corrige o que descreve a prova. Não é versão nova: o conteúdo não mudou."""
    evidencia = _get_evidencia_or_404(db, evidencia_id, empresa_id)
    _verificar_acesso_evidencia(db, evidencia, utilizador)
    _exigir_escrita_por_ligacao(db, evidencia, utilizador)

    campos: list[str] = []
    if titulo is not None:
        limpo = titulo.strip()
        if not limpo:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="O título da evidência é obrigatório.",
            )
        if limpo != evidencia.titulo:
            campos.append("titulo")
        evidencia.titulo = limpo
    if valido_ate is not None:
        if valido_ate != evidencia.valido_ate:
            campos.append("valido_ate")
        evidencia.valido_ate = valido_ate

    db.add(evidencia)
    # Regista QUE campos mudaram, não o antes e o depois: corrigir um título
    # costuma ser tirar dele o que lá não devia estar, e a trilha não se
    # reescreve — guardar o antigo seria guardá-lo para sempre.
    registar_acao(
        db,
        acao=Acao.EVIDENCIA_METADADOS_ALTERADOS,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Evidencia",
        entidade_id=evidencia.id,
        dados_novos={
            "campos": campos,
            "conteudo_hash": evidencia.conteudo_hash,
            # Sem validade fica `null` (e não o texto "None").
            "valido_ate": evidencia.valido_ate.isoformat() if evidencia.valido_ate else None,
        },
        request=request,
    )
    db.commit()
    db.refresh(evidencia)

    uploader = db.get(Utilizador, evidencia.uploaded_by_id)
    return _schema_from_evidencia(
        evidencia, decifrar_pii(uploader.nome) if uploader else None
    )


def criar_versao(
    db: Session,
    evidencia_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
    titulo: str | None = None,
    conteudo_texto: str | None = None,
    ficheiro: UploadFile | None = None,
    requisitos: list[uuid.UUID] | None = None,
    request: Request | None = None,
) -> EvidenciaSchema:
    """Substitui o conteúdo: cria a versão seguinte e move as ligações escolhidas.

    O que isto resolve: rever uma política era apagar e voltar a carregar, e a
    nova entrava por **um** controlo — os outros ficavam sem prova, em silêncio,
    e o relatório mostrava um período sem prova quando a empresa até fez o
    trabalho a horas.

    `requisitos` é a lista **editável** de controlos que passam à versão nova.
    Por omissão vão todos, que é o caso comum; desmarcar um deixa-o na versão
    anterior, que continua viva porque continua ligada. É o caso perigoso: uma
    revisão que reduz o âmbito, propagada em silêncio, **piora** a conformidade
    — por isso a decisão fica à vista em vez de ser automática.

    A versão anterior que fique sem ligações passa à reciclagem como qualquer
    órfã. Só fica guardada se já tiver sido prova (ver `retencao`): substituir um
    upload errado não o pode transformar em registo permanente.
    """
    anterior = _get_evidencia_or_404(db, evidencia_id, empresa_id)

    # Controlo otimista: duas pessoas a rever ao mesmo tempo. Quem
    # chega em segundo escreveria por cima da revisão que não viu — a cadeia
    # bifurcaria e a história deixaria de ter uma linha só.
    #
    # Vem ANTES do gate de acesso, e a autorização é feita pela **sucessora**:
    # ao ser substituída, a versão anterior perde as ligações para a nova e fica
    # órfã, e uma órfã não é alcançável por controlo nenhum. Pela ordem inversa,
    # o segundo revisor levava um 404 «não encontrado» — quando o que aconteceu
    # foi o documento ter sido revisto entretanto, que é outra conversa.
    if anterior.substituida_em is not None:
        sucessora = db.exec(
            select(Evidencia).where(Evidencia.substitui_id == anterior.id)
        ).first()
        if sucessora is None:
            raise HTTPException(status_code=404, detail="Evidência não encontrada.")
        _verificar_acesso_evidencia(db, sucessora, utilizador)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": "versao_desatualizada",
                "evidencia_id": str(sucessora.id),
                "mensagem": traduzir(
                    MsgsI18n.EVIDENCIA_VERSAO_DESATUALIZADA, locale_de_request(request)
                ),
            },
        )

    _verificar_acesso_evidencia(db, anterior, utilizador)
    permitidos = _exigir_escrita_por_ligacao(db, anterior, utilizador)

    ligadas_agora = {l.requisito_id: l for l in ligacoes.requisitos_de(db, anterior.id)}
    if requisitos is None:
        alvos = list(ligadas_agora.keys())
    else:
        alvos = [r for r in requisitos if r in ligadas_agora]
        if len(alvos) != len(set(requisitos)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Só é possível propagar para controlos que esta evidência já sustenta.",
            )
    alheios = [r for r in alvos if r not in permitidos]
    if alheios:
        from app.shared.audit import registar_negacao

        registar_negacao(
            utilizador, modulo="evidencias", acao="delegar",
            codigo="sem_permissao_recurso", request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sem permissão para propagar a revisão a todos os controlos indicados.",
        )

    # A criação entra pelo controlo de origem da versão anterior se ele for um
    # dos alvos, senão pelo primeiro alvo: é preciso um para o caminho normal
    # validar o acesso, e tem de ser um em que quem revê pode escrever (todos os
    # alvos já foram confirmados acima). Entrar pela origem quando ela não era
    # alvo recusava a revisão de quem só a queria para o seu controlo.
    porta = anterior.controlo_empresa_v2_id
    if porta is None or porta not in alvos:
        porta = alvos[0] if alvos else None
    if porta is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Uma evidência sem controlo associado não pode ser revista; ligue-a primeiro.",
        )

    nova = criar_evidencia(
        db,
        porta,
        empresa_id,
        titulo=(titulo or anterior.titulo),
        conteudo_texto=conteudo_texto,
        ficheiro=ficheiro,
        utilizador=utilizador,
        request=request,
        # Conteúdo idêntico não cria versão nenhuma: devolve a que já
        # existe, e nesse caso não há nada a propagar.
        evitar_duplicado=True,
        # A revisão é deliberada: se o conteúdo for diferente mas já existir
        # noutra prova da empresa, não se pergunta nada — cria-se a versão.
        ligar_existente=False,
    )
    if nova.criado is False:
        return nova

    versao = db.get(Evidencia, nova.id)
    versao.substitui_id = anterior.id
    anterior.substituida_em = datetime.now(timezone.utc)
    if anterior.valido_ate is not None and versao.valido_ate is None:
        versao.valido_ate = anterior.valido_ate
    db.add(versao)
    db.add(anterior)
    db.flush()

    for requisito_id in alvos:
        ligacao = ligadas_agora[requisito_id]
        ligacoes.desligar(db, evidencia_id=anterior.id, requisito_id=requisito_id)
        ligacoes.ligar(
            db,
            evidencia_id=versao.id,
            requisito_id=requisito_id,
            empresa_id=empresa_id,
            ligado_por_id=utilizador.id,
            nota_ambito=ligacao.nota_ambito,
            # A nota dizia "secção 4.2" e na versão nova a secção 4.2 pode ser
            # outra coisa. Apagá-la perderia informação; mantê-la como verdade
            # mentiria. Fica com aviso até alguém a confirmar.
            ambito_por_confirmar=bool(ligacao.nota_ambito),
        )

    registar_acao(
        db,
        acao=Acao.EVIDENCIA_VERSAO_CRIADA,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Evidencia",
        entidade_id=versao.id,
        dados_anteriores={
            "evidencia_id": str(anterior.id),
            "conteudo_hash": anterior.conteudo_hash,
        },
        dados_novos={
            "conteudo_hash": versao.conteudo_hash,
            # Quantos controlos passaram à versão nova e quantos ficaram para
            # trás: é o tamanho do efeito, e sem ele "criou uma versão" não diz
            # nada a quem lê a trilha.
            "controlos_propagados": len(alvos),
            "controlos_na_versao_anterior": len(ligadas_agora) - len(alvos),
        },
        request=request,
    )
    db.commit()
    db.refresh(versao)

    schema = _schema_from_evidencia(versao, decifrar_pii(utilizador.nome))
    schema.total_ligacoes = len(alvos)
    return schema


# ---------------------------------------------------------------------------
# Saída de uma evidência
#
# Dois atos, e nenhum deles sobre uma prova viva sem aviso:
#   * apagar definitivamente — da reciclagem, só órfãs que nunca foram prova,
#     com razão. Sem lápide: nunca sustentou nada.
#   * apagamento com lápide — da administração, sobre qualquer prova (retida ou
#     ainda ligada), com fundamento. Leva a cadeia de versões inteira e as
#     cópias com o mesmo conteúdo, e deixa lápide sem título.
# Uma prova ligada a controlos retira-se primeiro (e retirar tem desfazer).
#
# Nos dois, a razão vai em código fechado para a trilha e o texto livre vai
# cifrado para a linha da evidência — nunca para a trilha, que não se reescreve
# e não pode guardar os dados que se estão a apagar.
# ---------------------------------------------------------------------------

RAZOES_DEFINITIVO = (
    "engano", "duplicado", "dados_pessoais", "informacao_confidencial", "outro",
)
FUNDAMENTOS_LAPIDE = (
    "pedido_titular", "dados_pessoais", "informacao_confidencial", "outro",
)

_TEXTO_MINIMO = 10

# Teto do alcance de um apagamento com lápide. Uma cadeia real tem poucas
# versões e poucas cópias; passar disto é sinal de dados estranhos, e é melhor
# parar e dizê-lo do que apagar às cegas.
_ALCANCE_MAXIMO = 500


def _motivo_cifrado(codigo: str, texto: str | None, **extra) -> str:
    return cifrar_pii(
        json.dumps({"codigo": codigo, "texto": texto or None, **extra}, ensure_ascii=False)
    )


def _ler_motivo(cifrado: str | None) -> dict:
    """O motivo guardado na linha. Os antigos eram só o texto de um pedido do titular."""
    claro = decifrar_pii(cifrado) if cifrado else None
    if not claro:
        return {}
    try:
        dados = json.loads(claro)
        if isinstance(dados, dict):
            return dados
    except ValueError:
        pass
    return {"codigo": "pedido_titular", "texto": claro}


def _tem_conteudo(evidencia: Evidencia) -> bool:
    # Uma linha eliminada por uma versão anterior da aplicação pode ter ficado
    # com o texto da nota ou o nome do ficheiro: ainda tem o que apagar.
    return evidencia.deleted_at is None or any(
        (evidencia.conteudo_texto, evidencia.ficheiro_path,
         evidencia.ficheiro_nome, evidencia.titulo)
    )


def _percorrer(
    db: Session, evidencia: Evidencia, incluir_copias: bool
) -> list[Evidencia]:
    """A cadeia de versões (para trás e para a frente) e, se pedido, as cópias
    com o mesmo conteúdo e as cadeias delas. Inclui linhas já apagadas: uma
    versão do meio que foi reciclada continua a ser o elo entre as outras."""
    empresa_id = evidencia.empresa_id
    vistas: dict[uuid.UUID, Evidencia] = {evidencia.id: evidencia}
    fila = [evidencia]
    while fila:
        atual = fila.pop()
        vizinhas: list[Evidencia] = []
        if atual.substitui_id:
            anterior = db.get(Evidencia, atual.substitui_id)
            if anterior is not None:
                vizinhas.append(anterior)
        vizinhas.extend(
            db.exec(select(Evidencia).where(Evidencia.substitui_id == atual.id)).all()
        )
        if incluir_copias and atual.conteudo_hash:
            vizinhas.extend(
                db.exec(
                    select(Evidencia).where(
                        Evidencia.empresa_id == empresa_id,
                        Evidencia.conteudo_hash == atual.conteudo_hash,
                    )
                ).all()
            )
        for vizinha in vizinhas:
            if vizinha.empresa_id != empresa_id or vizinha.id in vistas:
                continue
            vistas[vizinha.id] = vizinha
            fila.append(vizinha)
            if len(vistas) > _ALCANCE_MAXIMO:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Esta evidência tem demasiadas versões e cópias para apagar de uma vez.",
                )
    return sorted(vistas.values(), key=lambda ev: ev.created_at)


def _exigir_eliminar(utilizador: Utilizador) -> None:
    """Gate do papel, antes de procurar registos: quem não pode apagar não deve
    descobrir, pela diferença entre 403 e 404, que evidências existem."""
    if ambito_de(utilizador, "evidencias", ClasseAcao.ELIMINAR) not in (
        Ambito.TOTAL, Ambito.ATRIBUIDO,
    ):
        exigir_ambito(utilizador, "evidencias", ClasseAcao.ELIMINAR, None)


def _exigir_apagavel(
    evidencia: Evidencia,
    utilizador: Utilizador,
    ligadas: int,
    motivos: set[str],
    request: Request | None,
) -> None:
    """Pode esta evidência ser apagada de vez, por esta pessoa? Levanta se não."""
    # O dono de uma órfã é quem a carregou.
    exigir_ambito(utilizador, "evidencias", ClasseAcao.ELIMINAR, evidencia.uploaded_by_id)
    if ligadas:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": "evidencia_ligada",
                "controlos": ligadas,
                "mensagem": traduzir(
                    MsgsI18n.EVIDENCIA_LIGADA_NAO_APAGA,
                    locale_de_request(request),
                    n=ligadas,
                ),
            },
        )
    if motivos:
        ordenados = sorted(motivos)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": "evidencia_retida",
                "motivos": ordenados,
                "mensagem": traduzir(
                    MsgsI18n.EVIDENCIA_RETIDA,
                    locale_de_request(request),
                    motivos=", ".join(ordenados),
                ),
            },
        )


def _validar_razao(razao: str, texto: str | None) -> str | None:
    if razao not in RAZOES_DEFINITIVO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Razão de apagamento inválida.",
        )
    limpo = (texto or "").strip() or None
    if razao == "outro" and len(limpo or "") < _TEXTO_MINIMO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Descreva a razão do apagamento (mínimo 10 caracteres).",
        )
    return limpo


def _preparar_apagamento_definitivo(
    db: Session, evidencia_ids: list[uuid.UUID], empresa_id: uuid.UUID
) -> tuple[dict[uuid.UUID, Evidencia], dict[uuid.UUID, int], dict[uuid.UUID, set[str]]]:
    encontradas = {
        ev.id: ev
        for ev in db.exec(
            select(Evidencia).where(
                Evidencia.id.in_(evidencia_ids),
                Evidencia.empresa_id == empresa_id,
                Evidencia.deleted_at.is_(None),
            )
        ).all()
    }
    totais = ligacoes.contar_ligacoes_ativas_em_lote(db, list(encontradas))
    motivos = retencao.motivos_em_lote(
        db, empresa_id, [i for i in encontradas if not totais.get(i)]
    )
    return encontradas, totais, motivos


def _executar_apagamento_definitivo(
    db: Session,
    evidencias: list[Evidencia],
    utilizador: Utilizador,
    razao: str,
    texto: str | None,
    request: Request | None,
) -> list[uuid.UUID]:
    agora = datetime.now(timezone.utc)
    ids = [ev.id for ev in evidencias]
    ficheiros: list[str | None] = []
    for evidencia in evidencias:
        ficheiros.append(apagamentos.limpar_conteudo(evidencia, agora))
        evidencia.eliminacao_por_id = utilizador.id
        evidencia.eliminacao_motivo = _motivo_cifrado(razao, texto)
        db.add(evidencia)
        registar_acao(
            db,
            acao=Acao.EVIDENCIA_APAGADA_DEFINITIVAMENTE,
            resultado=ResultadoAcao.SUCESSO,
            empresa_id=evidencia.empresa_id,
            utilizador_id=utilizador.id,
            entidade_tipo="Evidencia",
            entidade_id=evidencia.id,
            dados_novos={
                # O mesmo `conteudo_hash` que o carregamento registou: é o que
                # prova, pela trilha, que o que saiu é o que tinha entrado.
                "conteudo_hash": evidencia.conteudo_hash,
                "razao": razao,
                "tem_texto": bool(texto),
            },
            request=request,
        )
    apagamentos.registar(
        evidencias, tipo=apagamentos.TIPO_DEFINITIVO, codigo=razao, por_id=utilizador.id
    )
    db.commit()
    apagamentos.remover_ficheiros(ficheiros)
    return ids


def apagar_definitivamente(
    db: Session,
    evidencia_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
    razao: str,
    texto: str | None = None,
    request: Request | None = None,
) -> None:
    """Apaga já, e de vez, uma órfã que nunca foi prova. Sem lápide.

    É o que resolve "carreguei o ficheiro errado e não posso esperar 30 dias": o
    ficheiro, o texto da nota, o nome do ficheiro e o título saem no momento.
    Só da reciclagem (409 se ainda estiver ligada) e nunca sobre prova retida
    (409 com os motivos) — essa sai pelo apagamento com lápide.
    """
    _exigir_eliminar(utilizador)
    texto_limpo = _validar_razao(razao, texto)

    encontradas, totais, motivos = _preparar_apagamento_definitivo(
        db, [evidencia_id], empresa_id
    )
    evidencia = encontradas.get(evidencia_id)
    if evidencia is None:
        raise HTTPException(status_code=404, detail="Evidência não encontrada.")
    _exigir_apagavel(
        evidencia, utilizador, totais.get(evidencia.id, 0),
        motivos.get(evidencia.id, set()), request,
    )
    _executar_apagamento_definitivo(db, [evidencia], utilizador, razao, texto_limpo, request)


def apagar_definitivamente_lote(
    db: Session,
    evidencia_ids: list[uuid.UUID],
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
    razao: str,
    texto: str | None = None,
    request: Request | None = None,
) -> ResultadoApagamentoSchema:
    """O mesmo, para várias órfãs com a mesma razão.

    Cada uma é verificada por si: uma retida, ligada ou alheia no meio do lote
    é recusada e as outras seguem. A resposta diz quais saíram e quais não.
    """
    _exigir_eliminar(utilizador)
    texto_limpo = _validar_razao(razao, texto)

    pedidos = list(dict.fromkeys(evidencia_ids))
    encontradas, totais, motivos = _preparar_apagamento_definitivo(db, pedidos, empresa_id)

    aceites: list[Evidencia] = []
    recusadas: list[RecusaApagamentoSchema] = []
    for evidencia_id in pedidos:
        evidencia = encontradas.get(evidencia_id)
        if evidencia is None:
            recusadas.append(RecusaApagamentoSchema(id=evidencia_id, codigo="nao_encontrada"))
            continue
        try:
            _exigir_apagavel(
                evidencia, utilizador, totais.get(evidencia.id, 0),
                motivos.get(evidencia.id, set()), request,
            )
        except HTTPException as recusa:
            detalhe = recusa.detail if isinstance(recusa.detail, dict) else {}
            recusadas.append(
                RecusaApagamentoSchema(
                    id=evidencia_id,
                    codigo=(
                        detalhe.get("codigo", "recusada")
                        if recusa.status_code == status.HTTP_409_CONFLICT
                        else "sem_permissao"
                    ),
                    motivos=detalhe.get("motivos", []),
                )
            )
            continue
        aceites.append(evidencia)

    apagadas = (
        _executar_apagamento_definitivo(db, aceites, utilizador, razao, texto_limpo, request)
        if aceites
        else []
    )
    return ResultadoApagamentoSchema(apagadas=apagadas, recusadas=recusadas)


def apagar_com_lapide(
    db: Session,
    evidencia_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
    fundamento: str,
    texto: str,
    sem_obrigacao_conservar: bool | None,
    confirmado: bool = False,
    request: Request | None = None,
) -> None:
    """Apaga prova — retida ou ainda ligada — com fundamento. Irreversível.

    É o único caminho que atravessa a retenção. Sai o conteúdo de toda a cadeia
    de versões e das cópias com o mesmo conteúdo (um pedido de apagamento que só
    alcançasse uma versão ficava por cumprir); fica a lápide, sem título, que
    diz que existiu, quando saiu, por ordem de quem e com que fundamento.

    Sem `confirmado`, responde 409 com o impacto: as versões e cópias que saem,
    os controlos que ficam sem prova e os dossiês exportados que a podem ter
    levado (a quem os recebeu tem de se comunicar o apagamento).

    `sem_obrigacao_conservar` é a confirmação de que não se aplica uma obrigação
    legal de conservar esta prova nem ela é necessária num litígio. `None` só
    vem da forma antiga do pedido, que não a perguntava.
    """
    exigir_ambito(utilizador, "evidencias", ClasseAcao.ELIMINAR, None)

    # A administração da instalação é quem responde por apagar prova: é
    # irreversível e atravessa a retenção.
    if utilizador.role not in (RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN):
        from app.shared.audit import registar_negacao

        registar_negacao(
            utilizador, modulo="evidencias", acao="eliminar",
            codigo="sem_permissao", request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Só a administração pode apagar uma evidência com lápide.",
        )

    if fundamento not in FUNDAMENTOS_LAPIDE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Fundamento de apagamento inválido.",
        )
    texto_limpo = (texto or "").strip()
    if len(texto_limpo) < _TEXTO_MINIMO:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Indique o motivo do apagamento (mínimo 10 caracteres).",
        )

    origem = _get_evidencia_or_404(db, evidencia_id, empresa_id)
    alcance = [ev for ev in _percorrer(db, origem, incluir_copias=True) if _tem_conteudo(ev)]
    afetados = {
        ligacao.requisito_id
        for ev in alcance
        for ligacao in ligacoes.requisitos_de(db, ev.id)
    }
    desde = min(ev.created_at for ev in alcance)
    if desde.tzinfo is None:
        desde = desde.replace(tzinfo=timezone.utc)
    dossies = [
        dossie
        for dossie in retencao.dossies_com_ficheiros(db, empresa_id)
        if (
            dossie.criado_em if dossie.criado_em.tzinfo
            else dossie.criado_em.replace(tzinfo=timezone.utc)
        ) >= desde
    ]

    # O impacto vai à frente do ato: quem apaga tem de ver o que sai e o que
    # fica sem prova ANTES de confirmar, e não a seguir.
    if not confirmado:
        contexto = _titulos_dos_controlos(db, empresa_id, afetados)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": "confirmar_apagamento",
                "controlos": [
                    contexto.get(ce_id, (str(ce_id), None, None))[0] for ce_id in afetados
                ],
                "versoes": [
                    {
                        "id": str(ev.id),
                        "titulo": ev.titulo if ev.deleted_at is None else None,
                        "criado_em": ev.created_at.isoformat(),
                        "origem": ev.id == origem.id,
                    }
                    for ev in alcance
                ],
                "dossies": [
                    {"id": str(d.id), "criado_em": d.criado_em.isoformat()} for d in dossies
                ],
                "mensagem": traduzir(
                    MsgsI18n.EVIDENCIA_APAGAMENTO_IMPACTO,
                    locale_de_request(request),
                    v=len(alcance),
                    n=len(afetados),
                    d=len(dossies),
                ),
            },
        )

    if sem_obrigacao_conservar is False:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Confirme que não se aplica uma obrigação legal de conservar esta "
                "prova nem ela é necessária num litígio."
            ),
        )

    agora = datetime.now(timezone.utc)
    motivo = _motivo_cifrado(
        fundamento, texto_limpo, sem_obrigacao_conservar=sem_obrigacao_conservar
    )
    ficheiros: list[str | None] = []
    for evidencia in alcance:
        controlos_desta = ligacoes.desligar_todas(db, evidencia.id)
        ficheiros.append(apagamentos.limpar_conteudo(evidencia, agora))
        evidencia.eliminacao_rgpd = True
        evidencia.eliminacao_por_id = utilizador.id
        evidencia.eliminacao_motivo = motivo
        db.add(evidencia)
        registar_acao(
            db,
            acao=Acao.EVIDENCIA_APAGADA_COM_LAPIDE,
            resultado=ResultadoAcao.SUCESSO,
            empresa_id=empresa_id,
            utilizador_id=utilizador.id,
            entidade_tipo="Evidencia",
            entidade_id=evidencia.id,
            dados_novos={
                # A impressão digital do que foi apagado é a prova de que a
                # lápide se refere àquele conteúdo — e não obriga a guardá-lo.
                "conteudo_hash": evidencia.conteudo_hash,
                "fundamento": fundamento,
                "sem_obrigacao_conservar": sem_obrigacao_conservar,
                "controlos_afetados": controlos_desta,
                "pedido_sobre": str(origem.id),
                "evidencias_no_apagamento": len(alcance),
                "dossies_possiveis": len(dossies),
            },
            request=request,
        )
    apagamentos.registar(
        alcance, tipo=apagamentos.TIPO_LAPIDE, codigo=fundamento, por_id=utilizador.id
    )
    db.commit()
    apagamentos.remover_ficheiros(ficheiros)


def apagar_rgpd(
    db: Session,
    evidencia_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
    motivo: str,
    confirmado: bool = False,
    request: Request | None = None,
) -> None:
    """Forma antiga do apagamento com lápide: só «pedido do titular»."""
    apagar_com_lapide(
        db, evidencia_id, empresa_id, utilizador,
        fundamento="pedido_titular",
        texto=motivo,
        sem_obrigacao_conservar=None,
        confirmado=confirmado,
        request=request,
    )


def lapide_de(
    db: Session, evidencia_id: uuid.UUID, empresa_id: uuid.UUID, utilizador: Utilizador
) -> dict:
    """O que sobra de uma prova apagada com lápide.

    Existe para a história não apontar para o nada: quem seguir uma referência a
    esta prova encontra a lápide, e não um 404 que se lê como prova perdida.
    Sem título. O texto do motivo só o vê a administração: pode identificar
    quem pediu o apagamento.
    """
    exigir_ambito(utilizador, "evidencias", ClasseAcao.VER, None)

    evidencia = db.exec(
        select(Evidencia).where(
            Evidencia.id == evidencia_id,
            Evidencia.empresa_id == empresa_id,
            Evidencia.eliminacao_rgpd.is_(True),
        )
    ).first()
    if evidencia is None:
        raise HTTPException(status_code=404, detail="Evidência não encontrada.")

    motivo = _ler_motivo(evidencia.eliminacao_motivo)
    administracao = utilizador.role in (RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN)
    autor = db.get(Utilizador, evidencia.eliminacao_por_id) if evidencia.eliminacao_por_id else None
    return {
        "id": evidencia.id,
        "conteudo_hash": evidencia.conteudo_hash,
        "eliminada_em": evidencia.deleted_at,
        "eliminada_por_nome": decifrar_pii(autor.nome) if autor else None,
        "fundamento": motivo.get("codigo"),
        "motivo": motivo.get("texto") if administracao else None,
    }


def listar_versoes(
    db: Session,
    evidencia_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
) -> ListaVersoesSchema:
    """A cadeia de versões de uma evidência, da mais antiga para a mais recente.

    É por aqui que se chega às versões anteriores — para ver o que sustentava um
    controlo em março, ou para as apagar com lápide.
    """
    evidencia = _get_evidencia_or_404(db, evidencia_id, empresa_id)
    _verificar_acesso_ou_orfa(db, evidencia, utilizador)

    cadeia = _percorrer(db, evidencia, incluir_copias=False)
    vivas = [ev.id for ev in cadeia if ev.deleted_at is None]
    totais = ligacoes.contar_ligacoes_ativas_em_lote(db, vivas)
    motivos = retencao.motivos_em_lote(db, empresa_id, vivas)
    autores = {
        u.id: decifrar_pii(u.nome)
        for u in db.exec(
            select(Utilizador).where(
                Utilizador.id.in_({ev.uploaded_by_id for ev in cadeia})
            )
        ).all()
    }

    versoes = []
    for ev in cadeia:
        if ev.eliminacao_rgpd:
            estado = "lapide"
        elif ev.deleted_at is not None:
            estado = "apagada"
        elif totais.get(ev.id):
            estado = "ligada"
        else:
            estado = "orfa"
        versoes.append(
            VersaoSchema(
                id=ev.id,
                titulo=ev.titulo if ev.deleted_at is None else None,
                criado_em=ev.created_at,
                substituida_em=ev.substituida_em,
                estado=estado,
                total_ligacoes=totais.get(ev.id, 0),
                motivos_retencao=sorted(motivos.get(ev.id, ())),
                uploaded_by_nome=autores.get(ev.uploaded_by_id),
                atual=ev.id == evidencia.id,
            )
        )
    return ListaVersoesSchema(total=len(versoes), versoes=versoes)


def restaurar_evidencia(
    db: Session,
    evidencia_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador: Utilizador,
    request: Request | None = None,
) -> RestauroSchema:
    """Tira da reciclagem: volta a ligar a evidência aos controlos de onde saiu.

    "De onde saiu" é o último gesto que a deixou órfã — as ligações não se
    apagam, fecham-se, e é por isso que o sistema sabe e não tem de perguntar.
    Volta com a nota de âmbito que tinha em cada um.

    Cada controlo é verificado por si: onde quem restaura já não pode operar, a
    evidência não volta e a resposta diz porquê; os outros seguem.
    """
    evidencia = _get_evidencia_or_404(db, evidencia_id, empresa_id)
    # Primeiro o acesso, depois o estado: quem não alcança a evidência não
    # deve saber, pela resposta, se ela está ou não na reciclagem.
    _verificar_acesso_ou_orfa(db, evidencia, utilizador)

    if not ligacoes.esta_orfa(db, evidencia.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": "nao_esta_na_reciclagem",
                "mensagem": traduzir(
                    MsgsI18n.EVIDENCIA_NAO_ESTA_NA_RECICLAGEM, locale_de_request(request)
                ),
            },
        )

    # Substituída: a versão nova já sustenta esses controlos, e restaurar esta
    # duplicaria a prova. O ecrã encaminha para as versões.
    sucessora = db.exec(
        select(Evidencia).where(Evidencia.substitui_id == evidencia.id)
    ).first()
    if sucessora is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": "versao_substituida",
                "evidencia_id": str(sucessora.id),
                "mensagem": traduzir(
                    MsgsI18n.EVIDENCIA_VERSAO_SUBSTITUIDA, locale_de_request(request)
                ),
            },
        )

    origem = ligacoes.ultimas_desligadas(db, evidencia.id)
    if not origem:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": "sem_origem",
                "mensagem": traduzir(
                    MsgsI18n.EVIDENCIA_SEM_ORIGEM, locale_de_request(request)
                ),
            },
        )

    contexto = _titulos_dos_controlos(db, empresa_id, {l.requisito_id for l in origem})
    restaurados: list[LigacaoSchema] = []
    nao_restaurados: list[NaoRestauradoSchema] = []
    for antiga in origem:
        codigo, titulo, estado = contexto.get(antiga.requisito_id, (None, None, None))
        ce = db.get(ControloEmpresaV2, antiga.requisito_id)
        if ce is None or ce.empresa_id != empresa_id:
            nao_restaurados.append(NaoRestauradoSchema(
                requisito_id=antiga.requisito_id, controlo_codigo=codigo,
                motivo="controlo_inexistente",
            ))
            continue
        try:
            _verificar_acesso_escrita_evidencias(ce, utilizador)
        except HTTPException:
            nao_restaurados.append(NaoRestauradoSchema(
                requisito_id=antiga.requisito_id, controlo_codigo=codigo,
                motivo="sem_permissao",
            ))
            continue
        nova = ligacoes.ligar(
            db,
            evidencia_id=evidencia.id,
            requisito_id=antiga.requisito_id,
            empresa_id=empresa_id,
            ligado_por_id=utilizador.id,
            nota_ambito=antiga.nota_ambito,
            ambito_por_confirmar=antiga.ambito_por_confirmar,
        )
        restaurados.append(LigacaoSchema(
            requisito_id=antiga.requisito_id,
            controlo_codigo=codigo,
            controlo_titulo=titulo,
            controlo_estado=estado,
            nota_ambito=nova.nota_ambito,
            ambito_por_confirmar=nova.ambito_por_confirmar,
            ligado_em=nova.ligado_em,
            ligado_por_nome=decifrar_pii(utilizador.nome),
        ))

    if not restaurados:
        from app.shared.audit import registar_negacao

        registar_negacao(
            utilizador, modulo="evidencias", acao="operar",
            codigo="sem_permissao_origem", request=request,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "codigo": "sem_permissao_origem",
                "controlos": [n.controlo_codigo for n in nao_restaurados],
                "mensagem": traduzir(
                    MsgsI18n.EVIDENCIA_SEM_PERMISSAO_ORIGEM, locale_de_request(request)
                ),
            },
        )

    registar_acao(
        db,
        acao=Acao.EVIDENCIA_RESTAURADA,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=utilizador.id,
        entidade_tipo="Evidencia",
        entidade_id=evidencia.id,
        dados_novos={
            "conteudo_hash": evidencia.conteudo_hash,
            "controlos_restaurados": [str(r.requisito_id) for r in restaurados],
            "controlos_nao_restaurados": len(nao_restaurados),
        },
        request=request,
    )
    db.commit()
    return RestauroSchema(restaurados=restaurados, nao_restaurados=nao_restaurados)
