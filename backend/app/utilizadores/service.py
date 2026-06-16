"""
Serviço do módulo de utilizadores.
Toda a lógica de negócio de gestão de utilizadores (CRUD, RBAC, RGPD).

Regras de isolamento:
  - Toda a query filtra sempre por empresa_id do utilizador autenticado.
  - Admin vê e gere todos os utilizadores da sua empresa.
  - Utilizador não-admin só pode ver e editar o seu próprio perfil.
  - Anonimização RGPD: remove dados pessoais mas mantém AuditLogs
    com um UUID anonimizado (Art. 17(3)(b) RGPD).
"""
import uuid
from datetime import datetime, timezone
import secrets
import string

from fastapi import HTTPException, Request, status
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.auth.models import CodigoBackup2FA, RoleUtilizador, TokenRefresh, Utilizador
from app.auth.service import hash_password, verify_password as verificar_password
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.pii import cifrar_pii, decifrar_pii
from app.utilizadores.schemas import (
    AlterarPasswordSchema,
    AlterarRoleSchema,
    AtualizarPerfilSchema,
    CriarUtilizadorSchema,
    ImplementadorSchema,
    ListaUtilizadoresSchema,
    UtilizadorSchema,
)


# ---------------------------------------------------------------------------
# Helpers privados
# ---------------------------------------------------------------------------


def _get_utilizador_ou_404(
    db: Session, utilizador_id: uuid.UUID, empresa_id: uuid.UUID
) -> Utilizador:
    """
    Devolve utilizador pelo ID dentro do tenant.
    Lança 404 se não encontrado ou pertencer a outro tenant.
    """
    u = db.exec(
        select(Utilizador).where(
            Utilizador.id == utilizador_id,
            Utilizador.empresa_id == empresa_id,
            Utilizador.deleted_at.is_(None),  # type: ignore[attr-defined]
        )
    ).first()
    if not u:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Utilizador não encontrado.",
        )
    return u


def _verificar_permissao_gestao(
    utilizador_atual: Utilizador,
    alvo: Utilizador,
) -> None:
    """
    Verifica se o utilizador_atual pode gerir o utilizador alvo.
    Regras:
      - Admin e SubAdmin podem gerir outros utilizadores (com restrições de hierarquia).
      - Qualquer utilizador pode ver/editar o próprio perfil.
      - Nenhum utilizador pode gerir utilizadores de outro tenant.
    """
    if utilizador_atual.empresa_id != alvo.empresa_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sem permissão.",
        )
    roles_gestores = (RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN)
    if utilizador_atual.role not in roles_gestores and utilizador_atual.id != alvo.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Apenas admins podem gerir outros utilizadores.",
        )


def _verificar_hierarquia_roles(
    atual: Utilizador,
    alvo: Utilizador,
) -> None:
    """
    Verifica se o utilizador atual tem hierarquia suficiente para realizar
    operações destrutivas sobre o alvo (alterar role, desativar, reset, etc.).

    Regras:
      - Ninguém pode operar sobre um Admin de outra forma que não seja o próprio Admin.
      - SubAdmin não pode operar sobre Admin nem sobre outro SubAdmin.
    """
    roles_protegidos: list[RoleUtilizador] = [RoleUtilizador.ADMIN]
    if atual.role == RoleUtilizador.SUBADMIN:
        roles_protegidos.append(RoleUtilizador.SUBADMIN)
    if alvo.role in roles_protegidos:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sem permissão para gerir este utilizador.",
        )


# ---------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------


