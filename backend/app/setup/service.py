"""
Lógica de negócio do módulo de setup inicial (on-prem).
Cria a primeira empresa + admin; só executa uma vez.
"""
import logging
from datetime import datetime, timezone

from fastapi import HTTPException, Request, status
from sqlmodel import Session, func, select

from app.auth.models import RoleUtilizador, Utilizador
from app.auth.service import criar_temp_token, hash_password
from app.config import get_settings
from app.empresas.models import DimensaoEmpresa, Empresa, NivelQNRCS, TipoEntidade
from app.setup.schemas import SetupConfigurarSchema
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.pii import cifrar_pii

settings = get_settings()
logger = logging.getLogger(__name__)


def verificar_setup_completo(db: Session) -> bool:
    """
    Verifica se o sistema já foi configurado (existe pelo menos 1 empresa ativa).
    Se True, o endpoint POST /setup/configurar devolve 410 Gone.
    """
    count = db.exec(
        select(func.count(Empresa.id)).where(
            Empresa.deleted_at.is_(None),  # type: ignore[union-attr]
            Empresa.ativo == True,  # noqa: E712
        )
    ).one()
    return count > 0


def executar_setup(
    db: Session,
    dados: SetupConfigurarSchema,
    request: Request | None = None,
) -> dict:
    """
    Cria a primeira empresa e o primeiro utilizador admin.
    Apenas disponível em DEPLOYMENT_MODE=onprem e quando o sistema não foi ainda configurado.
    Devolve access_token + info do utilizador + (opcionalmente) aviso_smtp se o email não estiver ativo.
    """
    if settings.DEPLOYMENT_MODE != "onprem":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Este endpoint só está disponível em modo on-prem.",
        )

    if verificar_setup_completo(db):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Esta instalação já foi configurada. Use o login normal.",
        )

    # Verificar email único
    existente = db.exec(
        select(Utilizador).where(Utilizador.email == dados.admin_email.lower())
    ).first()
    if existente:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este endereço de email já está registado.",
        )

    # Mapear strings para enums
    try:
        dimensao_enum = DimensaoEmpresa(dados.empresa_dimensao)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Dimensão inválida: {dados.empresa_dimensao}",
        )
    try:
        tipo_entidade_enum = TipoEntidade(dados.empresa_tipo_entidade)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Tipo de entidade inválido: {dados.empresa_tipo_entidade}",
        )
    try:
        nivel_qnrcs_enum = NivelQNRCS(dados.empresa_nivel_qnrcs)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Nível de conformidade QNRCS inválido: {dados.empresa_nivel_qnrcs}",
        )

    # Criar empresa
    empresa = Empresa(
        nome=cifrar_pii(dados.empresa_nome),
        setor=dados.empresa_setor,
        dimensao=dimensao_enum,
        tipo_entidade=tipo_entidade_enum,
        nivel_qnrcs=nivel_qnrcs_enum,
        ativo=True,
        onboarding_completo=True,
    )
    db.add(empresa)
    db.flush()

    # Criar admin
    admin = Utilizador(
        empresa_id=empresa.id,
        email=dados.admin_email.lower(),
        nome=cifrar_pii(dados.admin_nome),
        password_hash=hash_password(dados.admin_password),
        role=RoleUtilizador.ADMIN,
        ativo=True,
        password_temporaria_ativa=False,
        consentimento_termos_at=datetime.now(timezone.utc),
        consentimento_termos_versao=dados.versao_termos,
    )
    db.add(admin)
    db.flush()

    # Inicializar controlos da empresa
    from app.controlos.service import inicializar_controlos_empresa
    inicializar_controlos_empresa(db, empresa.id)

    registar_acao(
        db,
        acao=Acao.UTILIZADOR_CRIADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa.id,
        utilizador_id=admin.id,
        entidade_tipo="Utilizador",
        entidade_id=admin.id,
        dados_novos={
            "email": admin.email,
            "role": admin.role.value,
            "origem": "setup_onprem",
        },
        request=request,
    )
    db.commit()
    db.refresh(admin)
    db.refresh(empresa)

    # 2FA é OBRIGATÓRIO: não emitir sessão completa aqui. Emite-se um token
    # temporário (2fa_setup_required) — o wizard força o enrolamento de 2FA antes
    # de conceder acesso, tal como o fluxo de login. Sem isto, o primeiro admin
    # (o mais privilegiado) entraria sem 2FA.
    temp_token = criar_temp_token(admin, "2fa_setup_required")

    resposta: dict = {
        "temp_token": temp_token,
        "requires_2fa_setup": True,
        "utilizador": {
            "id": str(admin.id),
            "email": admin.email,
            "nome": dados.admin_nome,
            "role": admin.role.value,
            "empresa_id": str(empresa.id),
            "totp_ativo": False,
            "password_temporaria_ativa": False,
        },
    }

    # Se SMTP não configurado, devolver código de aviso (texto traduzido no frontend).
    # Lido fresco: o passo de email do wizard pode tê-lo ativado antes deste passo,
    # sem reiniciar o processo.
    if not get_settings().EMAIL_ENABLED:
        resposta["aviso_smtp"] = "email_nao_configurado"

    return resposta
