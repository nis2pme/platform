"""
Router do módulo de evidências.
"""
from __future__ import annotations

import logging
import uuid

from typing import Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status,
)
from fastapi.responses import FileResponse, Response
from sqlmodel import Session

from app.database import get_session
from app.evidencias import service
from app.evidencias.schemas import (
    ApagamentoComLapidePedido,
    ApagamentoRgpdPedido,
    ApagarDefinitivamenteLotePedido,
    ApagarDefinitivamentePedido,
    EvidenciaSchema,
    LapideSchema,
    LigacaoSchema,
    LigarPedido,
    MetadadosPedido,
    ListaEvidenciasSchema,
    ListaLigacoesSchema,
    ListaOrfasSchema,
    ListaTodasEvidenciasSchema,
    ListaVersoesSchema,
    NotaAmbitoPedido,
    RestauroSchema,
    ResultadoApagamentoSchema,
)
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, get_empresa_ativa
from app.shared.utils import content_disposition_anexo

# Quem pode a ação; sobre que evidência em concreto decide-se no service, que
# segue o dono do controlo a que a evidência pertence.
VerDep = Depends(require_capability("evidencias", ClasseAcao.VER))
OperarDep = Depends(require_capability("evidencias", ClasseAcao.OPERAR))
EliminarDep = Depends(require_capability("evidencias", ClasseAcao.ELIMINAR))

# Mesmo chão de leitura do router de controlos, e pela mesma razão: a eliminação
# de uma evidência exigia apenas ELIMINAR, e chegou a ser possível apagá-la sem
# a poder ver. A política já não deixa configurar isso; o chão fecha o caminho
# também do lado da rota.
logger = logging.getLogger(__name__)

router = APIRouter(tags=["Evidências"], dependencies=[VerDep])


# ---------------------------------------------------------------------------
# GET /evidencias  (listagem global da empresa)
# ---------------------------------------------------------------------------