def listar_utilizadores(
    db: Session,
    empresa_id: uuid.UUID,
    utilizador_atual: Utilizador,
    so_ativos: bool = False,
    q: str | None = None,
    role: RoleUtilizador | None = None,
    limite: int | None = None,
    offset: int = 0,
) -> ListaUtilizadoresSchema:
    """
    Lista utilizadores do tenant.
    Admin: vê todos.
    Outros roles: apenas o próprio.
    """
    roles_gestores = (RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN)
    if utilizador_atual.role in roles_gestores:
        filtros = [
            Utilizador.empresa_id == empresa_id,
            # Mostra utilizadores não apagados OU anonimizados (devem aparecer no painel)
            or_(
                Utilizador.deleted_at.is_(None),  # type: ignore[attr-defined]
                Utilizador.anonimizado_at.is_not(None),  # type: ignore[attr-defined]
            ),
        ]
        if so_ativos:
            filtros.append(Utilizador.ativo.is_(True))  # type: ignore[attr-defined]
        if role is not None:
            filtros.append(Utilizador.role == role)

        query = (
            select(Utilizador)
            .where(*filtros)
            .order_by(Utilizador.created_at.desc())
        )

        termo = q.strip().lower() if q and q.strip() else None
        if termo:
            utilizadores = db.exec(query).all()
            utilizadores = [
                utilizador
                for utilizador in utilizadores
                if (
                    termo in utilizador.email.lower()
                    or termo in utilizador.role.value.lower()
                    or termo in decifrar_pii(utilizador.nome).lower()
                )
            ]
            total = len(utilizadores)
            if offset:
                utilizadores = utilizadores[offset:]
            if limite is not None:
                utilizadores = utilizadores[:limite]
        else:
            total = db.exec(
                select(func.count())
                .select_from(Utilizador)
                .where(*filtros)
            ).one()
            if offset:
                query = query.offset(offset)
            if limite is not None:
                query = query.limit(limite)
            utilizadores = db.exec(query).all()
    else:
        utilizadores = [utilizador_atual]
        termo = q.strip().lower() if q and q.strip() else None
        if role is not None and utilizador_atual.role != role:
            utilizadores = []
        if so_ativos and not utilizador_atual.ativo:
            utilizadores = []
        if termo:
            utilizadores = [
                utilizador
                for utilizador in utilizadores
                if (
                    termo in utilizador.email.lower()
                    or termo in utilizador.role.value.lower()
                    or termo in decifrar_pii(utilizador.nome).lower()
                )
            ]
        total = len(utilizadores)
        if offset:
            utilizadores = utilizadores[offset:]
        if limite is not None:
            utilizadores = utilizadores[:limite]

    return ListaUtilizadoresSchema(
        total=total,
        utilizadores=[UtilizadorSchema.model_validate(u) for u in utilizadores],
    )


def listar_implementadores(
    db: Session,
    empresa_id: uuid.UUID,
) -> list[ImplementadorSchema]:
    """
    Devolve implementadores ativos da empresa (usados para delegação de controlos).
    Apenas admin chama este endpoint.
    """
    implementadores = db.exec(
        select(Utilizador).where(
            Utilizador.empresa_id == empresa_id,
            Utilizador.role == RoleUtilizador.IMPLEMENTADOR,
            Utilizador.ativo.is_(True),  # type: ignore[attr-defined]
            Utilizador.deleted_at.is_(None),  # type: ignore[attr-defined]
        )
    ).all()
    return [ImplementadorSchema.model_validate(u) for u in implementadores]


# ---------------------------------------------------------------------------
# CRUD individual
# ---------------------------------------------------------------------------


def get_utilizador(
    db: Session,
    utilizador_id: uuid.UUID,
    empresa_id: uuid.UUID,
    utilizador_atual: Utilizador,
) -> UtilizadorSchema:
    """
    Devolve dados de um utilizador.
    Admin pode ver qualquer utilizador da empresa.
    Outros só podem ver o próprio.
    """
    alvo = _get_utilizador_ou_404(db, utilizador_id, empresa_id)
    _verificar_permissao_gestao(utilizador_atual, alvo)
    return UtilizadorSchema.model_validate(alvo)


def criar_utilizador(
    db: Session,
    dados: CriarUtilizadorSchema,
    empresa_id: uuid.UUID,
    criador: Utilizador,
    request: Request | None = None,
) -> UtilizadorSchema:
    """
    Admin ou SubAdmin cria um novo utilizador na empresa.
    - Admin não pode criar outro admin (restrição no schema).
    - SubAdmin não pode criar admin nem subadmin (restrição no service).
    """
    # SubAdmin não pode criar Admin ou SubAdmin
    if criador.role == RoleUtilizador.SUBADMIN and dados.role in (
        RoleUtilizador.ADMIN,
        RoleUtilizador.SUBADMIN,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sub-administradores não podem criar utilizadores com este perfil.",
        )

    # Verifica email único (global — emails são únicos na plataforma)
    existente = db.exec(
        select(Utilizador).where(Utilizador.email == dados.email)
    ).first()
    if existente:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este email já está registado.",
        )

    novo = Utilizador(
        empresa_id=empresa_id,
        email=dados.email,
        nome=cifrar_pii(dados.nome),
        password_hash=hash_password(dados.password),
        role=dados.role,
        ativo=True,
    )
    db.add(novo)
    db.flush()

    registar_acao(
        db,
        acao=Acao.UTILIZADOR_CRIADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=criador.id,
        entidade_tipo="Utilizador",
        entidade_id=novo.id,
        dados_novos={"email": novo.email, "role": novo.role.value},
        request=request,
    )

    db.commit()
    db.refresh(novo)
    return UtilizadorSchema.model_validate(novo)


