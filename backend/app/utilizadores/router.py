"""
Router do módulo de utilizadores.
Gestão de utilizadores dentro do tenant: CRUD, roles, ativação, password, RGPD.

Prefixo base: /api (incluído em main.py)
Prefixo do router: /utilizadores
"""
import uuid

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.auth.models import RoleUtilizador
# O mesmo limitador das rotas de autenticação: as rotas daqui que pagam um argon2
# contam-se como as de lá (por IP, e em IPv6 pelo /64).
from app.auth.router import _limiter as limiter_auth, renovar_sessao_de_quem_pediu
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, SessionDep
from app.utilizadores import schemas, service

router = APIRouter(prefix="/utilizadores", tags=["Utilizadores"])

# ---------------------------------------------------------------------------
# Aliases de dependência de role
# ---------------------------------------------------------------------------

GerirDep = Depends(require_capability("utilizadores", ClasseAcao.OPERAR))

# Seletores de pessoas dos módulos: quem opera dados + o órgão de gestão. O
# auditor fica de fora (segregação de funções — audita, não preenche).
EquipaDep = Depends(require_capability("utilizadores", ClasseAcao.VER))

# A anonimização é irreversível e tem módulo próprio na matriz, para poder ser
# concedida ou retirada sem mexer no resto da gestão de utilizadores.
AnonimizarDep = Depends(require_capability("rgpd", ClasseAcao.OPERAR))


