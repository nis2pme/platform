"""
Router do dossiê de auditoria (.nis2pme).

Acesso restrito a admin/subadmin: o dossiê agrega TODOS os dados de
conformidade da empresa (e, nas fases seguintes, as evidências decifradas) —
gerar um é um evento de segurança e fica sempre na auditoria.

As rotas são `def`: a geração (agregação + cifra) e a trilha correm no
threadpool, fora do event loop; o ficheiro sai em streaming a partir de um diretório temporário que é apagado
depois de a resposta ser enviada — nada persiste no servidor.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.datastructures import FormData

from app.config import get_settings
from app.dossie import crypto
from app.dossie import diretorio
from app.dossie import parecer as parecer_service
from app.dossie import service
from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, SessionDep, get_empresa_ativa
from app.shared.multipart import (
    booleano_do_formulario,
    ficheiro_do_formulario,
    formulario_autorizado,
)

router = APIRouter(
    prefix="/dossie",
    tags=["Dossiê de auditoria"],
    # Chão de LEITURA: consultar a chave da instância e a estimativa do dossiê.
    # Tudo o que produza, envie ou importe um dossiê acrescenta o de ESCRITA.
    dependencies=[Depends(require_capability("dossie", ClasseAcao.VER))],
)

_OperarDossie = Depends(require_capability("dossie", ClasseAcao.OPERAR))

# Rotas sem o gate admin/subadmin: o selo de auditoria é informação de
# dashboard, visível a qualquer utilizador autenticado da empresa.
router_selo = APIRouter(prefix="/dossie", tags=["Dossiê de auditoria"])


class GerarDossieIn(BaseModel):
    # Exatamente um dos dois modos de cifra (validado no serviço):
    #   - passphrase ≥12 (comunicada fora de banda), ou
    #   - convite: token do auditor (cifra ao destinatário age dele).
    passphrase: str | None = Field(default=None, max_length=200)
    convite: str | None = Field(default=None, max_length=8000)
    incluir_evidencias: bool = True
    # 3 | 6 | 12 meses, ou None = desde sempre (validado no serviço).
    periodo_atividade_meses: int | None = 12
    # Modo convite com auditor não atestado nem fixado: o admin declara que
    # confirmou o fingerprint por outro canal (senão o serviço recusa, 409).
    confirmar_fingerprint: bool = False


class VerificarConviteIn(BaseModel):
    convite: str = Field(min_length=1, max_length=8000)


@router.post(
    "/convite/verificar",
    summary="Verificar um convite do auditor (preview)",
    dependencies=[_OperarDossie],
)
def verificar_convite(dados: VerificarConviteIn, utilizador: CurrentUserDep, db: SessionDep):
    """Valida o token e devolve quem convida (fingerprint + estado da atestação
    + se a chave já está fixada) para a UI mostrar antes de gerar. Não gera nada."""
    empresa = get_empresa_ativa(db, utilizador)
    try:
        info = crypto.verificar_convite(dados.convite)
    except crypto.ConviteInvalido as erro:
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"codigo": "convite_invalido", "detalhe": str(erro)},
        ) from erro
    fixado = service.auditor_fixado(db, empresa, info["auditor_pub"])
    return {
        "convite_id": info["convite_id"],
        "auditor_nome": info["auditor_nome"],
        "auditor_fingerprint": info["auditor_fingerprint"],
        "atestacao_estado": info["atestacao_estado"],
        # Atestada OU já fixada → a geração dispensa a confirmação manual do
        # fingerprint; caso contrário a UI exige-a antes de gerar.
        "confiavel": fixado is not None or info["atestacao_estado"] == "verificada",
        # Se o convite traz um relay conhecido, a UI oferece o envio direto
        # (sem USB/email); com outro, fica o ficheiro.
        "relay_disponivel": info["relay"] is not None and service.relay_permitido(info["relay"]["url"]),
    }


@router.get("/chave", summary="Chave de assinatura da empresa (fingerprint)")
def chave(utilizador: CurrentUserDep, db: SessionDep):
    """Gera a chave no primeiro uso. O fingerprint serve para o destinatário
    confirmar a origem dos dossiês por outro canal (ex.: telefone)."""
    empresa = get_empresa_ativa(db, utilizador)
    return service.obter_chave_publica(db, empresa)


class DefinirAtestacaoIn(BaseModel):
    # O blob {payload_b64,sig} devolvido pelo License Service (attest-key).
    atestacao: dict


@router.put(
    "/atestacao",
    summary="Definir a atestação da chave de assinatura desta empresa",
    dependencies=[_OperarDossie],
)
def definir_atestacao(dados: DefinirAtestacaoIn, request: Request, utilizador: CurrentUserDep, db: SessionDep):
    """Regista a atestação colada pelo admin (obtida fora de banda do License
    Service para a chave mostrada em `GET /dossie/chave`). Recusa qualquer
    blob que não verifique contra essa chave."""
    # Em SaaS as atestações vivem no volume da instância, partilhado por todos
    # os tenants: colocá-las é do operador, não do admin de um deles.
    if get_settings().DEPLOYMENT_MODE != "onprem":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"codigo": "so_onprem"},
        )
    empresa = get_empresa_ativa(db, utilizador)
    # A atestação é da chave com que esta empresa assina — a que a UI mostra.
    estado = crypto.guardar_atestacao(dados.atestacao, crypto.obter_chave_empresa(db, empresa)["pub"])
    registar_acao(
        db, acao=Acao.DOSSIE_ATESTACAO_DEFINIDA, empresa_id=empresa.id, utilizador_id=utilizador.id,
        dados_novos={"estado": estado}, request=request,
    )
    db.commit()
    return {"estado": estado}


@router.get("/estimativa", summary="Contagens e tamanho previsto do dossiê")
def estimativa(utilizador: CurrentUserDep, db: SessionDep):
    """O que o dossiê vai incluir — para a UI mostrar antes de gerar."""
    empresa = get_empresa_ativa(db, utilizador)
    return service.estimar(db, empresa)


@router.post(
    "",
    summary="Gerar e descarregar um dossiê de auditoria",
    dependencies=[_OperarDossie],
)
def gerar(
    dados: GerarDossieIn, request: Request, utilizador: CurrentUserDep, db: SessionDep
):
    """Devolve o ficheiro .nis2pme cifrado (passphrase OU convite) e assinado.
    A passphrase nunca é guardada — sem ela o ficheiro é irrecuperável (e o
    dossiê pode sempre ser gerado de novo: a fonte de verdade é a app, não o
    ficheiro). No modo convite, só a plataforma do auditor (que tem a chave
    efémera) o abre."""
    empresa = get_empresa_ativa(db, utilizador)
    resultado = service.gerar_dossie(
        db, empresa, utilizador, dados.passphrase,
        dados.incluir_evidencias, dados.periodo_atividade_meses, request,
        dados.convite, dados.confirmar_fingerprint,
    )
    registar_acao(
        db, acao=Acao.DOSSIE_GERADO, empresa_id=empresa.id, utilizador_id=utilizador.id,
        dados_novos={
            "dossie_id": resultado["dossie_id"],
            "ficheiro": resultado["ficheiro"],
            "sha256": resultado["sha256"],
            "contagens": resultado["contagens"],
            "ambito": resultado["ambito"],
        },
        request=request,
    )
    db.commit()
    return FileResponse(
        path=resultado["caminho"],
        media_type="application/octet-stream",
        filename=resultado["ficheiro"],
        background=BackgroundTask(shutil.rmtree, resultado["tmp_dir"], ignore_errors=True),
    )


class EnviarRelayIn(BaseModel):
    convite: str = Field(min_length=1, max_length=8000)
    incluir_evidencias: bool = True
    periodo_atividade_meses: int | None = 12
    confirmar_fingerprint: bool = False


@router.post(
    "/relay/enviar",
    summary="Gerar e enviar o dossiê pelo relay do convite",
    dependencies=[_OperarDossie],
)
def enviar_relay(
    dados: EnviarRelayIn, request: Request, utilizador: CurrentUserDep, db: SessionDep
):
    """Gera o dossiê no modo convite e fá-lo chegar ao auditor pelo relay cego
    indicado no próprio convite — o ficheiro cifrado nunca passa pelo browser.
    Recusa (400) se o convite não trouxer bloco de relay."""
    from fastapi import HTTPException, status
    empresa = get_empresa_ativa(db, utilizador)
    # O destino confere-se antes de gerar (a geração é a parte cara). Um
    # convite ilegível segue: a geração recusa-o com o código certo.
    try:
        relay = crypto.verificar_convite(dados.convite)["relay"]
    except crypto.ConviteInvalido:
        relay = False
    if relay is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"codigo": "convite_sem_relay"})
    if relay and not service.relay_permitido(relay["url"]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"codigo": "relay_nao_permitido"})
    resultado = service.gerar_dossie(
        db, empresa, utilizador, None,
        dados.incluir_evidencias, dados.periodo_atividade_meses, request,
        dados.convite, dados.confirmar_fingerprint,
    )
    try:
        if not resultado["relay"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"codigo": "convite_sem_relay"},
            )
        envio = service.enviar_para_relay(
            Path(resultado["caminho"]), resultado["relay"]
        )
    finally:
        shutil.rmtree(resultado["tmp_dir"], ignore_errors=True)
    registar_acao(
        db, acao=Acao.DOSSIE_GERADO, empresa_id=empresa.id, utilizador_id=utilizador.id,
        dados_novos={
            "dossie_id": resultado["dossie_id"], "sha256": resultado["sha256"],
            "ambito": resultado["ambito"], "via": "relay",
        },
        request=request,
    )
    db.commit()
    return {"ok": True, "dossie_id": resultado["dossie_id"], "bytes": envio.get("bytes")}


# ---------------------------------------------------------------------------
# Parecer do auditor (round-trip)
# ---------------------------------------------------------------------------

@router.post(
    "/parecer/inspecionar",
    summary="Verificar um parecer e pré-visualizar",
    dependencies=[_OperarDossie],
)
def parecer_inspecionar(
    utilizador: CurrentUserDep, db: SessionDep,
    # Depois do utilizador e da capacidade: o ficheiro só é lido quando passaram.
    formulario: FormData = Depends(formulario_autorizado(max_ficheiros=1, max_campos=1)),
):
    """Corre a verificação completa (assinatura, referência ao dossiê, decifra)
    e devolve o preview — nada é gravado. A confirmação re-verifica tudo."""
    ficheiro = ficheiro_do_formulario(formulario)
    empresa = get_empresa_ativa(db, utilizador)
    dados = ficheiro.file.read(parecer_service.FICHEIRO_MAX + 1)
    return parecer_service.inspecionar(db, empresa, dados)


@router.post(
    "/parecer/confirmar",
    summary="Importar um parecer de auditor",
    dependencies=[_OperarDossie],
)
def parecer_confirmar(
    request: Request,
    utilizador: CurrentUserDep,
    db: SessionDep,
    # Depois do utilizador e da capacidade: o ficheiro só é lido quando passaram.
    formulario: FormData = Depends(formulario_autorizado(max_ficheiros=1, max_campos=2)),
):
    """Importa o parecer: relatórios de auditoria externos, achados→tarefas,
    pedidos→notificações e selo. Uma chave de auditor ainda não fixada exige
    `confirmar_fingerprint` (TOFU — confirmação por outro canal)."""
    ficheiro = ficheiro_do_formulario(formulario)
    confirmar_fingerprint = booleano_do_formulario(formulario, "confirmar_fingerprint")
    empresa = get_empresa_ativa(db, utilizador)
    dados = ficheiro.file.read(parecer_service.FICHEIRO_MAX + 1)
    resultado = parecer_service.confirmar(
        db, empresa, utilizador, dados, confirmar_fingerprint
    )
    registar_acao(
        db, acao=Acao.PARECER_IMPORTADO, empresa_id=empresa.id, utilizador_id=utilizador.id,
        dados_novos=resultado, request=request,
    )
    db.commit()
    return resultado


@router_selo.get("/selo", summary="Selo de auditoria externa (dashboard)")
def selo(utilizador: CurrentUserDep, db: SessionDep):
    """Último parecer importado: "dossiê de {data} revisto por {auditor}"."""
    empresa = get_empresa_ativa(db, utilizador)
    return parecer_service.selo(db, empresa)


@router.get("/auditores", summary="Encontrar auditor (diretório do ecossistema)")
def encontrar_auditores(
    utilizador: CurrentUserDep,
    q: str = Query(default="", max_length=100),
    regiao: str = Query(default="", max_length=100),
    idioma: str = Query(default="", max_length=20),
):
    """Consulta o diretório público de auditores com os filtros dados. O pedido
    ao serviço só sai daqui, quando o utilizador o pede; sem URL configurada
    devolve `configurado=false` sem contactar ninguém."""
    try:
        return diretorio.procurar(q=q, regiao=regiao, idioma=idioma)
    except diretorio.DiretorioIndisponivel as erro:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"codigo": "diretorio_indisponivel", "detalhe": str(erro)[:200]},
        ) from erro