def atualizar_perfil(
    db: Session,
    utilizador_id: uuid.UUID,
    dados: AtualizarPerfilSchema,
    empresa_id: uuid.UUID,
    utilizador_atual: Utilizador,
    request: Request | None = None,
) -> UtilizadorSchema:
    """
    Atualiza dados de perfil.
    Admin pode atualizar qualquer utilizador da empresa.
    Utilizador pode atualizar o próprio nome.
    """
    alvo = _get_utilizador_ou_404(db, utilizador_id, empresa_id)
    _verificar_permissao_gestao(utilizador_atual, alvo)

    from app.shared.pii import decifrar_pii

    dados_anteriores: dict = {}
    dados_novos: dict = {}
    houve_alteracao_nome = False
    houve_alteracao_role = False
    houve_alteracao_estado = False

    if dados.nome is not None:
        nome_atual_decifrado = decifrar_pii(alvo.nome)
        if dados.nome != nome_atual_decifrado:
            # Não logamos nomes em AuditLog (campo PII cifrado)
            dados_anteriores["nome_alterado"] = True
            dados_novos["nome_alterado"] = True
            alvo.nome = cifrar_pii(dados.nome)
            houve_alteracao_nome = True

    if dados.role is not None and dados.role != alvo.role:
        if utilizador_atual.role not in (RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Apenas administradores podem alterar o papel.",
            )
        if dados.role == RoleUtilizador.ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Não é possível promover um utilizador a admin por este endpoint.",
            )
        # SubAdmin não pode promover para SubAdmin
        if utilizador_atual.role == RoleUtilizador.SUBADMIN and dados.role == RoleUtilizador.SUBADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Sub-administradores não podem atribuir este perfil.",
            )
        # Verifica hierarquia sobre o alvo
        _verificar_hierarquia_roles(utilizador_atual, alvo)
        if alvo.id == utilizador_atual.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Não pode alterar o seu próprio role.",
            )
        role_anterior = alvo.role
        alvo.role = dados.role
        houve_alteracao_role = True

    if dados.ativo is not None and dados.ativo != alvo.ativo:
        if utilizador_atual.role not in (RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Apenas administradores podem alterar o estado da conta.",
            )
        if alvo.id == utilizador_atual.id and not dados.ativo:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Não pode desativar a sua própria conta.",
            )
        # Verifica hierarquia
        _verificar_hierarquia_roles(utilizador_atual, alvo)
        ativo_anterior = alvo.ativo
        alvo.ativo = dados.ativo
        houve_alteracao_estado = True

    if not (
        houve_alteracao_nome
        or houve_alteracao_role
        or houve_alteracao_estado
    ):
        return UtilizadorSchema.model_validate(alvo)

    alvo.updated_at = datetime.now(timezone.utc)
    db.add(alvo)

    if houve_alteracao_nome:
        registar_acao(
            db,
            acao=Acao.EMPRESA_DADOS_ATUALIZADOS,
            resultado=ResultadoAcao.SUCESSO,
            empresa_id=empresa_id,
            utilizador_id=utilizador_atual.id,
            entidade_tipo="Utilizador",
            entidade_id=alvo.id,
            dados_anteriores=dados_anteriores,
            dados_novos=dados_novos,
            request=request,
        )

    if houve_alteracao_role:
        registar_acao(
            db,
            acao=Acao.UTILIZADOR_ROLE_ALTERADO,
            resultado=ResultadoAcao.SUCESSO,
            empresa_id=empresa_id,
            utilizador_id=utilizador_atual.id,
            entidade_tipo="Utilizador",
            entidade_id=alvo.id,
            dados_anteriores={"role": role_anterior.value},
            dados_novos={"role": alvo.role.value},
            request=request,
        )

    if houve_alteracao_estado:
        registar_acao(
            db,
            acao=(
                Acao.UTILIZADOR_REATIVADO
                if alvo.ativo
                else Acao.UTILIZADOR_DESATIVADO
            ),
            resultado=ResultadoAcao.SUCESSO,
            empresa_id=empresa_id,
            utilizador_id=utilizador_atual.id,
            entidade_tipo="Utilizador",
            entidade_id=alvo.id,
            dados_anteriores={"email": alvo.email, "ativo": ativo_anterior},
            dados_novos={"email": alvo.email, "ativo": alvo.ativo},
            request=request,
        )

    db.commit()
    db.refresh(alvo)
    return UtilizadorSchema.model_validate(alvo)