# ---------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=schemas.ListaUtilizadoresSchema,
    summary="Listar utilizadores da empresa",
)
def listar_utilizadores(
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    so_ativos: bool = False,
    q: str | None = Query(None, description="Pesquisa por nome, email ou papel"),
    role: RoleUtilizador | None = Query(None, description="Filtrar por papel"),
    limite: int | None = Query(None, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    Admin vê todos os utilizadores da empresa.
    Outros roles apenas veem o próprio perfil.
    """
    return service.listar_utilizadores(
        db,
        utilizador_atual.empresa_id,
        utilizador_atual,
        so_ativos=so_ativos,
        q=q,
        role=role,
        limite=limite,
        offset=offset,
    )


@router.get(
    "/implementadores",
    response_model=list[schemas.ImplementadorSchema],
    summary="Listar implementadores ativos (para delegação)",
    dependencies=[GerirDep],
)
def listar_implementadores(
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
):
    """
    Candidatos a receber a delegação de um controlo — só o papel implementador,
    que é o único que o `ControloEmpresaV2.implementador_id` aceita.

    NÃO serve para escolher responsáveis nos módulos: esses aceitam qualquer
    pessoa da empresa (incluindo o órgão de gestão) e usam `/equipa`. Usar este
    endpoint num seletor de responsável esconde toda a gente menos um papel.
    """
    return service.listar_implementadores(db, utilizador_atual.empresa_id)


@router.get(
    "/equipa",
    response_model=list[schemas.MembroEquipaSchema],
    summary="Listar pessoas ativas da empresa (para seletores dos módulos)",
    dependencies=[EquipaDep],
)
def listar_equipa(
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
):
    """
    Pessoas ativas da empresa, de todos os papéis, com o mínimo para um seletor
    (id, nome, papel).

    Existe porque `/implementadores` só devolve um papel e há módulos que precisam
    de escolher qualquer pessoa — a Formação tem de poder registar o órgão de
    gestão como responsável e como participante.

    Fica fora do alcance do auditor por segregação de funções: audita os módulos,
    não preenche os seus campos.
    """
    return service.listar_equipa(db, utilizador_atual.empresa_id)


# ---------------------------------------------------------------------------
# Perfil próprio
# ---------------------------------------------------------------------------


@router.get(
    "/me",
    response_model=schemas.UtilizadorSchema,
    summary="Ver o meu perfil",
)
def get_meu_perfil(utilizador_atual: CurrentUserDep):
    """Devolve o perfil do utilizador autenticado."""
    return schemas.UtilizadorSchema.model_validate(utilizador_atual)


@router.post(
    "/me/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Alterar a minha password",
)
@limiter_auth.limit("5/minute")
def alterar_minha_password(
    dados: schemas.AlterarPasswordSchema,
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
    response: Response,
):
    """
    Utilizador altera a sua própria password.
    Requer confirmação da password atual.

    Limitada por IP como o login: cada pedido paga uma ou duas verificações
    argon2 (64 MiB cada), e sem limite próprio uma conta qualquer ocupava as
    vagas de argon2 de toda a instalação. As outras sessões da conta terminam;
    quem mudou recebe um cookie de sessão novo.
    """
    service.alterar_password(db, dados, utilizador_atual, request=request)
    renovar_sessao_de_quem_pediu(db, response, request, utilizador_atual)


# ---------------------------------------------------------------------------
# Gestão individual (admin + self)
# ---------------------------------------------------------------------------


@router.get(
    "/{utilizador_id}",
    response_model=schemas.UtilizadorSchema,
    summary="Ver utilizador por ID",
)
def get_utilizador(
    utilizador_id: uuid.UUID,
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
):
    """Admin pode ver qualquer utilizador da empresa. Outros só o próprio."""
    return service.get_utilizador(
        db, utilizador_id, utilizador_atual.empresa_id, utilizador_atual
    )


@router.post(
    "",
    response_model=schemas.UtilizadorSchema,
    status_code=status.HTTP_201_CREATED,
    summary="Criar utilizador (admin/subadmin)",
    dependencies=[GerirDep],
)
@limiter_auth.limit("10/minute")
def criar_utilizador(
    dados: schemas.CriarUtilizadorSchema,
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
):
    """
    Admin ou SubAdmin cria um novo utilizador na empresa.
    Não é possível criar utilizadores com role admin por este endpoint.
    SubAdmin não pode criar utilizadores com role admin ou subadmin.
    """
    return service.criar_utilizador(
        db, dados, utilizador_atual.empresa_id, utilizador_atual, request=request
    )


@router.patch(
    "/{utilizador_id}",
    response_model=schemas.UtilizadorSchema,
    summary="Atualizar perfil",
)
def atualizar_perfil(
    utilizador_id: uuid.UUID,
    dados: schemas.AtualizarPerfilSchema,
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
):
    """
    Atualiza dados de perfil (nome).
    Admin pode atualizar qualquer utilizador. Outros só o próprio.
    """
    return service.atualizar_perfil(
        db, utilizador_id, dados, utilizador_atual.empresa_id, utilizador_atual, request=request
    )


@router.patch(
    "/{utilizador_id}/role",
    response_model=schemas.UtilizadorSchema,
    summary="Alterar role (admin/subadmin)",
    dependencies=[GerirDep],
)
def alterar_role(
    utilizador_id: uuid.UUID,
    dados: schemas.AlterarRoleSchema,
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
):
    """
    Admin/SubAdmin altera o role de um utilizador.
    Não é possível alterar o role de um admin nem o próprio role.
    """
    return service.alterar_role(
        db, utilizador_id, dados, utilizador_atual.empresa_id, utilizador_atual, request=request
    )


@router.post(
    "/{utilizador_id}/desativar",
    response_model=schemas.UtilizadorSchema,
    summary="Desativar utilizador (admin/subadmin)",
    dependencies=[GerirDep],
)
def desativar_utilizador(
    utilizador_id: uuid.UUID,
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
):
    """Desativa a conta de um utilizador. O utilizador perde imediatamente o acesso."""
    return service.desativar_utilizador(
        db, utilizador_id, utilizador_atual.empresa_id, utilizador_atual, request=request
    )


@router.post(
    "/{utilizador_id}/reativar",
    response_model=schemas.UtilizadorSchema,
    summary="Reativar utilizador (admin/subadmin)",
    dependencies=[GerirDep],
)
def reativar_utilizador(
    utilizador_id: uuid.UUID,
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
):
    """Reativa a conta de um utilizador previamente desativado."""
    return service.reativar_utilizador(
        db, utilizador_id, utilizador_atual.empresa_id, utilizador_atual, request=request
    )


@router.post(
    "/{utilizador_id}/reset-password",
    response_model=schemas.ResultadoResetPasswordAdminSchema,
    summary="Resetar password (admin/subadmin)",
    dependencies=[GerirDep],
)
@limiter_auth.limit("10/minute")
def resetar_password_admin(
    utilizador_id: uuid.UUID,
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
):
    """Gera password temporária para um utilizador e força troca no próximo login."""
    return service.resetar_password_admin(
        db, utilizador_id, utilizador_atual.empresa_id, utilizador_atual, request=request
    )


@router.post(
    "/{utilizador_id}/reset-mfa",
    response_model=schemas.ResultadoResetMFAAdminSchema,
    summary="Resetar MFA (admin/subadmin)",
    dependencies=[GerirDep],
)
def resetar_mfa_admin(
    utilizador_id: uuid.UUID,
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
):
    """Desativa e limpa configuração MFA do utilizador alvo."""
    return service.resetar_mfa_admin(
        db, utilizador_id, utilizador_atual.empresa_id, utilizador_atual, request=request
    )


# ---------------------------------------------------------------------------
# RGPD — direito ao esquecimento
# ---------------------------------------------------------------------------


@router.post(
    "/{utilizador_id}/anonimizar",
    response_model=schemas.ResultadoAnonimizacaoSchema,
    summary="Anonimizar utilizador (RGPD Art. 17)",
    dependencies=[AnonimizarDep],
)
def anonimizar_utilizador(
    utilizador_id: uuid.UUID,
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
    request: Request,
):
    """
    Anonimiza dados pessoais do utilizador (RGPD — direito ao esquecimento).
    Os AuditLogs são mantidos com o UUID anonimizado (base legal Art. 17(3)(b) RGPD).
    Esta operação é irreversível.
    """
    return service.anonimizar_utilizador(
        db, utilizador_id, utilizador_atual.empresa_id, utilizador_atual, request=request
    )