@router.get(
    "/evidencias",
    response_model=ListaTodasEvidenciasSchema,
    summary="Listar todas as evidências da empresa",
    dependencies=[VerDep],
)
def listar_todas_evidencias(
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Devolve todas as evidências activas da empresa, enriquecidas com código/objetivo do controlo.

    Sem o texto das notas (só o resumo das que não têm título): o conteúdo lê-se
    no detalhe, que fica na trilha."""
    empresa = get_empresa_ativa(db, utilizador)
    return service.listar_todas_evidencias(db, empresa.id, utilizador)



# ---------------------------------------------------------------------------
# GET /controlos/{controlo_empresa_id}/evidencias
# ---------------------------------------------------------------------------

@router.get(
    "/controlos/{controlo_empresa_id}/evidencias",
    response_model=ListaEvidenciasSchema,
    summary="Listar evidências de um controlo",
    dependencies=[VerDep],
)
def listar_evidencias(
    controlo_empresa_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
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
    dependencies=[OperarDep],
)
def criar_evidencia(
    controlo_empresa_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    titulo: str = Form(...),
    conteudo_texto: Optional[str] = Form(None),
    ficheiro: Optional[UploadFile] = File(None),
    conteudo_hash: Optional[str] = Form(None),
    evitar_duplicado: bool = Form(False),
    ligar_existente: Optional[bool] = Form(None),
    db: Session = Depends(get_session, scope="function"),
):
    """
    Cria uma evidência para o controlo indicado.
    Modes suportados:
    - Nota de texto: preencher `conteudo_texto`
    - Ficheiro: enviar `ficheiro` (PDF, Word, Excel, PNG, JPEG; max 10 MB)
    - Ambos: preencher `conteudo_texto` E enviar `ficheiro`
    O campo `titulo` é obrigatório e identifica a evidência.

    Deduplicação (usada pelo "anexar como evidência"): `conteudo_hash` fixa a
    impressão digital estável do conteúdo (para documentos gerados, cujo ficheiro
    muda a cada geração); `evitar_duplicado=true` faz o servidor devolver a
    evidência existente (sem criar cópia) se já houver uma igual neste controlo.

    Conteúdo repetido no envio manual: se a empresa já tiver este ficheiro,
    responde **409** com o identificador da evidência existente, para o ecrã
    poder oferecer ligá-la a este controlo em vez de guardar outra cópia.
    `ligar_existente=true` liga; `ligar_existente=false` guarda mesmo a cópia.
    """
    empresa = get_empresa_ativa(db, utilizador)
    return service.criar_evidencia(
        db,
        controlo_empresa_id,
        empresa.id,
        titulo=titulo,
        conteudo_texto=conteudo_texto,
        ficheiro=ficheiro if (ficheiro and ficheiro.filename) else None,
        utilizador=utilizador,
        request=request,
        evitar_duplicado=evitar_duplicado,
        conteudo_hash_ext=(conteudo_hash or None),
        ligar_existente=ligar_existente,
    )


# ---------------------------------------------------------------------------
# GET /evidencias/orfas  (reciclagem)
#
# TEM de ficar antes de `/evidencias/{evidencia_id}`: o FastAPI serve a primeira
# rota que casa, e a de identificador engoliria «orfas» — o pedido rebentaria com
# um 422 a queixar-se de um UUID inválido, num sítio onde ninguém escreveu um.
# ---------------------------------------------------------------------------

@router.get(
    "/evidencias/orfas",
    response_model=ListaOrfasSchema,
    summary="Evidências sem controlo associado (reciclagem)",
    dependencies=[VerDep],
)
def listar_orfas(
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Provas que deixaram de sustentar controlo nenhum.

    Ficam na lista `EVIDENCIA_ORFA_DIAS` antes de serem apagadas — a contar na
    quota, para haver motivo para a esvaziar, e religáveis a qualquer momento. As
    que já foram prova de um controlo, ou que podem ter saído num dossiê,
    ficam retidas e a varredura não lhes toca.
    """
    empresa = get_empresa_ativa(db, utilizador)
    return service.listar_orfas(db, empresa.id, utilizador)


@router.post(
    "/evidencias/apagamento-definitivo",
    response_model=ResultadoApagamentoSchema,
    summary="Apagar de vez várias órfãs da reciclagem (irreversível)",
    dependencies=[EliminarDep],
)
def apagar_definitivamente_lote(
    pedido: ApagarDefinitivamenteLotePedido,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """A mesma razão para o lote; cada evidência é verificada por si.

    Só órfãs que nunca foram prova. As ligadas, retidas ou que o utilizador não
    pode apagar vêm em `recusadas`, com o código; as outras saem.
    """
    empresa = get_empresa_ativa(db, utilizador)
    return service.apagar_definitivamente_lote(
        db, pedido.evidencia_ids, empresa.id, utilizador,
        razao=pedido.razao, texto=pedido.texto, request=request,
    )


# ---------------------------------------------------------------------------
# GET /evidencias/{evidencia_id}
# ---------------------------------------------------------------------------

@router.get(
    "/evidencias/{evidencia_id}",
    response_model=EvidenciaSchema,
    summary="Obter evidência",
    dependencies=[VerDep],
)
def get_evidencia(
    evidencia_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Devolve os metadados de uma evidência e, numa nota, o texto.

    O texto é conteúdo: quando a resposta o leva, fica na trilha que foi visto.
    Os metadados de um ficheiro não contam (o ficheiro vê-se pelo download)."""
    empresa = get_empresa_ativa(db, utilizador)
    evidencia = service.get_evidencia(db, evidencia_id, empresa.id, utilizador)
    if evidencia.conteudo_texto is not None:
        service.registar_acesso_conteudo(
            db, evidencia_id, empresa.id, utilizador,
            descarregada=False, parte="texto", request=request,
        )
        db.commit()
    return evidencia


# ---------------------------------------------------------------------------
# GET /evidencias/{evidencia_id}/download
# ---------------------------------------------------------------------------

@router.get(
    "/evidencias/{evidencia_id}/download",
    summary="Download de ficheiro de evidência",
    dependencies=[VerDep],
    response_class=FileResponse,
)
def download_evidencia(
    evidencia_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    ver: bool = False,
    db: Session = Depends(get_session, scope="function"),
):
    """
    Serve o ficheiro de uma evidência para download.
    O ficheiro é servido directamente pelo servidor (não via URL pública).

    Fica na trilha, antes de o conteúdo sair: `evidencia.descarregada`, ou
    `evidencia.visualizada` com `ver=true` (a pré-visualização no ecrã).
    """
    empresa = get_empresa_ativa(db, utilizador)
    path, nome, mime, cifrado = service.get_evidencia_ficheiro_path(
        db, evidencia_id, empresa.id, utilizador
    )

    def _registar() -> None:
        service.registar_acesso_conteudo(
            db, evidencia_id, empresa.id, utilizador,
            descarregada=not ver, parte="ficheiro", request=request,
        )
        db.commit()

    if cifrado:
        # Desencripta em memória e responde de uma vez. Um StreamingResponse sobre
        # um BytesIO mandava o ficheiro linha a linha (~4 000 envios por MiB de
        # binário), sem poupar memória nenhuma: os bytes já estão todos aqui.
        with open(path, "rb") as f:
            dados_cifrados = f.read()
        try:
            dados_limpos = service.decifrar_bytes_evidencia(dados_cifrados)
        except service.EvidenciaIlegivel:
            logger.error("Evidência %s não decifra com a chave desta instalação.", evidencia_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=service.MSG_EVIDENCIA_ILEGIVEL,
            )
        # Só depois de decifrado: um ficheiro que não sai não conta como visto.
        _registar()
        return Response(
            content=dados_limpos,
            media_type=mime,
            headers={"Content-Disposition": content_disposition_anexo(nome)},
        )
    _registar()
    return FileResponse(
        path=path,
        media_type=mime,
        headers={"Content-Disposition": content_disposition_anexo(nome)},
    )


# ---------------------------------------------------------------------------
# Metadados e versões
#
# Dois atos que é tentador confundir e não devem sê-lo: corrigir uma gralha no
# título não é uma versão nova (o conteúdo não mudou); substituir o ficheiro é.
# Juntos, uma gralha deixava sete versões da mesma política para trás.
# ---------------------------------------------------------------------------

@router.patch(
    "/evidencias/{evidencia_id}",
    response_model=EvidenciaSchema,
    summary="Corrigir título/validade (sem criar versão)",
    dependencies=[OperarDep],
)
def atualizar_metadados(
    evidencia_id: uuid.UUID,
    pedido: MetadadosPedido,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    empresa = get_empresa_ativa(db, utilizador)
    return service.atualizar_metadados(
        db, evidencia_id, empresa.id, utilizador,
        titulo=pedido.titulo, valido_ate=pedido.valido_ate, request=request,
    )


@router.post(
    "/evidencias/{evidencia_id}/versoes",
    response_model=EvidenciaSchema,
    status_code=status.HTTP_201_CREATED,
    summary="Substituir o conteúdo: cria a versão seguinte",
    dependencies=[OperarDep],
)
def criar_versao(
    evidencia_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    titulo: Optional[str] = Form(None),
    conteudo_texto: Optional[str] = Form(None),
    ficheiro: Optional[UploadFile] = File(None),
    requisitos: Optional[str] = Form(None),
    db: Session = Depends(get_session, scope="function"),
):
    """A versão nova herda as ligações escolhidas; as outras ficam na anterior.

    `requisitos` é uma lista de identificadores separados por vírgula. Omitir
    propaga a **todos** os controlos que a evidência sustenta, que é o caso
    comum; enviar um subconjunto deixa os restantes na versão anterior — é o que
    permite uma revisão que reduz o âmbito não piorar a conformidade em silêncio.
    """
    empresa = get_empresa_ativa(db, utilizador)

    alvos: list[uuid.UUID] | None = None
    if requisitos is not None:
        try:
            alvos = [uuid.UUID(r.strip()) for r in requisitos.split(",") if r.strip()]
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Lista de controlos inválida.",
            )

    return service.criar_versao(
        db, evidencia_id, empresa.id, utilizador,
        titulo=titulo,
        conteudo_texto=conteudo_texto,
        ficheiro=ficheiro if (ficheiro and ficheiro.filename) else None,
        requisitos=alvos,
        request=request,
    )


@router.get(
    "/evidencias/{evidencia_id}/versoes",
    response_model=ListaVersoesSchema,
    summary="Cadeia de versões desta evidência",
    dependencies=[VerDep],
)
def listar_versoes(
    evidencia_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Da mais antiga para a mais recente, incluindo as já apagadas (sem título)."""
    empresa = get_empresa_ativa(db, utilizador)
    return service.listar_versoes(db, evidencia_id, empresa.id, utilizador)


# ---------------------------------------------------------------------------
# Ligações evidência ↔ controlo
#
# A mesma prova sustenta vários controlos. Ligar e desligar são atos próprios e
# não variantes de criar/apagar: desligar tira a prova de UM controlo e deixa a
# evidência viva nos outros. Sem ligação nenhuma, vai para a reciclagem.
# ---------------------------------------------------------------------------

@router.get(
    "/evidencias/{evidencia_id}/ligacoes",
    response_model=ListaLigacoesSchema,
    summary="Controlos que esta evidência sustenta",
    dependencies=[VerDep],
)
def listar_ligacoes(
    evidencia_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    empresa = get_empresa_ativa(db, utilizador)
    return service.listar_ligacoes(db, evidencia_id, empresa.id, utilizador)


@router.post(
    "/evidencias/{evidencia_id}/ligacoes",
    response_model=LigacaoSchema,
    status_code=status.HTTP_201_CREATED,
    summary="Ligar esta evidência a mais um controlo",
    dependencies=[OperarDep],
)
def ligar_evidencia(
    evidencia_id: uuid.UUID,
    pedido: LigarPedido,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Uma cópia em disco, várias ligações — a quota é consumida uma vez só.

    O direito exigido é sobre o controlo **de destino**: ver a evidência não
    chega para a poder pendurar num controlo de outra pessoa.
    """
    empresa = get_empresa_ativa(db, utilizador)
    return service.ligar_evidencia(
        db, evidencia_id, pedido.requisito_id, empresa.id, utilizador,
        nota_ambito=pedido.nota_ambito, request=request,
    )


@router.post(
    "/evidencias/{evidencia_id}/restaurar",
    response_model=RestauroSchema,
    summary="Tirar da reciclagem: volta aos controlos de onde saiu",
    dependencies=[OperarDep],
)
def restaurar_evidencia(
    evidencia_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Volta a ligar a evidência aos controlos que a perderam no último
    «retirar», com a nota de âmbito que tinha em cada um.

    **409** `nao_esta_na_reciclagem`, `versao_substituida` (com o id da versão
    nova) ou `sem_origem`; **403** `sem_permissao_origem` se quem restaura não
    opera nenhum desses controlos. Os que não voltam vêm em `nao_restaurados`.
    """
    empresa = get_empresa_ativa(db, utilizador)
    return service.restaurar_evidencia(db, evidencia_id, empresa.id, utilizador, request)


@router.delete(
    "/evidencias/{evidencia_id}/ligacoes/{requisito_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Retirar esta evidência de um controlo",
    dependencies=[OperarDep],
)
def desligar_evidencia(
    evidencia_id: uuid.UUID,
    requisito_id: uuid.UUID,
    request: Request,
    utilizador: CurrentUserDep,
    confirmado: bool = False,
    db: Session = Depends(get_session, scope="function"),
):
    """Retira a prova deste controlo. A evidência continua a sustentar os outros.

    Se for a última prova de um controlo já declarado como implementado,
    responde **409** com o aviso; repetir com `confirmado=true` executa. Não se
    impede — impede-se que aconteça sem ninguém dar por isso.
    """
    empresa = get_empresa_ativa(db, utilizador)
    service.desligar_evidencia(
        db, evidencia_id, requisito_id, empresa.id, utilizador,
        confirmado=confirmado, request=request,
    )


@router.patch(
    "/evidencias/{evidencia_id}/ligacoes/{requisito_id}",
    response_model=LigacaoSchema,
    summary="Nota de âmbito: onde olhar neste controlo",
    dependencies=[OperarDep],
)
def atualizar_nota_ambito(
    evidencia_id: uuid.UUID,
    requisito_id: uuid.UUID,
    pedido: NotaAmbitoPedido,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """«secção 4.2», «páginas 3-5», «anexo B».

    Sem isto, o auditor abre o mesmo PDF em seis controlos sem saber onde olhar,
    e uma evidência que serve vários deixa de ser prova para passar a ruído.
    """
    empresa = get_empresa_ativa(db, utilizador)
    return service.atualizar_nota_ambito(
        db, evidencia_id, requisito_id, empresa.id, utilizador,
        nota_ambito=pedido.nota_ambito, request=request,
    )


# ---------------------------------------------------------------------------
# Saída de uma evidência
#
# Não há "eliminar" sobre uma prova viva: retira-se do controlo (com desfazer) e
# vai para a reciclagem. De lá sai de uma de duas formas:
#   * apagar definitivamente — só órfãs que nunca foram prova; irreversível e
#     imediato, com razão, sem lápide;
#   * apagamento com lápide — a administração, sobre qualquer prova (retida ou
#     ainda ligada), com fundamento; leva a cadeia de versões e deixa lápide.
# ---------------------------------------------------------------------------

@router.delete(
    "/evidencias/{evidencia_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Apagar de vez uma órfã da reciclagem (irreversível)",
    dependencies=[EliminarDep],
)
def apagar_definitivamente(
    evidencia_id: uuid.UUID,
    pedido: ApagarDefinitivamentePedido,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Apaga já o ficheiro, o texto da nota, o nome do ficheiro e o título.

    **409** `evidencia_ligada` se ainda sustentar algum controlo (retire-a
    primeiro); **409** `evidencia_retida`, com os motivos, se já tiver sido
    prova. O implementador só apaga as que carregou.
    """
    empresa = get_empresa_ativa(db, utilizador)
    service.apagar_definitivamente(
        db, evidencia_id, empresa.id, utilizador,
        razao=pedido.razao, texto=pedido.texto, request=request,
    )


@router.post(
    "/evidencias/{evidencia_id}/apagamento",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Apagar prova com lápide (irreversível, só administração)",
    dependencies=[EliminarDep],
)
def apagar_com_lapide(
    evidencia_id: uuid.UUID,
    pedido: ApagamentoComLapidePedido,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Sem `confirmado`, responde **409** com o impacto: versões e cópias que
    saem, controlos que ficam sem prova e dossiês que a podem ter levado."""
    empresa = get_empresa_ativa(db, utilizador)
    service.apagar_com_lapide(
        db, evidencia_id, empresa.id, utilizador,
        fundamento=pedido.fundamento,
        texto=pedido.texto,
        sem_obrigacao_conservar=pedido.sem_obrigacao_conservar,
        confirmado=pedido.confirmado,
        request=request,
    )


@router.post(
    "/evidencias/{evidencia_id}/apagamento-rgpd",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Forma antiga do apagamento com lápide (pedido do titular)",
    dependencies=[EliminarDep],
    deprecated=True,
)
def apagar_rgpd(
    evidencia_id: uuid.UUID,
    pedido: ApagamentoRgpdPedido,
    request: Request,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    """Mantida durante uma versão; use `POST /evidencias/{id}/apagamento`."""
    empresa = get_empresa_ativa(db, utilizador)
    service.apagar_rgpd(
        db, evidencia_id, empresa.id, utilizador,
        motivo=pedido.motivo, confirmado=pedido.confirmado, request=request,
    )


@router.get(
    "/evidencias/{evidencia_id}/lapide",
    response_model=LapideSchema,
    summary="O que resta de uma evidência apagada com lápide",
    dependencies=[VerDep],
)
def lapide(
    evidencia_id: uuid.UUID,
    utilizador: CurrentUserDep,
    db: Session = Depends(get_session, scope="function"),
):
    empresa = get_empresa_ativa(db, utilizador)
    return service.lapide_de(db, evidencia_id, empresa.id, utilizador)