# ---------------------------------------------------------------------------
# Gestão de roles e estados (admin only)
# ---------------------------------------------------------------------------


def alterar_role(
    db: Session,
    utilizador_id: uuid.UUID,
    dados: AlterarRoleSchema,
    empresa_id: uuid.UUID,
    admin: Utilizador,
    request: Request | None = None,
) -> UtilizadorSchema:
    """
    Altera o role de um utilizador (admin only).
    Não é possível alterar o role de outro admin (proteção de conta).
    """
    alvo = _get_utilizador_ou_404(db, utilizador_id, empresa_id)

    # Impede operação sobre si próprio
    if alvo.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Não pode alterar o seu próprio role.",
        )

    # Verifica hierarquia: Admin não gere Admin; SubAdmin não gere Admin/SubAdmin
    _verificar_hierarquia_roles(admin, alvo)

    # SubAdmin não pode promover para Admin ou SubAdmin
    if admin.role == RoleUtilizador.SUBADMIN and dados.novo_role in (
        RoleUtilizador.ADMIN,
        RoleUtilizador.SUBADMIN,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sub-administradores não podem atribuir este perfil.",
        )

    role_anterior = alvo.role
    alvo.role = dados.novo_role
    alvo.updated_at = datetime.now(timezone.utc)
    db.add(alvo)

    registar_acao(
        db,
        acao=Acao.UTILIZADOR_ROLE_ALTERADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=admin.id,
        entidade_tipo="Utilizador",
        entidade_id=alvo.id,
        dados_anteriores={"role": role_anterior.value},
        dados_novos={"role": dados.novo_role.value},
        request=request,
    )

    db.commit()
    db.refresh(alvo)
    return UtilizadorSchema.model_validate(alvo)


def desativar_utilizador(
    db: Session,
    utilizador_id: uuid.UUID,
    empresa_id: uuid.UUID,
    admin: Utilizador,
    request: Request | None = None,
) -> UtilizadorSchema:
    """
    Desativa conta de um utilizador (admin/subadmin).
    O utilizador mantém os dados, apenas perde acesso.
    """
    alvo = _get_utilizador_ou_404(db, utilizador_id, empresa_id)

    if alvo.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Não pode desativar a sua própria conta.",
        )

    # Verifica hierarquia: Admin não gere Admin; SubAdmin não gere Admin/SubAdmin
    _verificar_hierarquia_roles(admin, alvo)

    if not alvo.ativo:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Utilizador já está desativado.",
        )

    alvo.ativo = False
    alvo.updated_at = datetime.now(timezone.utc)
    db.add(alvo)

    # Revogar todos os refresh tokens ativos do utilizador desativado
    tokens_ativos = db.exec(
        select(TokenRefresh).where(
            TokenRefresh.utilizador_id == alvo.id,
            TokenRefresh.revogado_at.is_(None),  # type: ignore[union-attr]
        )
    ).all()
    agora = datetime.now(timezone.utc)
    for token in tokens_ativos:
        token.revogado_at = agora
        db.add(token)

    registar_acao(
        db,
        acao=Acao.UTILIZADOR_DESATIVADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=admin.id,
        entidade_tipo="Utilizador",
        entidade_id=alvo.id,
        dados_anteriores={"email": alvo.email, "ativo": True},
        dados_novos={"email": alvo.email, "ativo": False},
        request=request,
    )

    db.commit()
    db.refresh(alvo)
    return UtilizadorSchema.model_validate(alvo)


