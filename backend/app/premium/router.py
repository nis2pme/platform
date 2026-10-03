"""
Router premium (open-core) — superfície fina; o direito a cada módulo é decidido no sidecar.
A lógica premium real vive do outro lado do contrato gRPC, no sidecar privado.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session

from app.config import get_settings
from app.database import get_session
from app.premium.client import PremiumClient, get_premium_client
from app.premium.schemas import (
    EstadoLicencaSchema,
    InstalarLicencaIn,
    LicencaInstaladaSchema,
    PrazoAcessoSchema,
)
from app.shared.audit import Acao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import get_current_user

logger = logging.getLogger(__name__)

# Estado do subsistema e da licença são informação da INSTALAÇÃO, e a
# instalação mostra-se na aba Sistema — é a mesma célula que a governa.
_SistemaDep = Depends(require_capability("sistema", ClasseAcao.VER))

router = APIRouter(prefix="/premium", tags=["Premium"])


@router.get(
    "/status",
    summary="Estado do subsistema premium",
    dependencies=[_SistemaDep],
)
def premium_status(
    utilizador=Depends(get_current_user),
    premium: PremiumClient = Depends(get_premium_client),
):
    """Indica se o premium está ligado (há sidecar configurado). Requer sessão."""
    return {"premium_enabled": premium.enabled}


def _tem_digitos(nif: str) -> bool:
    return any(ch.isdigit() for ch in nif or "")


def _nif_da_empresa(db: Session, utilizador) -> str:
    """NIF da empresa, decifrado, para o sidecar compor o código de instalação e
    conferir a licença. Vai sobre o mTLS do mesh. Se não se conseguir ler, sai
    vazio em vez de o cartão falhar."""
    try:
        from app.empresas.models import Empresa
        from app.shared.pii import decifrar_pii

        empresa = db.get(Empresa, utilizador.empresa_id)
        return (decifrar_pii(empresa.nif) or "") if empresa is not None else ""
    except Exception:  # noqa: BLE001 — informativo, nunca bloqueia
        logger.warning("licenca: NIF ilegível; segue sem NIF", exc_info=True)
        return ""


@router.get(
    "/prazo",
    response_model=PrazoAcessoSchema,
    summary="Fim do trial ou da licença (faixa no topo da app)",
    dependencies=[_SistemaDep],
)
def prazo_acesso(
    utilizador=Depends(get_current_user),
    premium: PremiumClient = Depends(get_premium_client),
    db: Session = Depends(get_session, scope="function"),
):
    """Quando e com que peso avisar do fim do acesso. Fail-soft: sem dados ou
    com o sidecar em baixo, devolve `tipo=""` e a faixa não aparece."""
    from dataclasses import asdict

    from app.empresas.models import Empresa
    from app.premium.prazo_acesso import obter_prazo

    empresa = db.get(Empresa, utilizador.empresa_id)
    if empresa is None:
        return PrazoAcessoSchema()
    prazo = obter_prazo(db, empresa, premium)
    return PrazoAcessoSchema(**asdict(prazo), planos_url=get_settings().SAAS_PLANOS_URL if prazo.tipo == "trial" else "")


@router.get(
    "/licenca",
    response_model=EstadoLicencaSchema,
    summary="Estado da licença (cartão no UI)",
    dependencies=[_SistemaDep],
)
def estado_licenca(
    utilizador=Depends(get_current_user),
    premium: PremiumClient = Depends(get_premium_client),
    db: Session = Depends(get_session, scope="function"),
):
    """Estado agregado da licença para o cartão de licença.

    Fail-soft: se o sidecar estiver inalcançável, devolve `estado="indisponivel"`
    com 200 — o cartão mostra "indisponível", não um erro. É informação de estado,
    não uma operação crítica.
    """
    tenant = str(utilizador.empresa_id)
    # O NIF só serve o código de instalação on-prem: em SaaS não sai do core.
    onprem = get_settings().DEPLOYMENT_MODE == "onprem"
    nif = _nif_da_empresa(db, utilizador) if onprem else ""
    # Sem NIF não há código de instalação (a licença prende-se ao NIF): o cartão
    # pede ao administrador que o defina primeiro, em vez de mostrar nada.
    empresa_sem_nif = get_settings().DEPLOYMENT_MODE == "onprem" and not _tem_digitos(nif)
    try:
        estado = premium.estado_licenca(tenant, nif)
    except Exception:  # noqa: BLE001 — fail-soft: nunca deitar abaixo o cartão
        logger.warning("estado_licenca: sidecar inalcançável", exc_info=True)
        return EstadoLicencaSchema(estado="indisponivel")
    return EstadoLicencaSchema(
        estado=estado.estado,
        plano=estado.plano,
        expires_at=estado.expires_at,
        grace_ate=estado.grace_ate,
        dias_restantes=estado.dias_restantes,
        codigo_instalacao=estado.codigo_instalacao,
        instance_id=estado.instance_id,
        heartbeat_estado=estado.heartbeat_estado,
        heartbeat_ultimo_ok=estado.heartbeat_ultimo_ok,
        dias_sem_heartbeat=estado.dias_sem_heartbeat,
        so_leitura=estado.so_leitura,
        so_leitura_motivo=estado.so_leitura_motivo,
        empresa_sem_nif=empresa_sem_nif,
    )


@router.post(
    "/licenca/instalar",
    response_model=LicencaInstaladaSchema,
    summary="Instalar (ou validar) um ficheiro de licença",
    dependencies=[Depends(require_capability("sistema", ClasseAcao.OPERAR))],
)
def instalar_licenca(
    dados: InstalarLicencaIn,
    request: Request,
    utilizador=Depends(get_current_user),
    premium: PremiumClient = Depends(get_premium_client),
    db: Session = Depends(get_session, scope="function"),
):
    """O administrador cola o ficheiro que recebeu do fornecedor. Com
    `so_validar` devolve só o resumo (cliente, NIF, plano, validade) para
    confirmar; sem ele, o sidecar grava e a licença entra em vigor sem reiniciar.
    Uma recusa é 400 com um código estável; o sidecar em baixo é 503.
    """
    # A licença é da instalação: em SaaS a subscrição é gerida pelo fornecedor e
    # um admin de tenant não tem nada a instalar.
    if get_settings().DEPLOYMENT_MODE != "onprem":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail={"codigo": "so_onprem"}
        )
    nif = _nif_da_empresa(db, utilizador)
    if not _tem_digitos(nif):
        # A licença prende-se ao NIF; sem ele o sidecar diria `nif_errado`, que
        # apontaria para o ficheiro em vez de para a empresa.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail={"codigo": "empresa_sem_nif"}
        )
    try:
        # A licença prende-se também à empresa (o id dela, que foi no código de
        # instalação): o sidecar recusa a de outra com `empresa_errada`.
        res = premium.instalar_licenca(
            dados.envelope, nif, dados.so_validar, str(utilizador.empresa_id)
        )
    except Exception:  # noqa: BLE001 — sidecar inalcançável
        logger.warning("instalar_licenca: sidecar inalcançável", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"codigo": "premium_indisponivel"},
        )
    if not res.aceite:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"codigo": res.codigo_erro or "recusada", "detalhe": res.detalhe[:200]},
        )
    if res.instalada:
        registar_acao(
            db,
            acao=Acao.LICENCA_INSTALADA,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            entidade_tipo="Sistema",
            dados_novos={
                "license_id": res.license_id,
                "plano": res.plano,
                "expires_at": res.expires_at,
                "modulos": res.modulos,
            },
            request=request,
        )
        db.commit()
    return LicencaInstaladaSchema(
        aceite=True,
        customer=res.customer,
        nif_licenca=res.nif_licenca,
        plano=res.plano,
        expires_at=res.expires_at,
        grace_dias=res.grace_dias,
        modulos=res.modulos,
        license_id=res.license_id,
        instalada=res.instalada,
    )
