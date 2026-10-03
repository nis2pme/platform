"""
Router do módulo de Backups — apenas on-prem (montado condicionalmente no main.py).

O backup contém todos os dados e segredos da instalação, por isso há dois gates:
o do router é o chão de LEITURA — saber que cópias existem e quando correram; e
tudo o que toque no conteúdo (criar, importar, inspecionar, descarregar,
restaurar, apagar) acrescenta o de ESCRITA. Descarregar conta como escrita: leva
a instalação inteira num ficheiro.

Todas as ações ficam na auditoria (criar/descarregar/apagar/passphrase). As
rotas que usam a base são `def`: o FastAPI corre-as no threadpool, e o
pg_dump, a cifra e a trilha não bloqueiam o event loop.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import threading

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.datastructures import FormData

from app.backup import restaurar, service
from app.backup.restaurar import RestauroErro
from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, SessionDep, get_empresa_ativa
from app.shared.multipart import ficheiro_do_formulario, formulario_autorizado

router = APIRouter(
    prefix="/backups",
    tags=["Backups"],
    dependencies=[Depends(require_capability("backup", ClasseAcao.VER))],
)

_OperarBackup = Depends(require_capability("backup", ClasseAcao.OPERAR))


class PassphraseIn(BaseModel):
    passphrase: str = Field(min_length=1, max_length=200)


class CriarBackupIn(BaseModel):
    modo: str = "completo"  # "completo" | "so_db"


class AgendadoIn(BaseModel):
    ativo: bool
    hora: int = Field(ge=0, le=23)


class InspecionarIn(BaseModel):
    passphrase: str = Field(min_length=1, max_length=200)
    sem_premium: bool = False


class RestaurarIn(InspecionarIn):
    confirmacao: str = Field(max_length=20)


def _erro_restauro(erro: RestauroErro) -> HTTPException:
    """RestauroErro → 400 com código estável (a UI traduz) + mensagem PT."""
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"codigo": erro.codigo, "mensagem": str(erro)},
    )


@router.get("", summary="Listar backups guardados")
def listar(utilizador: CurrentUserDep, db: SessionDep):
    return {
        "passphrase_definida": service.passphrase_definida(),
        "agendado": service.obter_agendado(),
        "retencao": service.retencao_em_vigor(db),
        "backups": service.listar_backups(),
    }


@router.put(
    "/agendado",
    summary="Ligar/desligar o backup diário e definir a hora",
    dependencies=[_OperarBackup],
)
def definir_agendado(
    dados: AgendadoIn, request: Request, utilizador: CurrentUserDep, db: SessionDep
):
    """O backup agendado vem ligado de fábrica (diário, completo, 03:00).
    Desligá-lo é uma decisão consciente — fica registado na auditoria."""
    empresa = get_empresa_ativa(db, utilizador)
    anterior = service.obter_agendado()
    novo = service.definir_agendado(dados.ativo, dados.hora)
    registar_acao(
        db, acao=Acao.BACKUP_AGENDADO_ALTERADO, empresa_id=empresa.id,
        utilizador_id=utilizador.id, dados_anteriores=anterior, dados_novos=novo,
        request=request,
    )
    db.commit()
    return novo


@router.post(
    "/passphrase",
    summary="Definir/alterar a passphrase dos backups",
    dependencies=[_OperarBackup],
)
def definir_passphrase(
    dados: PassphraseIn, request: Request, utilizador: CurrentUserDep, db: SessionDep
):
    """Backups antigos continuam a abrir com a passphrase antiga (cada um leva no
    cabeçalho a sua identidade embrulhada). A passphrase em si nunca é guardada nem logada."""
    empresa = get_empresa_ativa(db, utilizador)
    service.definir_passphrase(dados.passphrase)
    registar_acao(
        db, acao=Acao.BACKUP_PASSPHRASE_DEFINIDA, empresa_id=empresa.id,
        utilizador_id=utilizador.id, request=request,
    )
    # A frase-secreta é da instalação: o aviso de "backups parados" sai para
    # todas as empresas, não só para a de quem a definiu.
    from sqlalchemy import text

    from app.notificacoes.catalogo import Codigo
    from app.notificacoes.service import marcar_lidas_por_chave_prefixo
    for empresa_id in db.execute(text("SELECT id FROM empresas")).scalars():
        marcar_lidas_por_chave_prefixo(
            db, prefixo=Codigo.BACKUP_SEM_PASSPHRASE, empresa_id=empresa_id,
        )
    db.commit()
    return {"ok": True}


@router.post(
    "",
    status_code=201,
    summary="Criar um backup agora",
    dependencies=[_OperarBackup],
)
def criar(
    dados: CriarBackupIn, request: Request, utilizador: CurrentUserDep, db: SessionDep
):
    """Modo `completo` (DB + evidências + segredos + .env) ou `so_db` (sem evidências —
    mais pequeno, mas um restauro só-DB pode deixar referências a ficheiros em falta)."""
    empresa = get_empresa_ativa(db, utilizador)
    resultado = service.criar_backup(db, dados.modo)
    registar_acao(
        db, acao=Acao.BACKUP_CRIADO, empresa_id=empresa.id, utilizador_id=utilizador.id,
        dados_novos=resultado, request=request,
    )
    db.commit()
    return resultado


@router.get("/restauro/estado", summary="Estado do último restauro")
def estado_restauro(utilizador: CurrentUserDep):
    """Durante um restauro a API está em manutenção (503) — quando este endpoint
    volta a responder e `pendente` é falso, a finalização terminou e o
    `relatorio` é o resultado. Persiste até ao restauro seguinte."""
    relatorio = None
    if restaurar._RELATORIO_FILE.is_file():
        try:
            relatorio = json.loads(restaurar._RELATORIO_FILE.read_text())
        except ValueError:
            relatorio = None
    return {"pendente": restaurar._PENDENTE_FILE.exists(), "relatorio": relatorio}


def _espaco_para_o_upload(request: Request) -> None:
    """Antes de receber o ficheiro: há espaço para o temporário e para a cópia?"""
    service.verificar_espaco_para_upload(request.headers.get("content-length"))


@router.post(
    "/upload",
    status_code=201,
    summary="Importar um ficheiro .nbk",
    dependencies=[_OperarBackup],
)
def importar(
    request: Request, utilizador: CurrentUserDep, db: SessionDep,
    # Por esta ordem: a capacidade (acima) e o espaço em disco decidem antes de o
    # ficheiro ser lido — com `File(...)` o FastAPI lia-o antes de tudo.
    _espaco: None = Depends(_espaco_para_o_upload),
    formulario: FormData = Depends(formulario_autorizado(max_ficheiros=1, max_campos=1)),
):
    """Guarda um backup vindo de fora (ex.: descarregado de outro servidor)
    junto dos locais — a validação a sério acontece na inspeção/restauro."""
    ficheiro = ficheiro_do_formulario(formulario)
    empresa = get_empresa_ativa(db, utilizador)
    nome = service.guardar_upload(ficheiro.file, ficheiro.filename)
    registar_acao(
        db, acao=Acao.BACKUP_IMPORTADO, empresa_id=empresa.id, utilizador_id=utilizador.id,
        dados_novos={"ficheiro": nome, "nome_original": ficheiro.filename}, request=request,
    )
    db.commit()
    return {"ficheiro": nome}


@router.post(
    "/{ficheiro}/inspecionar",
    summary="Verificar um backup (não altera nada)",
    # Não altera nada, mas decifra o conteúdo inteiro com a passphrase dada.
    dependencies=[_OperarBackup],
)
async def inspecionar(
    ficheiro: str, dados: InspecionarIn, utilizador: CurrentUserDep
):
    """Decifra com a passphrase dada e devolve o manifest — o passo de
    verificação do wizard de restauro. Recusas vêm com código estável."""
    try:
        manifest = await asyncio.to_thread(
            restaurar.inspecionar_backup, ficheiro, dados.passphrase, dados.sem_premium
        )
    except RestauroErro as erro:
        raise _erro_restauro(erro)
    return {"manifest": manifest}


@router.post(
    "/{ficheiro}/restaurar",
    summary="Restaurar um backup (SUBSTITUI os dados atuais)",
    dependencies=[_OperarBackup],
)
def executar_restauro(
    ficheiro: str, dados: RestaurarIn, utilizador: CurrentUserDep, db: SessionDep
):
    """O MESMO serviço do script de restauro, com todas as proteções:
    backup de segurança automático, modo manutenção, validações, premium via
    sidecar. Responde e reinicia o backend em seguida — as migrações e o
    relatório final acontecem no arranque (poll a /backups/restauro/estado).
    Sem auditoria aqui: a base vai ser substituída — quem executou fica no
    testemunho e é auditado na finalização, já sobre a base restaurada."""
    if dados.confirmacao.strip().upper() != "RESTAURAR":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"codigo": "confirmacao_invalida"},
        )
    # O restauro termina todas as ligações à base. A sessão deste pedido — a que
    # autenticou o utilizador — morria com elas, e o commit dela no fim do pedido
    # respondia 500 a um restauro que tinha corrido bem.
    db.close()
    try:
        resultado = restaurar.executar_restauro_ui(
            ficheiro, dados.passphrase,
            dados.sem_premium, str(utilizador.id),
        )
    except RestauroErro as erro:
        raise _erro_restauro(erro)

    # Reinício deliberado: a resposta sai primeiro; o SIGTERM ao uvicorn (PID 1)
    # faz o container reiniciar e o entrypoint correr as migrações — o mesmo
    # caminho de um update normal.
    threading.Timer(3.0, lambda: os.kill(1, signal.SIGTERM)).start()
    return {"ok": True, **resultado}


@router.get(
    "/{ficheiro}/download",
    summary="Descarregar um backup",
    # Leva a instalação inteira num ficheiro — é a ação de maior alcance daqui.
    dependencies=[_OperarBackup],
)
def descarregar(
    ficheiro: str, request: Request, utilizador: CurrentUserDep, db: SessionDep
):
    """O download fica auditado: o ficheiro contém todos os dados da instalação."""
    empresa = get_empresa_ativa(db, utilizador)
    caminho = service.caminho_backup(ficheiro)
    registar_acao(
        db, acao=Acao.BACKUP_DESCARREGADO, empresa_id=empresa.id,
        utilizador_id=utilizador.id, dados_novos={"ficheiro": ficheiro}, request=request,
    )
    db.commit()
    return FileResponse(
        path=str(caminho),
        media_type="application/octet-stream",
        filename=ficheiro,
    )


@router.delete(
    "/{ficheiro}",
    status_code=204,
    summary="Apagar um backup guardado",
    dependencies=[_OperarBackup],
)
def apagar(
    ficheiro: str, request: Request, utilizador: CurrentUserDep, db: SessionDep
):
    empresa = get_empresa_ativa(db, utilizador)
    caminho = service.caminho_backup(ficheiro)
    caminho.unlink()
    registar_acao(
        db, acao=Acao.BACKUP_ELIMINADO, empresa_id=empresa.id,
        utilizador_id=utilizador.id, dados_novos={"ficheiro": ficheiro}, request=request,
    )
    db.commit()