def reativar_utilizador(
    db: Session,
    utilizador_id: uuid.UUID,
    empresa_id: uuid.UUID,
    admin: Utilizador,
    request: Request | None = None,
) -> UtilizadorSchema:
    """
    Reativa conta de um utilizador desativado (admin/subadmin).
    """
    alvo = _get_utilizador_ou_404(db, utilizador_id, empresa_id)

    # Verifica hierarquia: Admin não gere Admin; SubAdmin não gere Admin/SubAdmin
    _verificar_hierarquia_roles(admin, alvo)

    if alvo.ativo:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Utilizador já está ativo.",
        )

    if alvo.anonimizado_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Conta anonimizada não pode ser reativada.",
        )

    alvo.ativo = True
    alvo.updated_at = datetime.now(timezone.utc)
    db.add(alvo)

    registar_acao(
        db,
        acao=Acao.UTILIZADOR_REATIVADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=admin.id,
        entidade_tipo="Utilizador",
        entidade_id=alvo.id,
        dados_anteriores={"email": alvo.email, "ativo": False},
        dados_novos={"email": alvo.email, "ativo": True},
        request=request,
    )

    db.commit()
    db.refresh(alvo)
    return UtilizadorSchema.model_validate(alvo)


# ---------------------------------------------------------------------------
# Alteração de password (self)
# ---------------------------------------------------------------------------


def alterar_password(
    db: Session,
    dados: AlterarPasswordSchema,
    utilizador_atual: Utilizador,
    request: Request | None = None,
) -> None:
    """
    Utilizador altera a sua própria password.
    Requer a password atual para confirmação.
    """
    # Verifica password atual
    if not verificar_password(dados.password_atual, utilizador_atual.password_hash):
        registar_acao(
            db,
            acao=Acao.PASSWORD_ALTERADA,
            resultado=ResultadoAcao.FALHA,
            empresa_id=utilizador_atual.empresa_id,
            utilizador_id=utilizador_atual.id,
            request=request,
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password atual incorreta.",
        )

    # Garante que a nova password é diferente da atual
    if verificar_password(dados.nova_password, utilizador_atual.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A nova password não pode ser igual à atual.",
        )

    utilizador_atual.password_hash = hash_password(dados.nova_password)
    utilizador_atual.updated_at = datetime.now(timezone.utc)
    db.add(utilizador_atual)

    registar_acao(
        db,
        acao=Acao.PASSWORD_ALTERADA,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador_atual.empresa_id,
        utilizador_id=utilizador_atual.id,
        request=request,
    )


# ---------------------------------------------------------------------------
# Anonimização RGPD (Art. 17 — direito ao esquecimento)
# ---------------------------------------------------------------------------


def anonimizar_utilizador(
    db: Session,
    utilizador_id: uuid.UUID,
    empresa_id: uuid.UUID,
    admin: Utilizador,
    request: Request | None = None,
) -> dict:
    """
    Anonimiza dados pessoais de um utilizador (RGPD Art. 17).
    Admin e SubAdmin podem anonimizar utilizadores da sua hierarquia.

    O que é anonimizado:
      - email → uuid_anonimizado@anonimizado.invalid
      - nome → "Utilizador Anonimizado"
      - password_hash → string inválida (conta não pode fazer login)
      - totp_secret_cifrado → None
      - consentimento_termos_* → None

    O que é MANTIDO (base legal Art. 17(3)(b) RGPD):
      - id (UUID — necessário para referências de AuditLog)
      - empresa_id, role, created_at, updated_at

    Os AuditLogs são mantidos com o utilizador_id original — o UUID persiste
    como identificador anónimo sem ligar a dados pessoais.
    """
    alvo = _get_utilizador_ou_404(db, utilizador_id, empresa_id)

    if alvo.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Não pode anonimizar a sua própria conta.",
        )

    # Verifica hierarquia: Admin não gere Admin; SubAdmin não gere Admin/SubAdmin
    _verificar_hierarquia_roles(admin, alvo)

    if alvo.anonimizado_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Utilizador já foi anonimizado.",
        )

    agora = datetime.now(timezone.utc)

    # Desativa e anonimiza dados pessoais
    alvo.email = f"{alvo.id}@anonimizado.invalid"
    alvo.nome = "Utilizador Anonimizado"
    alvo.password_hash = "ANONIMIZADO"  # login impossível
    alvo.totp_secret_cifrado = None
    alvo.totp_ativo = False
    alvo.consentimento_termos_at = None
    alvo.consentimento_termos_versao = None
    alvo.ativo = False
    alvo.anonimizado_at = agora
    alvo.deleted_at = agora
    alvo.updated_at = agora

    db.add(alvo)

    registar_acao(
        db,
        acao=Acao.UTILIZADOR_ANONIMIZADO,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=admin.id,
        entidade_tipo="Utilizador",
        entidade_id=alvo.id,
        dados_novos={"anonimizado_at": agora.isoformat()},
        request=request,
    )

    db.commit()

    return {
        "mensagem": "Utilizador anonimizado com sucesso.",
        "utilizador_id": alvo.id,
        "anonimizado_at": agora,
    }


