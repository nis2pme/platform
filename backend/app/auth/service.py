"""
Lógica de negócio do módulo de autenticação.
Todas as operações de auth passam por aqui — os routers são finos.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from cryptography.fernet import InvalidToken
from fastapi import BackgroundTasks, HTTPException, Request, status
from jose import jwt
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlmodel import Session, select

from app.auth.models import (
    BloqueioIP,
    CodigoBackup2FA,
    PasswordResetToken,
    RoleUtilizador,
    TokenRefresh,
    Utilizador,
    registar_adesao,
)
from app.auth.schemas import RegistarEmpresaSchema
from app.config import get_settings
from app.empresas.models import Empresa
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.concorrencia import LIMITE_ARGON2
from app.shared.pii import cifrar_pii, truncar_para_cifra
from app.shared.politica_seguranca import exigir_password_valida, password_min
from app.shared.utils import (
    criar_password_hasher,
    decodificar_jwt_temp,
    gerar_codigos_backup,
    gerar_token_opaco,
    hash_token,
    normalizar_codigo_backup,
    tem_formato_de_codigo_backup,
)

settings = get_settings()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Password hashing — argon2id
# Os parâmetros de custo vivem em shared/utils, para haver um só valor em toda
# a instalação. Não construir um PasswordHasher aqui: herdaria os defaults da
# biblioteca, que mudam de versão para versão.
# ---------------------------------------------------------------------------

_ph = criar_password_hasher()

# Hash pré-computado para verificação constant-time quando o email não existe.
# Sem isto, o login responde mais rápido para emails inválidos (CWE-208).
_DUMMY_HASH: str = _ph.hash("NIS2PME_dummy_constant_time_protection")


def hash_password(password: str, prioritario: bool = False) -> str:
    """Gera hash argon2id da password com parâmetros OWASP-recomendados.

    Cada hash reserva 64 MiB: quantos correm ao mesmo tempo é limitado (503 se
    as vagas não abrirem a tempo), para uma rajada não esgotar a memória.
    `prioritario` (quem traz um cookie de dispositivo válido) pode usar a vaga
    reservada."""
    with LIMITE_ARGON2.ocupar(prioritario=prioritario):
        return _ph.hash(password)


def verify_password(plain: str, hashed: str, prioritario: bool = False) -> bool:
    """Verifica password em texto limpo contra hash argon2id (com o mesmo limite
    de simultâneos do `hash_password`; `prioritario` usa a vaga reservada)."""
    try:
        with LIMITE_ARGON2.ocupar(prioritario=prioritario):
            return _ph.verify(hashed, plain)
    except (VerifyMismatchError, InvalidHashError):
        return False


def precisa_rehash(hashed: str) -> bool:
    """O hash foi gerado com parâmetros diferentes dos de hoje?

    Fixar os parâmetros em `criar_password_hasher()` só resolve metade do
    problema: fixa o custo dos hashes **novos**. Sem uma migração ao login, o dia
    em que o custo subir — porque o hardware evoluiu, que é o motivo por que
    estes números existem — deixa **todos os utilizadores já criados** com hashes
    de custo antigo para sempre.

    O argon2 guarda os parâmetros dentro do próprio hash, por isso a comparação
    não precisa de estado nenhum: pergunta-se à biblioteca.

    Um hash ilegível não é caso de rehash — é caso de credencial inválida, e essa
    decisão pertence ao `verify_password`, que já a toma.
    """
    try:
        return _ph.check_needs_rehash(hashed)
    except (InvalidHashError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Fernet — cifra/decifra segredo TOTP em repouso
# ---------------------------------------------------------------------------

MSG_2FA_ILEGIVEL = (
    "O 2FA não pode ser validado: a chave de cifra desta instalação não abre o "
    "segredo guardado. Contacte o administrador."
)
MSG_2FA_JA_ATIVO = "O 2FA já está ativo."


def _get_fernet():
    from app.shared.chaves import fernet_obrigatorio

    return fernet_obrigatorio("TOTP_ENCRYPTION_KEY")


def cifrar_totp_secret(secret: str) -> str:
    """Cifra o segredo TOTP com Fernet antes de guardar na DB."""
    return _get_fernet().encrypt(secret.encode()).decode()


def passo_totp(secret: str, codigo: str, agora: datetime | None = None) -> int | None:
    """O passo de 30 s em que `codigo` é válido, ou None.

    Mesma tolerância do `verify(valid_window=1)`: o passo atual, o anterior e o
    seguinte. Devolver o passo (e não só «válido») é o que deixa recusar um
    código que já foi usado — ver `consumir_passo_totp`.
    """
    import hmac

    import pyotp

    totp = pyotp.TOTP(secret)
    limpo = codigo.strip().replace(" ", "")
    atual = totp.timecode(agora or datetime.now(timezone.utc))
    for passo in (atual - 1, atual, atual + 1):
        if hmac.compare_digest(totp.generate_otp(passo), limpo):
            return passo
    return None


def consumir_passo_totp(db: Session, modelo, conta_id, passo: int) -> bool:
    """Marca `passo` como usado pela conta. False se esse passo, ou um posterior,
    já foi aceite — o mesmo código não entra duas vezes.

    É um UPDATE condicional e não uma leitura seguida de escrita: dois pedidos em
    simultâneo com o mesmo código disputam a mesma linha e só um a altera.
    `modelo` é `Utilizador` ou a conta do superadmin (as duas têm a coluna).
    """
    from sqlalchemy import or_, update

    resultado = db.execute(
        update(modelo)
        .where(
            modelo.id == conta_id,
            or_(modelo.totp_ultimo_passo.is_(None), modelo.totp_ultimo_passo < passo),
        )
        .values(totp_ultimo_passo=passo)
        .execution_options(synchronize_session=False)
    )
    return resultado.rowcount == 1


def decifrar_totp_secret(cifrado: str) -> str:
    """Decifra o segredo TOTP armazenado na DB.

    Um segredo que não decifra é uma chave que não é a desta instalação (um
    restauro com o `.env` de outra máquina, uma rotação a meio). Sem o segredo não
    há 2FA possível: responde-se 503 com a causa, em vez de um 500 sem explicação.
    """
    try:
        return _get_fernet().decrypt(cifrado.encode()).decode()
    except InvalidToken:
        logger.error(
            "Segredo 2FA não decifra com a TOTP_ENCRYPTION_KEY desta instalação "
            "(chave trocada ou dados de outra instalação)."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_2FA_ILEGIVEL,
        )


# ---------------------------------------------------------------------------
# JWT — access token e temp token (2FA pending)
# ---------------------------------------------------------------------------

def criar_access_token(utilizador: Utilizador) -> str:
    """
    Cria um JWT de acesso completo com claims: sub, user_id, empresa_id, role, type.
    Expira em JWT_ACCESS_TOKEN_EXPIRE_MINUTES (default 30 min).
    """
    expira = datetime.now(timezone.utc) + timedelta(
        minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    # O `sub` leva o id e não o email: o JWT só é assinado, não cifrado — quem
    # apanhe o token lê-o em base64, e um identificador interno não diz nada.
    payload = {
        "sub": str(utilizador.id),
        "user_id": str(utilizador.id),
        "empresa_id": str(utilizador.empresa_id),
        "role": utilizador.role.value,
        "type": "access",
        "jti": str(uuid.uuid4()),
        "exp": expira,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def criar_temp_token(utilizador: Utilizador, tipo: str) -> str:
    """
    Cria JWT temporário para fluxo de 2FA.
    tipo = "2fa_pending" | "2fa_setup_required"
    Expira em 5 minutos.
    """
    expira = datetime.now(timezone.utc) + timedelta(minutes=5)
    payload = {
        "sub": str(utilizador.id),
        "user_id": str(utilizador.id),
        "empresa_id": str(utilizador.empresa_id),
        "type": tipo,
        "jti": str(uuid.uuid4()),
        "exp": expira,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def _decodificar_temp_token(temp_token: str, tipo_esperado: str) -> dict:
    """Valida e decodifica um token temporário de 2FA. Delega em decodificar_jwt_temp."""
    return decodificar_jwt_temp(
        temp_token, settings.JWT_SECRET_KEY, tipo_esperado, settings.JWT_ALGORITHM
    )


def alterar_password_temporaria_login(
    db: Session,
    temp_token: str,
    nova_password: str,
    confirmar_nova_password: str,
    request: Request | None = None,
) -> Utilizador:
    """Altera password temporária e conclui login."""
    if nova_password != confirmar_nova_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="As passwords não coincidem.",
        )

    # O utilizador resolve-se PRIMEIRO: a política de password é da empresa dele,
    # e a empresa só se conhece depois de ler o token. Pela ordem inversa, a
    # função tentava ler `utilizador.empresa_id` antes de o carregar e rebentava
    # em todos os pedidos — o que deixava quem tinha uma password temporária
    # (reset por um administrador) sem forma de entrar.
    payload = _decodificar_temp_token(temp_token, "password_change_required")
    utilizador_id = uuid.UUID(payload["user_id"])
    utilizador = db.get(Utilizador, utilizador_id)
    if not utilizador:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Utilizador inválido para esta operação.",
        )
    exigir_conta_utilizavel(db, utilizador, request)

    # O mínimo é o da empresa da conta, que só se conhece depois de ler o token.
    exigir_password_valida(nova_password, db=db, empresa_id=utilizador.empresa_id)

    if not utilizador.password_temporaria_ativa:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este utilizador já não tem password temporária ativa.",
        )

    if verify_password(nova_password, utilizador.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A nova password não pode ser igual à password temporária.",
        )

    utilizador.password_hash = hash_password(nova_password)
    utilizador.password_temporaria_ativa = False
    utilizador.updated_at = datetime.now(timezone.utc)
    db.add(utilizador)
    terminadas = terminar_sessoes(db, utilizador.id)

    registar_acao(
        db,
        acao=Acao.PASSWORD_ALTERADA,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        dados_novos={"origem": "password_temporaria", "sessoes_terminadas": terminadas},
        request=request,
    )

    return utilizador


# ---------------------------------------------------------------------------
# Refresh tokens — opacos, armazenados como hash SHA-256
# ---------------------------------------------------------------------------

def criar_refresh_token(
    db: Session,
    utilizador: Utilizador,
    request: Request | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> str:
    """
    Gera um refresh token opaco, guarda o hash na DB, e devolve o token em texto limpo.
    O token deve ser enviado ao cliente em httpOnly cookie.
    """
    token = gerar_token_opaco(32)
    token_hash = hash_token(token)

    ip = ip_address
    ua = user_agent
    if request:
        from app.shared.audit import _extrair_ip
        ip = _extrair_ip(request)
        ua = request.headers.get("user-agent", "")

    # Este caminho corre no login BEM-SUCEDIDO: sem o corte, um User-Agent longo
    # fazia o criptograma não caber na coluna e impedia a sessão de ser criada.
    ip = truncar_para_cifra(ip, limite_coluna=200)
    ua = truncar_para_cifra(ua, limite_coluna=700)

    expira = datetime.now(timezone.utc) + timedelta(
        days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS
    )

    refresh = TokenRefresh(
        utilizador_id=utilizador.id,
        token_hash=token_hash,
        ip_address=cifrar_pii(ip),
        user_agent=cifrar_pii(ua),
        expires_at=expira.replace(tzinfo=None),  # armazenar sem tz na DB
    )
    db.add(refresh)
    return token


def _utc(momento: datetime | None) -> datetime | None:
    """As datas lidas da base podem vir sem fuso; são sempre UTC."""
    if momento is None or momento.tzinfo is not None:
        return momento
    return momento.replace(tzinfo=timezone.utc)


def _terminar(sessao: TokenRefresh, agora: datetime) -> None:
    """Termina uma sessão: revogada e expirada no mesmo instante.

    Um token rodado só é revogado — a sessão continua no token seguinte, e o
    prazo dele fica como estava. É esta diferença que deixa a renovação
    distinguir um browser que ainda guardava uma sessão terminada (logout,
    password mudada noutro dispositivo: inofensivo) de alguém que apresenta um
    token já rodado (uma cópia).

    As duas colunas guardam-se sem fuso, com o MESMO valor: é a igualdade que
    marca a sessão terminada, e não pode depender do fuso do servidor da base."""
    momento = agora.astimezone(timezone.utc).replace(tzinfo=None)
    sessao.revogado_at = momento
    sessao.expires_at = momento


def revogar_refresh_token(db: Session, token: str) -> bool:
    """
    Termina a sessão deste refresh token (logout). Devolve True se estava ativa.
    """
    token_hash = hash_token(token)
    refresh = db.exec(
        select(TokenRefresh).where(
            TokenRefresh.token_hash == token_hash,
            TokenRefresh.revogado_at.is_(None),  # type: ignore[union-attr]
        )
    ).first()

    if not refresh:
        return False

    _terminar(refresh, datetime.now(timezone.utc))
    db.add(refresh)
    return True


def terminar_sessoes(db: Session, utilizador_id: uuid.UUID) -> int:
    """Termina todas as sessões de um utilizador e inutiliza os pedidos de
    recuperação de password ainda por usar. Devolve quantas sessões terminou.

    Chama-se sempre que a password ou o 2FA mudam, seja quem for que os mude — o
    próprio, o administrador, o email de recuperação ou o operador da plataforma.
    Uma credencial nova não protege nada se a sessão aberta com a antiga (por
    quem a roubou, por exemplo) continuar a renovar-se; e um pedido de
    recuperação feito antes da mudança não pode servir para a desfazer.

    Não faz commit: fica na transação de quem chama, com a mudança que a motivou.
    """
    agora = datetime.now(timezone.utc)
    sessoes = db.exec(
        select(TokenRefresh).where(
            TokenRefresh.utilizador_id == utilizador_id,
            TokenRefresh.revogado_at.is_(None),  # type: ignore[union-attr]
        )
    ).all()
    for sessao in sessoes:
        _terminar(sessao, agora)
        db.add(sessao)
    for pedido in db.exec(
        select(PasswordResetToken).where(
            PasswordResetToken.utilizador_id == utilizador_id,
            PasswordResetToken.usado_at.is_(None),  # type: ignore[union-attr]
        )
    ).all():
        pedido.usado_at = agora
        db.add(pedido)
    return len(sessoes)


def validar_refresh_token(db: Session, token: str) -> Utilizador | None:
    """
    Valida um refresh token: existe na DB, não revogado, não expirado.
    Devolve o Utilizador associado ou None se inválido.
    """
    token_hash = hash_token(token)
    refresh = db.exec(
        select(TokenRefresh).where(
            TokenRefresh.token_hash == token_hash,
            TokenRefresh.revogado_at.is_(None),  # type: ignore[union-attr]
            TokenRefresh.expires_at > datetime.now(timezone.utc),
        )
    ).first()

    if not refresh:
        logger.info("Refresh token inválido: não encontrado, revogado ou expirado")
        return None

    utilizador = db.get(Utilizador, refresh.utilizador_id)
    if not utilizador:
        logger.info("Refresh token válido mas utilizador %s não encontrado", refresh.utilizador_id)
        return None
    if not utilizador.ativo or utilizador.deleted_at is not None:
        logger.info("Refresh token válido mas utilizador %s está inativo", utilizador.id)
        return None

    # A renovação é a porta por onde uma sessão sobrevive: sem esta verificação
    # a empresa suspensa continuava a renovar sessões indefinidamente.
    empresa = db.get(Empresa, utilizador.empresa_id)
    if empresa is not None and (empresa.suspenso or empresa.deleted_at is not None):
        logger.info("Refresh token válido mas a empresa %s está suspensa ou apagada", empresa.id)
        return None

    return utilizador


def revogar_sessoes_da_empresa(db: Session, empresa_id: uuid.UUID) -> int:
    """Revoga os refresh tokens ativos de todos os utilizadores de uma empresa.

    Chamado ao suspender: o portão de sessão passa a recusar os pedidos, mas as
    linhas de refresh continuariam válidas até expirar — e uma reativação
    seguinte reabria-as todas. Devolve quantas ficaram revogadas.
    """
    agora = datetime.now(timezone.utc)
    sessoes = db.exec(
        select(TokenRefresh)
        .join(Utilizador, Utilizador.id == TokenRefresh.utilizador_id)
        .where(
            Utilizador.empresa_id == empresa_id,
            TokenRefresh.revogado_at.is_(None),  # type: ignore[union-attr]
        )
    ).all()
    for sessao in sessoes:
        _terminar(sessao, agora)
        db.add(sessao)
    return len(sessoes)


# ---------------------------------------------------------------------------
# Registo: Empresa + admin num único passo atómico
# ---------------------------------------------------------------------------

def registar_empresa_e_admin(
    db: Session,
    dados: RegistarEmpresaSchema,
    request: Request | None = None,
) -> tuple[Empresa, Utilizador]:
    """
    Cria uma nova Empresa e o seu primeiro utilizador admin atomicamente.
    Apenas disponível quando DEPLOYMENT_MODE=saas.

    Returns:
        Tupla (Empresa, Utilizador) criados.

    Raises:
        HTTPException 409 se o email já estiver registado.
        HTTPException 403 se modo onprem.
    """
    if settings.DEPLOYMENT_MODE == "onprem":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registo público não disponível neste modo de instalação.",
        )

    # Verifica email único
    existente = db.exec(
        select(Utilizador).where(Utilizador.email == dados.admin_email.lower())
    ).first()
    if existente:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este email já está registado.",
        )

    # Empresa nova: ainda não tem política, vale o mínimo da plataforma.
    exigir_password_valida(dados.admin_password)

    # Cria Empresa
    empresa = Empresa(
        nome=cifrar_pii(dados.empresa_nome),
        nif=cifrar_pii(dados.empresa_nif),
        setor=dados.empresa_setor,
        dimensao=dados.empresa_dimensao,
        tipo_entidade=dados.empresa_tipo_entidade,
        nivel_qnrcs=dados.empresa_nivel_qnrcs,
    )
    db.add(empresa)
    db.flush()  # obter empresa.id sem commit

    # Cria admin
    agora = datetime.now(timezone.utc)
    admin = Utilizador(
        empresa_id=empresa.id,
        email=dados.admin_email.lower(),
        nome=cifrar_pii(dados.admin_nome),
        password_hash=hash_password(dados.admin_password),
        role=RoleUtilizador.ADMIN,
        consentimento_termos_at=agora,
        consentimento_termos_versao=dados.versao_termos,
    )
    db.add(admin)
    db.flush()  # obter admin.id
    registar_adesao(db, admin)

    registar_acao(
        db,
        acao=Acao.EMPRESA_REGISTADA,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa.id,
        utilizador_id=admin.id,
        entidade_tipo="Empresa",
        entidade_id=empresa.id,
        dados_novos={"admin_email": admin.email},
        request=request,
    )

    # Inicializa registos ControloEmpresaV2 para o framework da empresa
    # Import local para evitar circular import (controlos → empresas → auth)
    from app.controlos.service import inicializar_controlos_empresa
    inicializar_controlos_empresa(db, empresa.id)

    return empresa, admin


# ---------------------------------------------------------------------------
# Bloqueio de conta — anti-brute-force por conta
# ---------------------------------------------------------------------------

def _conta_bloqueada(utilizador: Utilizador | None, agora: datetime) -> bool:
    """True se a conta existe e ainda está dentro da janela de bloqueio."""
    if utilizador is None or utilizador.bloqueado_ate is None:
        return False
    ate = utilizador.bloqueado_ate
    if ate.tzinfo is None:  # valores lidos da DB podem vir sem tz
        ate = ate.replace(tzinfo=timezone.utc)
    return ate > agora


def _registar_tentativa_falhada(
    db: Session, utilizador: Utilizador, agora: datetime, request: Request | None
) -> None:
    """
    Conta a falha numa janela deslizante e, ao atingir o limite dentro dela,
    bloqueia a conta temporariamente. A janela reinicia quando a última falha foi
    há mais de LOGIN_JANELA_MINUTOS — evita trancar quem erra a password de forma
    esporádica.

    Persiste o próprio contador (db.commit): o chamador lança HTTPException logo a
    seguir e o get_session faz rollback da transação principal. O registar_acao com
    force_commit usa uma sessão INDEPENDENTE — grava só o log, não estas alterações —
    por isso sem este commit o contador nunca sobreviveria e o bloqueio nunca dispararia.
    """
    janela = utilizador.tentativas_janela_inicio
    if janela is not None and janela.tzinfo is None:  # valores da DB podem vir sem tz
        janela = janela.replace(tzinfo=timezone.utc)

    if janela is None or (agora - janela) > timedelta(
        minutes=settings.LOGIN_JANELA_MINUTOS
    ):
        # Fora da janela (ou primeira falha): recomeça a contagem.
        utilizador.tentativas_janela_inicio = agora
        utilizador.tentativas_falhadas = 1
    else:
        utilizador.tentativas_falhadas = (utilizador.tentativas_falhadas or 0) + 1

    if utilizador.tentativas_falhadas >= settings.LOGIN_MAX_TENTATIVAS:
        utilizador.bloqueado_ate = agora + timedelta(
            minutes=settings.LOGIN_BLOQUEIO_MINUTOS
        )
        utilizador.tentativas_falhadas = 0
        utilizador.tentativas_janela_inicio = None
        db.add(utilizador)
        registar_acao(
            db,
            acao=Acao.CONTA_BLOQUEADA,
            resultado=ResultadoAcao.FALHA,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            entidade_tipo="Utilizador",
            dados_novos={
                "email": utilizador.email,
                "minutos": settings.LOGIN_BLOQUEIO_MINUTOS,
            },
            request=request,
        )
    else:
        db.add(utilizador)
    db.commit()


def _limpar_bloqueio(db: Session, utilizador: Utilizador) -> None:
    """Autenticação válida: zera contador, janela e bloqueio, se existirem."""
    if (
        utilizador.tentativas_falhadas
        or utilizador.tentativas_janela_inicio
        or utilizador.bloqueado_ate
    ):
        utilizador.tentativas_falhadas = 0
        utilizador.tentativas_janela_inicio = None
        utilizador.bloqueado_ate = None
        db.add(utilizador)
        db.commit()


# ---------------------------------------------------------------------------
# Acumulador por IP — anti password-spray (entre contas)
# ---------------------------------------------------------------------------

def _hash_ip(ip: str) -> str:
    """Impressão com chave do IP — o acumulador por IP nunca guarda o endereço.

    Conta o cliente e não o endereço: em IPv6, o /64 inteiro (ver
    `chave_de_limite_ip`), senão quem tem um /64 mudava de endereço a cada
    tentativa e o acumulador nunca chegava ao limiar."""
    from app.shared.hashes import hash_ip_bloqueio
    from app.shared.utils import chave_de_limite_ip

    return hash_ip_bloqueio(chave_de_limite_ip(ip))


def _ip_bloqueado(db: Session, ip_hash: str, agora: datetime) -> bool:
    """True se este IP está dentro da janela de bloqueio por spray."""
    reg = db.exec(
        select(BloqueioIP).where(BloqueioIP.ip_hash == ip_hash)
    ).first()
    if reg is None or reg.bloqueado_ate is None:
        return False
    ate = reg.bloqueado_ate
    if ate.tzinfo is None:  # valores da DB podem vir sem tz
        ate = ate.replace(tzinfo=timezone.utc)
    return ate > agora


def _purgar_bloqueios_ip_antigos(db: Session, agora: datetime) -> None:
    """
    Remove linhas de IP já não bloqueadas e sem atividade há mais de um dia.
    Mantém a tabela limitada sem um job periódico — corre só quando surge um IP
    nunca visto (evento raro). Comparação de tz feita em Python (SQLite/Postgres).
    """
    limite = agora - timedelta(days=1)
    for reg in db.exec(
        select(BloqueioIP).where(BloqueioIP.bloqueado_ate.is_(None))  # type: ignore[union-attr]
    ).all():
        atu = reg.atualizado_em
        if atu is not None and atu.tzinfo is None:
            atu = atu.replace(tzinfo=timezone.utc)
        if atu is None or atu < limite:
            db.delete(reg)


def _registar_falha_ip(
    db: Session,
    ip_hash: str,
    agora: datetime,
    request: Request | None,
    empresa_id: uuid.UUID | None = None,
) -> None:
    """
    Conta uma falha de login para este IP (entre todas as contas, incl. emails
    inexistentes) numa janela deslizante; ao atingir o limiar bloqueia o IP.

    Persiste o próprio contador (db.commit): o chamador lança HTTPException logo a
    seguir e o get_session faz rollback da transação principal. O registar_acao com
    force_commit usa uma sessão INDEPENDENTE — grava só o log, não estas alterações —
    por isso sem este commit o contador nunca sobreviveria e o bloqueio nunca dispararia.

    `empresa_id` é preenchido pelo chamador APENAS quando a conta alvo já foi
    encontrada. Sem ele o registo não pertence a tenant nenhum e não aparece no
    ecrã de auditoria do cliente — um ataque contra a conta dele ficava invisível
    para ele. Quando a conta é desconhecida fica a NULL de propósito: escolher um
    tenant a partir de um email que não existe seria dizer ao atacante que ele
    existe.
    """
    reg = db.exec(
        select(BloqueioIP).where(BloqueioIP.ip_hash == ip_hash)
    ).first()
    if reg is None:
        _purgar_bloqueios_ip_antigos(db, agora)
        db.add(
            BloqueioIP(
                ip_hash=ip_hash, contador=1, janela_inicio=agora, atualizado_em=agora
            )
        )
        db.commit()
        return

    janela = reg.janela_inicio
    if janela is not None and janela.tzinfo is None:
        janela = janela.replace(tzinfo=timezone.utc)

    if janela is None or (agora - janela) > timedelta(
        minutes=settings.LOGIN_IP_JANELA_MINUTOS
    ):
        reg.contador = 1
        reg.janela_inicio = agora
        reg.bloqueado_ate = None
    else:
        reg.contador = (reg.contador or 0) + 1

    reg.atualizado_em = agora

    if reg.contador >= settings.LOGIN_IP_MAX_FALHAS:
        reg.bloqueado_ate = agora + timedelta(
            minutes=settings.LOGIN_IP_BLOQUEIO_MINUTOS
        )
        reg.contador = 0
        reg.janela_inicio = agora
        # Sem PII em dados_novos — o IP (cifrado) já entra pelo request.
        registar_acao(
            db,
            acao=Acao.IP_BLOQUEADO,
            resultado=ResultadoAcao.FALHA,
            empresa_id=empresa_id,
            entidade_tipo="Login",
            dados_novos={
                "motivo": "spray",
                "limiar": settings.LOGIN_IP_MAX_FALHAS,
                "minutos": settings.LOGIN_IP_BLOQUEIO_MINUTOS,
            },
            request=request,
        )
    db.add(reg)
    db.commit()


# ---------------------------------------------------------------------------
# Login passo 1: validação email + password
# ---------------------------------------------------------------------------

def exigir_conta_utilizavel(
    db: Session, utilizador: Utilizador, request: Request | None = None
) -> None:
    """A conta pode abrir sessão agora: ativa, não removida, e a empresa nem
    suspensa nem apagada. Senão 403, registado.

    O login faz-se em passos (password, depois o 2FA ou a troca da password
    temporária), e o token temporário de cada passo vale 5 minutos. Se a conta
    for desativada ou a empresa suspensa nesse intervalo, o passo seguinte tem de
    o ver: senão abria uma sessão que o resto da aplicação recusa, e a trilha
    ficava com um «login com sucesso» de quem já não podia entrar. As respostas
    só se dão a quem já provou a password, por isso não dizem nada sobre contas
    alheias.
    """
    if not utilizador.ativo or utilizador.deleted_at:
        registar_acao(
            db,
            acao=Acao.LOGIN_FALHA,
            resultado=ResultadoAcao.FALHA,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            dados_novos={"email": utilizador.email, "motivo": "conta_inativa"},
            request=request,
            force_commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Conta desativada. Contacte o administrador.",
        )

    # Empresa suspensa (ou apagada — conta como suspensa).
    empresa = db.get(Empresa, utilizador.empresa_id)
    if empresa and (empresa.suspenso or empresa.deleted_at is not None):
        registar_acao(
            db,
            acao=Acao.LOGIN_FALHA,
            resultado=ResultadoAcao.FALHA,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            dados_novos={"email": utilizador.email, "motivo": "empresa_suspensa"},
            request=request,
            force_commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A sua conta está temporariamente suspensa. Contacte o suporte.",
        )


def login_passo1(
    db: Session,
    email: str,
    password: str,
    request: Request | None = None,
    cookie_dispositivo: str | None = None,
) -> dict:
    """
    Valida credenciais e determina o próximo passo do fluxo de autenticação.

    Returns um dict com:
            - "tipo": "acesso_completo" | "2fa_necessario" | "2fa_configurar" | "password_temporaria"
      - "access_token" (se acesso_completo)
      - "temp_token" (se 2fa)
      - "utilizador" (se acesso_completo)
    """
    from app.auth import dispositivo, limites_login

    ip = None
    if request:
        from app.shared.audit import _extrair_ip
        ip = _extrair_ip(request)
    ip_hash = _hash_ip(ip) if ip else None

    agora = datetime.now(timezone.utc)

    utilizador = db.exec(
        select(Utilizador).where(Utilizador.email == email.lower())
    ).first()

    # Camada 2 — cookie de dispositivo válido para ESTE utilizador? Se sim, quem
    # chega é reconhecido: fica fora dos limites por endereço, tem uma vaga de
    # argon2 reservada e o seu próprio contador de falhas, e não é apanhado pelo
    # bloqueio por conta/IP que um atacante consegue provocar de propósito.
    nonce_dispositivo = None
    if utilizador is not None:
        nonce_dispositivo = dispositivo.validar(cookie_dispositivo, utilizador.id)
        if nonce_dispositivo and dispositivo.cookie_travado(db, nonce_dispositivo, agora):
            nonce_dispositivo = None
    prioritario = nonce_dispositivo is not None

    # Camada por IP (anti password-spray): se este IP já ultrapassou o limiar de
    # falhas entre contas, responde 429 sem sequer avaliar credenciais. Escopo de
    # IP — igual para qualquer email, logo não é oráculo de enumeração de contas.
    # Quem traz um cookie de dispositivo válido não é travado por aqui.
    if not prioritario and ip_hash and _ip_bloqueado(db, ip_hash, agora):
        registar_acao(
            db,
            acao=Acao.LOGIN_FALHA,
            resultado=ResultadoAcao.FALHA,
            entidade_tipo="Login",
            dados_novos={"motivo": "ip_bloqueado"},
            request=request,
            force_commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Demasiados pedidos. Tente novamente mais tarde.",
        )

    # Camada 1 (e 3): limites por endereço e por grupo (/48, /24) e prova de
    # trabalho só sob carga. Corre antes do argon2. Quem traz cookie conta só o
    # seu limite por cookie. Levanta 429 (ou 429 com o desafio a resolver).
    limites_login.guarda_login(
        request, prioritario=prioritario, agora=agora, nonce=nonce_dispositivo
    )

    # Bloqueio de conta: após LOGIN_MAX_TENTATIVAS falhas seguidas, a conta fica
    # bloqueada por LOGIN_BLOQUEIO_MINUTOS. Complementa o rate-limit por IP, que
    # não trava ataques distribuídos (muitos IPs contra a mesma conta). Quem traz
    # um cookie de dispositivo válido não é apanhado por aqui (o seu contador é o
    # do cookie).
    bloqueado = _conta_bloqueada(utilizador, agora) and not prioritario

    # Constant-time: verify_password é SEMPRE chamado — dummy quando o utilizador
    # não existe OU quando a conta está bloqueada — para o tempo de resposta não
    # revelar existência nem estado de bloqueio (CWE-208).
    password_valida = verify_password(
        password,
        utilizador.password_hash if (utilizador and not bloqueado) else _DUMMY_HASH,
        prioritario=prioritario,
    )

    # Conta bloqueada: resposta genérica idêntica à de credenciais inválidas — não
    # revela ao atacante que a conta existe nem que está bloqueada (o oráculo de
    # bloqueio fica só no log interno). Não incrementa (não estende o bloqueio).
    if bloqueado:
        if ip_hash:
            _registar_falha_ip(db, ip_hash, agora, request, utilizador.empresa_id)
        registar_acao(
            db,
            acao=Acao.LOGIN_FALHA,
            resultado=ResultadoAcao.FALHA,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            entidade_tipo="Utilizador",
            dados_novos={"email": utilizador.email, "motivo": "conta_bloqueada"},
            request=request,
            force_commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais inválidas.",
        )

    # Resposta HTTP genérica para não revelar se o email existe (anti-enumeração CWE-208).
    # O log interno inclui empresa_id/utilizador_id quando o utilizador é encontrado —
    # isso não expõe informação ao atacante (a resposta HTTP é idêntica nos dois casos).
    if not utilizador or not password_valida:
        _motivo = "email_nao_encontrado" if not utilizador else "password_incorreta"
        if prioritario:
            # Falha de um dispositivo reconhecido: conta para o cookie (ao fim de
            # N o cookie morre), não para o bloqueio da conta nem para o anti-spray
            # por IP — que um atacante provocaria de propósito contra a vítima.
            dispositivo.registar_falha_cookie(db, nonce_dispositivo, agora)
        else:
            if ip_hash:
                _registar_falha_ip(
                    db, ip_hash, agora, request,
                    utilizador.empresa_id if utilizador else None,
                )
            if utilizador:
                _registar_tentativa_falhada(db, utilizador, agora, request)
            if ip:
                limites_login.registar_falha(ip, agora)
        registar_acao(
            db,
            acao=Acao.LOGIN_FALHA,
            resultado=ResultadoAcao.FALHA,
            empresa_id=utilizador.empresa_id if utilizador else None,
            utilizador_id=utilizador.id if utilizador else None,
            entidade_tipo="Utilizador",
            dados_novos={"email": email, "motivo": _motivo},
            request=request,
            force_commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais inválidas.",
        )

    # Password correta — limpa qualquer contador/bloqueio residual da conta.
    _limpar_bloqueio(db, utilizador)

    # Migração de custo, no único instante em que é possível: a password em texto
    # limpo só existe aqui. Se os parâmetros do argon2 subirem, é este passo que
    # leva as contas antigas ao custo novo, uma a uma, à medida que entram — sem
    # ele, subir os parâmetros protegeria apenas quem se registasse depois.
    #
    # Falhar aqui não pode custar o login a ninguém: a password está correta e a
    # sessão é legítima. Um erro a regravar é um aviso, nunca um 500.
    if precisa_rehash(utilizador.password_hash):
        try:
            utilizador.password_hash = hash_password(password)
            db.add(utilizador)
            db.commit()
            logger.info(
                "hash de password migrado para os parâmetros atuais (utilizador %s)",
                utilizador.id,
            )
        except Exception:  # noqa: BLE001 — a sessão do utilizador vale mais
            db.rollback()
            logger.warning(
                "não foi possível migrar o hash de password do utilizador %s",
                utilizador.id, exc_info=True,
            )

    exigir_conta_utilizavel(db, utilizador, request)

    if utilizador.password_temporaria_ativa:
        temp_token = criar_temp_token(utilizador, "password_change_required")
        return {
            "tipo": "password_temporaria",
            "temp_token": temp_token,
            # O ecrã da troca mostra a regra antes de a pessoa escrever.
            "password_min": password_min(db, utilizador.empresa_id),
        }

    # 2FA obrigatório para todos os roles (Feature: MFA universal)
    if utilizador.totp_ativo:
        # Passo 2: verificar TOTP
        temp_token = criar_temp_token(utilizador, "2fa_pending")
        return {"tipo": "2fa_necessario", "temp_token": temp_token}

    # Qualquer utilizador sem 2FA configurado — forçar setup
    temp_token = criar_temp_token(utilizador, "2fa_setup_required")
    return {"tipo": "2fa_configurar", "temp_token": temp_token}


# ---------------------------------------------------------------------------
# Login passo 2: verificação TOTP / backup code
# ---------------------------------------------------------------------------

def login_passo2(
    db: Session,
    temp_token: str,
    codigo: str,
    request: Request | None = None,
    cookie_dispositivo: str | None = None,
) -> Utilizador:
    """
    Valida o código TOTP ou backup code após passo 1.
    Devolve o Utilizador autenticado ou lança 401.
    """
    from app.auth import dispositivo, limites_login

    payload = _decodificar_temp_token(temp_token, "2fa_pending")
    utilizador_id = uuid.UUID(payload["user_id"])

    utilizador = db.get(Utilizador, utilizador_id)
    if not utilizador or not utilizador.totp_ativo or not utilizador.totp_secret_cifrado:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Configuração 2FA inválida.",
        )
    # O passo 1 viu a conta como estava; entre os dois passos pode ter sido
    # desativada, ou a empresa suspensa.
    exigir_conta_utilizavel(db, utilizador, request)

    ip = None
    if request:
        from app.shared.audit import _extrair_ip
        ip = _extrair_ip(request)
    ip_hash = _hash_ip(ip) if ip else None

    agora = datetime.now(timezone.utc)

    # Camada 2 — cookie de dispositivo válido para este utilizador (conhecido pelo
    # token do passo 1). Reconhecido: fora dos limites por endereço, vaga de argon2
    # reservada, contador próprio.
    nonce_dispositivo = dispositivo.validar(cookie_dispositivo, utilizador.id)
    if nonce_dispositivo and dispositivo.cookie_travado(db, nonce_dispositivo, agora):
        nonce_dispositivo = None
    prioritario = nonce_dispositivo is not None

    # Camada 1 (e 3): limites por endereço/grupo e prova de trabalho sob carga.
    limites_login.guarda_login(
        request, prioritario=prioritario, agora=agora, nonce=nonce_dispositivo
    )

    # Camada por IP (anti spray): 429 se o IP já ultrapassou o limiar. Aqui a
    # conta já é conhecida (veio no token do passo 1), por isso o registo fica
    # associada a ela — ao contrário do passo 1, onde o IP é travado antes de
    # sequer se procurar o email.
    if not prioritario and ip_hash and _ip_bloqueado(db, ip_hash, agora):
        registar_acao(
            db,
            acao=Acao.LOGIN_FALHA,
            resultado=ResultadoAcao.FALHA,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            entidade_tipo="Login",
            dados_novos={"motivo": "ip_bloqueado"},
            request=request,
            force_commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Demasiados pedidos. Tente novamente mais tarde.",
        )

    # O mesmo contador de bloqueio protege o passo 2 contra força bruta ao código
    # TOTP (6 dígitos). Resposta genérica quando bloqueado (não revela o estado).
    # Quem traz um cookie de dispositivo válido não é apanhado por aqui.
    if not prioritario and _conta_bloqueada(utilizador, agora):
        registar_acao(
            db,
            acao=Acao.LOGIN_FALHA,
            resultado=ResultadoAcao.FALHA,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            entidade_tipo="Utilizador",
            dados_novos={"email": utilizador.email, "motivo": "conta_bloqueada"},
            request=request,
            force_commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Código 2FA inválido.",
        )

    secret = decifrar_totp_secret(utilizador.totp_secret_cifrado)

    # Tenta TOTP primeiro. Um código certo mas já usado conta como errado.
    codigo_limpo = codigo.strip().replace(" ", "")
    passo = passo_totp(secret, codigo_limpo)
    if passo is not None and consumir_passo_totp(db, Utilizador, utilizador.id, passo):
        _limpar_bloqueio(db, utilizador)
        registar_acao(
            db,
            acao=Acao.FA2_VERIFICADO,
            resultado=ResultadoAcao.SUCESSO,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            dados_novos={"email": utilizador.email},
            request=request,
        )
        registar_acao(
            db,
            acao=Acao.LOGIN_SUCESSO,
            resultado=ResultadoAcao.SUCESSO,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador.id,
            dados_novos={"email": utilizador.email},
            request=request,
        )
        return utilizador

    # Tenta backup code — só quando o código tem a forma de um. Um TOTP errado
    # (6 dígitos) nunca bate com um hash de backup, e verificar os dez hashes
    # argon2id à mesma custava perto de um segundo de CPU por tentativa: um
    # atacante a martelar o passo 2 punha o servidor a pagar a conta.
    codigo_normalizado = normalizar_codigo_backup(codigo_limpo)
    codigos: list[CodigoBackup2FA] = []
    if tem_formato_de_codigo_backup(codigo_normalizado):
        codigos = list(db.exec(
            select(CodigoBackup2FA).where(
                CodigoBackup2FA.utilizador_id == utilizador_id,
                CodigoBackup2FA.usado_at.is_(None),  # type: ignore[union-attr]
            )
        ).all())

    for c in codigos:
        if verify_password(codigo_normalizado, c.codigo_hash, prioritario=prioritario):
            _limpar_bloqueio(db, utilizador)
            c.usado_at = datetime.now(timezone.utc)
            db.add(c)
            registar_acao(
                db,
                acao=Acao.BACKUP_CODE_USADO,
                resultado=ResultadoAcao.SUCESSO,
                empresa_id=utilizador.empresa_id,
                utilizador_id=utilizador.id,
                entidade_id=c.id,
                request=request,
            )
            registar_acao(
                db,
                acao=Acao.LOGIN_SUCESSO,
                resultado=ResultadoAcao.SUCESSO,
                empresa_id=utilizador.empresa_id,
                utilizador_id=utilizador.id,
                dados_novos={"email": utilizador.email},
                request=request,
            )
            return utilizador

    # Falhou. Se veio de um dispositivo reconhecido, conta para o cookie; senão
    # para o bloqueio por conta (trava força bruta ao TOTP) e por IP.
    if prioritario:
        dispositivo.registar_falha_cookie(db, nonce_dispositivo, agora)
    else:
        if ip_hash:
            _registar_falha_ip(db, ip_hash, agora, request, utilizador.empresa_id)
        _registar_tentativa_falhada(db, utilizador, agora, request)
        if ip:
            limites_login.registar_falha(ip, agora)
    registar_acao(
        db,
        acao=Acao.FA2_FALHOU,
        resultado=ResultadoAcao.FALHA,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        dados_novos={"email": utilizador.email, "motivo": "codigo_invalido"},
        request=request,
        force_commit=True,
    )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Código 2FA inválido.",
    )


# ---------------------------------------------------------------------------
# Refresh: troca refresh token por novo access token
# ---------------------------------------------------------------------------

def _recusar_token_revogado(
    db: Session, registo: TokenRefresh, request: Request | None
) -> None:
    """Recusa (401) um refresh que já não vale. Se for um token rodado que volta
    a aparecer, alguém tem uma cópia dele: terminam todas as sessões da conta,
    também a que descende da cópia, e fica registado.

    Duas exceções, que dão só o 401:
      - uma sessão terminada (logout, password mudada, conta suspensa): um
        browser que ainda a guardava apresenta-a sem mal nenhum;
      - um token rodado há menos de `REFRESH_REUTILIZACAO_TOLERANCIA_S`: dois
        separadores que renovam ao mesmo tempo, ou um pedido repetido depois de
        a resposta se perder.
    """
    agora = datetime.now(timezone.utc)
    revogado = _utc(registo.revogado_at)
    expira = _utc(registo.expires_at)
    rodado = expira is not None and revogado is not None and expira > revogado
    tolerancia = timedelta(seconds=settings.REFRESH_REUTILIZACAO_TOLERANCIA_S)
    if rodado and agora - revogado > tolerancia:
        utilizador = db.get(Utilizador, registo.utilizador_id)
        terminadas = terminar_sessoes(db, registo.utilizador_id)
        registar_acao(
            db,
            acao=Acao.LOGIN_FALHA,
            resultado=ResultadoAcao.FALHA,
            empresa_id=utilizador.empresa_id if utilizador else None,
            utilizador_id=registo.utilizador_id,
            entidade_tipo="Utilizador",
            dados_novos={"motivo": "refresh_reutilizado", "sessoes_terminadas": terminadas},
            request=request,
        )
        # Grava-se já: o 401 a seguir faz rollback da transação do pedido, e
        # sessões que continuassem vivas depois do alarme não serviam de nada.
        db.commit()
        logger.warning(
            "Refresh token já rodado apresentado de novo (utilizador %s): %d sessão(ões) terminada(s).",
            registo.utilizador_id, terminadas,
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Sessão expirada. Por favor, faça login novamente.",
    )


def renovar_access_token(
    db: Session, refresh_token: str, request: Request | None = None
) -> tuple[str, str, Utilizador]:
    """
    Valida o refresh token do cookie e emite novo access token + novo refresh token.
    O refresh token anterior é revogado (rotação — CWE-384), e um token já rodado
    que volte a aparecer termina as sessões da conta (ver `_recusar_token_revogado`).
    Lança 401 se o token for inválido, revogado ou expirado.

    Returns:
        Tupla (access_token, novo_refresh_token, utilizador).
    """
    from sqlalchemy import update

    token_hash = hash_token(refresh_token)
    registo = db.exec(
        select(TokenRefresh).where(TokenRefresh.token_hash == token_hash)
    ).first()
    if registo is not None and registo.revogado_at is not None:
        _recusar_token_revogado(db, registo, request)

    utilizador = validar_refresh_token(db, refresh_token)
    if not utilizador:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão expirada. Por favor, faça login novamente.",
        )

    # Rotação: o token antigo só é revogado se ainda estiver vivo. Dois pedidos
    # com o mesmo token disputam a mesma linha e só um a altera; o outro leva 401.
    # Ler primeiro e revogar depois deixava os dois passar, e um token dava duas
    # sessões — uma delas podia ser de quem o copiou.
    rodado = db.execute(
        update(TokenRefresh)
        .where(
            TokenRefresh.token_hash == token_hash,
            TokenRefresh.revogado_at.is_(None),  # type: ignore[union-attr]
        )
        .values(revogado_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    if rodado.rowcount != 1:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão expirada. Por favor, faça login novamente.",
        )
    novo_refresh_token = criar_refresh_token(db, utilizador, request)

    registar_acao(
        db,
        acao=Acao.REFRESH_TOKEN,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        request=request,
    )
    return criar_access_token(utilizador), novo_refresh_token, utilizador


# ---------------------------------------------------------------------------
# Logout: revoga refresh token
# ---------------------------------------------------------------------------

def logout(
    db: Session,
    refresh_token: str,
    utilizador: Utilizador,
    request: Request | None = None,
) -> None:
    """Revoga o refresh token e regista o logout no AuditLog."""
    revogar_refresh_token(db, refresh_token)
    registar_acao(
        db,
        acao=Acao.LOGOUT,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        dados_novos={"email": utilizador.email},
        request=request,
    )


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

async def _enviar_reset_sem_revelar(email: str, link: str, locale: str | None) -> None:
    """Envia o email de recuperação depois da resposta; uma falha fica só no registo."""
    from app.shared.email import enviar_email_reset_password

    try:
        await enviar_email_reset_password(email, link, locale=locale)
    except Exception:  # noqa: BLE001 — o pedido já respondeu; não há a quem propagar
        logger.exception("Falha a enviar o email de recuperação de password.")


def _gravar_pedido_de_reset(
    motor, utilizador_id: uuid.UUID, token: str, ip: str | None, user_agent: str | None
) -> tuple[str, str | None]:
    """Grava o token de recuperação e a entrada na trilha, numa sessão própria.
    Devolve o link do email e a língua da empresa. Corre numa thread, depois da
    resposta."""
    with Session(motor) as db:
        utilizador = db.get(Utilizador, utilizador_id)
        db.add(PasswordResetToken(
            utilizador_id=utilizador_id,
            token_hash=hash_token(token),
            ip_address=cifrar_pii(ip) if ip else None,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ))
        registar_acao(
            db,
            acao=Acao.PASSWORD_RESET_PEDIDO,
            resultado=ResultadoAcao.SUCESSO,
            empresa_id=utilizador.empresa_id,
            utilizador_id=utilizador_id,
            ip_address=ip,
            user_agent=user_agent,
        )
        db.commit()
        empresa = db.get(Empresa, utilizador.empresa_id)
        # Lido fresco: o wizard de HTTPS pode mudar o APP_URL (esquema/domínio) sem
        # reiniciar o processo; o link do email tem de refletir o valor atual.
        link = f"{get_settings().APP_URL}/reset-password?token={token}"
        return link, getattr(empresa, "locale_preferido", None)


async def _concluir_pedido_de_reset(
    motor, utilizador_id: uuid.UUID, token: str, email: str, ip: str | None, user_agent: str | None
) -> None:
    """Depois da resposta: grava o pedido (numa thread, fora do event loop) e só
    então manda o email. Uma falha fica só no registo — sem token gravado não
    sai email, e o pedido já respondeu."""
    from starlette.concurrency import run_in_threadpool

    try:
        link, locale = await run_in_threadpool(
            _gravar_pedido_de_reset, motor, utilizador_id, token, ip, user_agent
        )
    except Exception:  # noqa: BLE001 — o pedido já respondeu; não há a quem propagar
        logger.exception("Falha a registar o pedido de recuperação de password.")
        return
    await _enviar_reset_sem_revelar(email, link, locale)


def solicitar_reset_password(
    db: Session,
    email: str,
    tarefas: BackgroundTasks,
    request: Request | None = None,
) -> None:
    """
    Cria token de reset e agenda o email via provedor configurado (SMTP ou Resend).
    Resposta sempre genérica — não revela se o email existe (evita user enumeration).

    Antes da resposta, os dois casos fazem o mesmo trabalho: uma leitura. Tudo o
    que só acontece quando a conta existe — gravar o token, a trilha (que tranca
    a cabeça da cadeia da empresa), o commit, o envio do email — corre depois, no
    `BackgroundTasks`. Medido: com a escrita antes da resposta, o caso «existe»
    demorava ~30 ms a mais, e um só pedido chegava para distinguir os dois.
    """
    utilizador = db.exec(
        select(Utilizador).where(Utilizador.email == email.lower())
    ).first()

    if utilizador and utilizador.ativo and not utilizador.deleted_at:
        ip = user_agent = None
        if request:
            from app.shared.audit import _extrair_ip
            ip = _extrair_ip(request)
            user_agent = request.headers.get("user-agent", "")
        tarefas.add_task(
            _concluir_pedido_de_reset,
            db.get_bind(), utilizador.id, gerar_token_opaco(32), email, ip, user_agent,
        )


def _reset_e_utilizador(db: Session, token: str) -> tuple[PasswordResetToken, Utilizador]:
    """O pedido de reset por usar e a conta dele; 400 se o token não servir."""
    token_hash = hash_token(token)
    reset = db.exec(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.usado_at.is_(None),  # type: ignore[union-attr]
            PasswordResetToken.expires_at > datetime.now(timezone.utc),
        )
    ).first()

    if not reset:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token inválido ou expirado.",
        )

    utilizador = db.get(Utilizador, reset.utilizador_id)
    if not utilizador or not utilizador.ativo:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token inválido.",
        )
    return reset, utilizador


def regras_reset_password(db: Session, token: str) -> int:
    """O comprimento mínimo que o reset deste token vai exigir.

    Quem chega pelo link do email não tem sessão e o ecrã não sabe de que empresa
    é a conta; sem isto mostraria a regra da plataforma e o servidor recusaria
    com a da empresa. Só responde a quem tem um token válido.
    """
    _, utilizador = _reset_e_utilizador(db, token)
    return password_min(db, utilizador.empresa_id)


def confirmar_reset_password(
    db: Session, token: str, nova_password: str, request: Request | None = None
) -> None:
    """
    Valida o token de reset e atualiza a password.
    Lança 400 se inválido, expirado ou já usado.
    """
    reset, utilizador = _reset_e_utilizador(db, token)

    # A mesma regra dos outros fluxos, com o mínimo da empresa, e sem reutilizar a
    # password atual (CWE-521).
    exigir_password_valida(nova_password, db=db, empresa_id=utilizador.empresa_id)
    if verify_password(nova_password, utilizador.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A nova password não pode ser igual à password atual.",
        )

    # Atualizar password
    utilizador.password_hash = hash_password(nova_password)
    utilizador.updated_at = datetime.now(timezone.utc)
    reset.usado_at = datetime.now(timezone.utc)
    # Quem recuperou a password pelo email escolheu-a: se havia uma temporária
    # (um reset do administrador), deixou de haver — senão o login seguinte
    # mandava mudar outra vez a password que acabou de escolher.
    utilizador.password_temporaria_ativa = False

    # O reset é uma via de recuperação legítima — levanta qualquer bloqueio ativo.
    utilizador.tentativas_falhadas = 0
    utilizador.tentativas_janela_inicio = None
    utilizador.bloqueado_ate = None

    db.add(utilizador)
    db.add(reset)

    # Terminar todas as sessões e os outros pedidos de recuperação pendentes.
    terminar_sessoes(db, utilizador.id)

    registar_acao(
        db,
        acao=Acao.PASSWORD_RESET_CONFIRMADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        request=request,
    )


# ---------------------------------------------------------------------------
# Configuração e ativação de 2FA (TOTP)
# ---------------------------------------------------------------------------

def setup_2fa(
    db: Session, utilizador: Utilizador, request: Request | None = None
) -> tuple[str, list[str]]:
    """
    Gera novo segredo TOTP e backup codes.
    O 2FA NÃO fica ativo — requer confirmação via ativar_2fa().

    Só com o 2FA inativo (409 se já estiver ativo). O segredo novo e os códigos
    novos valem logo — é o que torna possível confirmar a seguir —, por isso,
    com um 2FA ativo, bastava um access token roubado para trocar o
    autenticador do dono pelo de outra pessoa e apagar os códigos de recuperação
    dele. O ecrã só oferece a configuração com o 2FA inativo; quem precisa de
    mudar de autenticador pede a reposição ao administrador (ou, se for o
    administrador, desativa-o primeiro com a password).

    Returns:
        Tuple (totp_uri, backup_codes) — mostrar ao utilizador UMA vez.
    """
    import pyotp

    if utilizador.totp_ativo:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_2FA_JA_ATIVO,
        )

    # Gerar segredo TOTP novo
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)

    # URI para QR code no frontend (otpauth://totp/...)
    totp_uri = totp.provisioning_uri(
        name=utilizador.email,
        issuer_name=settings.APP_NAME,
    )

    # Guardar segredo cifrado (ainda não ativo)
    utilizador.totp_secret_cifrado = cifrar_totp_secret(secret)
    utilizador.updated_at = datetime.now(timezone.utc)
    db.add(utilizador)

    # Gerar backup codes e guardar hashes
    codigos_texto = gerar_codigos_backup(10)

    # Remover backup codes antigos se existirem
    codigos_antigos = db.exec(
        select(CodigoBackup2FA).where(
            CodigoBackup2FA.utilizador_id == utilizador.id
        )
    ).all()
    for c in codigos_antigos:
        db.delete(c)

    # Guardar novos hashes
    for codigo in codigos_texto:
        normalizado = normalizar_codigo_backup(codigo)
        codigo_hash = hash_password(normalizado)
        db.add(
            CodigoBackup2FA(
                utilizador_id=utilizador.id,
                codigo_hash=codigo_hash,
            )
        )

    # O segredo tem de estar gravado antes de o cliente o receber: a sessão do
    # pedido só faz commit depois de a resposta sair, e uma confirmação que
    # chegue nesse intervalo compararia o código com o segredo anterior.
    db.commit()

    return totp_uri, codigos_texto


def ativar_2fa(
    db: Session,
    utilizador: Utilizador,
    codigo_totp: str,
    request: Request | None = None,
) -> None:
    """
    Valida o código TOTP e ativa o 2FA para o utilizador.
    Lança 400 se o código for inválido, 409 se o 2FA já estiver ativo.

    Termina as sessões que existiam: quem chama e precisa de continuar com
    sessão cria-a depois disto.
    """
    if utilizador.totp_ativo:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MSG_2FA_JA_ATIVO,
        )
    if not utilizador.totp_secret_cifrado:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configure primeiro o 2FA antes de o ativar.",
        )

    secret = decifrar_totp_secret(utilizador.totp_secret_cifrado)
    passo = passo_totp(secret, codigo_totp)
    if passo is None or not consumir_passo_totp(db, Utilizador, utilizador.id, passo):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Código TOTP inválido. Verifique a hora do seu dispositivo e tente novamente.",
        )

    utilizador.totp_ativo = True
    utilizador.updated_at = datetime.now(timezone.utc)
    db.add(utilizador)
    terminadas = terminar_sessoes(db, utilizador.id)

    registar_acao(
        db,
        acao=Acao.FA2_ATIVADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        dados_novos={"email": utilizador.email, "sessoes_terminadas": terminadas},
        request=request,
    )


def desativar_2fa(
    db: Session,
    utilizador: Utilizador,
    password_atual: str,
    request: Request | None = None,
) -> None:
    """
    Desativa 2FA para o utilizador após confirmação com password.
    Apenas para utilizadores com role admin (verificado no router).

    Termina as sessões que existiam: quem chama e precisa de continuar com
    sessão cria-a depois disto.
    """
    if not verify_password(password_atual, utilizador.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password incorreta.",
        )

    utilizador.totp_ativo = False
    utilizador.totp_secret_cifrado = None
    utilizador.updated_at = datetime.now(timezone.utc)
    db.add(utilizador)

    # Apagar backup codes
    codigos = db.exec(
        select(CodigoBackup2FA).where(
            CodigoBackup2FA.utilizador_id == utilizador.id
        )
    ).all()
    for c in codigos:
        db.delete(c)
    terminadas = terminar_sessoes(db, utilizador.id)

    registar_acao(
        db,
        acao=Acao.FA2_DESATIVADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador.empresa_id,
        utilizador_id=utilizador.id,
        dados_novos={"sessoes_terminadas": terminadas},
        request=request,
    )


# ---------------------------------------------------------------------------
# Higiene: tokens de refresh e de reset que já não servem para nada
# ---------------------------------------------------------------------------

TOKENS_RETENCAO_DIAS = 30


def purgar_tokens_expirados(db: Session, dias: int = TOKENS_RETENCAO_DIAS) -> dict[str, int]:
    """Apaga refresh tokens expirados ou revogados e tokens de reset expirados
    ou usados há mais de `dias`.

    Nunca eram apagados: a tabela crescia a cada login e um token revogado há
    um ano continuava lá. Fica uma margem de `dias` depois de deixarem de
    valer — o suficiente para uma investigação recente, e nada mais.
    """
    from sqlalchemy import delete, or_

    agora = datetime.now(timezone.utc)
    limite = agora - timedelta(days=dias)
    refresh = db.exec(  # type: ignore[call-overload]
        delete(TokenRefresh).where(
            or_(TokenRefresh.expires_at < limite, TokenRefresh.revogado_at < limite)
        )
    )
    reset = db.exec(  # type: ignore[call-overload]
        delete(PasswordResetToken).where(
            or_(PasswordResetToken.expires_at < limite, PasswordResetToken.usado_at < limite)
        )
    )
    db.commit()
    return {"refresh": refresh.rowcount or 0, "reset": reset.rowcount or 0}
