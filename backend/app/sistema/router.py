"""
Router do módulo Sistema (saúde da instalação) — apenas on-prem.

O router só é montado em DEPLOYMENT_MODE=onprem (ver main.py); em SaaS a
operação da plataforma é do operador, não do tenant.

Dois gates: o do router é o chão de LEITURA — ver o estado do sistema é
informação de operação e não é de toda a gente; cada endpoint que mexe na
instalação acrescenta o de ESCRITA. Separados porque são perguntas diferentes, e
uma organização pode querer responder-lhes de maneira diferente.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, SessionDep
from app.sistema.schemas import (
    EmailConfigSchema,
    EmailConfigUpdateSchema,
    EmailTesteRespostaSchema,
    EmailTesteSchema,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/sistema",
    tags=["Sistema"],
    dependencies=[Depends(require_capability("sistema", ClasseAcao.VER))],
)

# Configurar o correio de saída é administração da instalação, não do dia-a-dia:
# a matriz dá `sistema.operar` só ao administrador.
_OperarSistema = Depends(require_capability("sistema", ClasseAcao.OPERAR))


@router.get("/saude", summary="Saúde da instalação (on-prem)")
def saude(request: Request, utilizador: CurrentUserDep, db: SessionDep):
    """Fotografia do estado: BD, disco, sidecar premium, email, TLS, updates, ticks, backups."""
    from app.sistema import service
    return service.obter_saude(db, request.app.state)


@router.post(
    "/teste-email",
    summary="Enviar um email de teste ao próprio utilizador",
    # Mesmo gate do teste de uma configuração nova: mandar correio pelo relé da
    # instalação é operá-la, e o gate de leitura do router não chega para isso.
    dependencies=[_OperarSistema],
)
async def teste_email(request: Request, utilizador: CurrentUserDep):
    """
    Valida a configuração de email na prática: envia um email de teste para o
    endereço do utilizador autenticado. Segue o MESMO gate das notificações
    (EMAIL_NOTIFICACOES no .env + email configurado) — se o gate recusar, o
    erro diz porquê, para o admin saber o que falta.
    """
    from app.config import get_settings
    from app.notificacoes.email import notificacoes_email_ativas
    from app.shared.email import enviar_email

    settings = get_settings()
    if not settings.EMAIL_NOTIFICACOES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"codigo": "flag_desligada"},
        )
    if not notificacoes_email_ativas():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"codigo": "email_nao_configurado"},
        )

    from app.shared.i18n import MsgsI18n, locale_de_request, traduzir

    locale = locale_de_request(request)
    try:
        await enviar_email(
            utilizador.email,
            traduzir(MsgsI18n.EMAIL_TESTE_ASSUNTO, locale),
            traduzir(MsgsI18n.EMAIL_TESTE_CORPO, locale),
        )
    except Exception as exc:  # noqa: BLE001 — devolver falha legível ao admin
        logger.exception("Envio do email de teste falhou.")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"codigo": "envio_falhou"},
        ) from exc
    return {"ok": True, "destinatario": utilizador.email}


# ---------------------------------------------------------------------------
# Configuração do servidor de saída de correio
# ---------------------------------------------------------------------------

@router.get(
    "/email",
    response_model=EmailConfigSchema,
    summary="Configuração de email em vigor (on-prem)",
)
def obter_email():
    """
    Devolve a configuração SMTP em vigor. **Nunca devolve a password** — só diz
    se existe uma guardada, para o ecrã poder distinguir "sem credenciais" de
    "credenciais que não vou mostrar".
    """
    from app.sistema import service

    return EmailConfigSchema(**service.obter_config_email())


@router.put(
    "/email",
    response_model=EmailConfigSchema,
    summary="Configurar o servidor de saída de correio (on-prem)",
    dependencies=[_OperarSistema],
)
def guardar_email(
    dados: EmailConfigUpdateSchema,
    request: Request,
    db: SessionDep,
    utilizador: CurrentUserDep,
):
    """
    Persiste a configuração no .env e aplica-a de imediato, sem reinício.

    O servidor de correio é da instalação (uma máquina, um relé) e não de cada
    organização — por isso vive no .env e não na base de dados: assim a
    credencial não entra em cópias de segurança da base nem em exportações.
    """
    from app.sistema import service

    anterior = service.obter_config_email()
    service.guardar_config_email(dados)
    novo = service.obter_config_email()

    registar_acao(
        db,
        acao=Acao.SISTEMA_EMAIL_CONFIGURADO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        dados_anteriores=anterior,
        dados_novos=novo,
        request=request,
    )
    db.commit()
    return EmailConfigSchema(**novo)


@router.post(
    "/email/testar",
    response_model=EmailTesteRespostaSchema,
    summary="Experimentar uma configuração de email sem a gravar (on-prem)",
    dependencies=[_OperarSistema],
)
async def testar_email(
    dados: EmailTesteSchema,
    request: Request,
    utilizador: CurrentUserDep,
):
    """
    Envia um email de teste com a configuração indicada, **sem a persistir** — a
    que está em vigor fica intacta enquanto o administrador afina a nova.

    O destinatário é sempre o endereço de quem está autenticado: assim este
    caminho não pode ser usado para mandar correio a terceiros. Sem password no
    pedido, usa a que está guardada — o ecrã nunca a recebeu para a poder
    reenviar.
    """
    from app.shared.i18n import locale_de_request
    from app.sistema import service

    try:
        await service.testar_config_email(
            dados, utilizador.email, locale_de_request(request)
        )
    except HTTPException:
        # Uma recusa da própria aplicação (ex.: a password guardada não é deste
        # servidor) não é uma falha de envio.
        raise
    except Exception as exc:  # noqa: BLE001 — o detalhe fica no log, não na resposta
        # A mensagem da biblioteca traz o erro do servidor remoto: fica no log do
        # servidor e não na resposta, que só leva um código estável para i18n.
        logger.exception("Teste de configuração de email falhou.")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"codigo": "envio_falhou"},
        ) from exc
    return EmailTesteRespostaSchema(ok=True, destinatario=utilizador.email)