def _gerar_password_temporaria(tamanho: int = 14) -> str:
    """Gera password temporária forte para reset administrativo."""
    minusculas = string.ascii_lowercase
    maiusculas = string.ascii_uppercase
    numeros = string.digits
    especiais = "!@#$%&*_-"
    alfabeto = minusculas + maiusculas + numeros + especiais

    while True:
        pwd = "".join(secrets.choice(alfabeto) for _ in range(tamanho))
        if (
            any(c in maiusculas for c in pwd)
            and any(c in numeros for c in pwd)
            and any(c in especiais for c in pwd)
        ):
            return pwd


def _revogar_sessoes_utilizador(db: Session, utilizador_id: uuid.UUID) -> None:
    """Revoga todas as sessões ativas de um utilizador."""
    sessoes = db.exec(
        select(TokenRefresh).where(
            TokenRefresh.utilizador_id == utilizador_id,
            TokenRefresh.revogado_at.is_(None),  # type: ignore[union-attr]
        )
    ).all()
    agora = datetime.now(timezone.utc)
    for sessao in sessoes:
        sessao.revogado_at = agora
        db.add(sessao)


def resetar_password_admin(
    db: Session,
    utilizador_id: uuid.UUID,
    empresa_id: uuid.UUID,
    admin: Utilizador,
    request: Request | None = None,
) -> dict:
    """
    Admin/SubAdmin define nova password temporária para um utilizador.
    Obriga troca no próximo login e revoga sessões ativas.
    """
    alvo = _get_utilizador_ou_404(db, utilizador_id, empresa_id)

    if alvo.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Use o ecrã de perfil para alterar a sua própria password.",
        )

    # Verifica hierarquia: Admin não gere Admin; SubAdmin não gere Admin/SubAdmin
    _verificar_hierarquia_roles(admin, alvo)

    if not alvo.ativo:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Não é possível resetar password de utilizador desativado.",
        )

    password_temporaria = _gerar_password_temporaria()
    alvo.password_hash = hash_password(password_temporaria)
    alvo.password_temporaria_ativa = True
    alvo.updated_at = datetime.now(timezone.utc)
    db.add(alvo)

    _revogar_sessoes_utilizador(db, alvo.id)

    registar_acao(
        db,
        acao=Acao.PASSWORD_RESET_ADMIN,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=admin.id,
        entidade_tipo="Utilizador",
        entidade_id=alvo.id,
        dados_novos={
            "email": alvo.email,
            "password_temporaria_ativa": True,
        },
        request=request,
    )

    db.commit()

    return {
        "mensagem": "Password temporária gerada com sucesso.",
        "password_temporaria": password_temporaria,
    }


def resetar_mfa_admin(
    db: Session,
    utilizador_id: uuid.UUID,
    empresa_id: uuid.UUID,
    admin: Utilizador,
    request: Request | None = None,
) -> dict:
    """
    Admin/SubAdmin faz reset de MFA de um utilizador.
    O utilizador terá de voltar a configurar 2FA na próxima ativação.
    """
    alvo = _get_utilizador_ou_404(db, utilizador_id, empresa_id)

    if alvo.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Use o seu perfil para gerir o seu próprio MFA.",
        )

    # Verifica hierarquia: Admin não gere Admin; SubAdmin não gere Admin/SubAdmin
    _verificar_hierarquia_roles(admin, alvo)

    alvo.totp_ativo = False
    alvo.totp_secret_cifrado = None
    alvo.updated_at = datetime.now(timezone.utc)
    db.add(alvo)

    codigos = db.exec(
        select(CodigoBackup2FA).where(CodigoBackup2FA.utilizador_id == alvo.id)
    ).all()
    for codigo in codigos:
        db.delete(codigo)

    _revogar_sessoes_utilizador(db, alvo.id)

    registar_acao(
        db,
        acao=Acao.MFA_RESET_ADMIN,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=empresa_id,
        utilizador_id=admin.id,
        entidade_tipo="Utilizador",
        entidade_id=alvo.id,
        dados_novos={
            "email": alvo.email,
            "totp_ativo": False,
        },
        request=request,
    )

    db.commit()

    return {
        "mensagem": "MFA do utilizador resetado com sucesso.",
    }
