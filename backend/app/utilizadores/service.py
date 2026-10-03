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
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.auth.models import (
    CodigoBackup2FA,
    RoleUtilizador,
    Utilizador,
    registar_adesao,
)
from app.auth.service import (
    hash_password,
    terminar_sessoes,
    verify_password as verificar_password,
)
from app.controlos.models import RelatorioAuditoria
from app.formacao.models import ParticipanteFormacao
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.pii import cifrar_pii, decifrar_pii
from app.shared.politica_seguranca import exigir_password_valida, password_min
from app.shared.utils import validar_forca_password
from app.utilizadores.schemas import (
    AlterarPasswordSchema,
    AlterarRoleSchema,
    AtualizarPerfilSchema,
    CriarUtilizadorSchema,
    ImplementadorSchema,
    ListaUtilizadoresSchema,
    MembroEquipaSchema,
    UtilizadorSchema,
)


# Nome que substitui o real numa conta anonimizada. É dado, não texto de
# interface: o ecrã pode traduzi-lo a partir de `anonimizado_at`.
NOME_ANONIMIZADO = "Utilizador Anonimizado"


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
    request: Request | None = None,
) -> None:
    """
    Verifica se o utilizador_atual pode gerir o utilizador alvo.
    Regras:
      - Admin e SubAdmin podem gerir outros utilizadores (com restrições de hierarquia).
      - Qualquer utilizador pode ver/editar o próprio perfil.
      - Nenhum utilizador pode gerir utilizadores de outro tenant.
    """
    if utilizador_atual.empresa_id != alvo.empresa_id:
        _recusar_gestao(utilizador_atual, "Sem permissão.", request)
    roles_gestores = (RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN)
    if utilizador_atual.role not in roles_gestores and utilizador_atual.id != alvo.id:
        _recusar_gestao(utilizador_atual, "Apenas admins podem gerir outros utilizadores.", request)


def _recusar_gestao(
    atual: Utilizador,
    detalhe: str,
    request: Request | None,
    codigo: str = "sem_permissao",
) -> None:
    """Deixa o rasto da recusa na trilha e levanta o 403.

    Toda a recusa da gestão de contas passa por aqui: uma varredura por conta que
    tenta tomar contas acima dela é o sinal que se quer poder ver, e uma linha
    verde não o mostra.
    """
    from app.shared.audit import registar_negacao

    registar_negacao(atual, modulo="utilizadores", acao="operar", codigo=codigo, request=request)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detalhe)


def _exigir_pode_gerir_papel(
    atual: Utilizador,
    papel_alvo: RoleUtilizador,
    request: Request | None = None,
) -> None:
    """O ator só cria, promove, repõe ou desativa contas cujo papel esteja contido
    no seu — senão a gestão de contas era um caminho de escalada.

    O administrador é a exceção: gere qualquer papel, incluindo o do auditor, cuja
    validação independente (`aprovar`) por desenho nem ele detém.
    """
    if atual.role == RoleUtilizador.ADMIN:
        return
    from app.shared.capacidades import papel_contido_em

    if not papel_contido_em(atual.empresa_id, papel_alvo, atual.role):
        _recusar_gestao(atual, "Sem permissão para gerir uma conta com este perfil.", request)


def _verificar_hierarquia_roles(
    atual: Utilizador,
    alvo: Utilizador,
    request: Request | None = None,
) -> None:
    """
    Verifica se o utilizador atual pode agir sobre a conta `alvo` (alterar role,
    desativar, repor, anonimizar).

    Duas camadas:
      - a hierarquia de gestão: ninguém gere um Admin exceto o próprio Admin, e um
        SubAdmin não gere outro SubAdmin (pares não se gerem entre si);
      - a contenção de capacidades: o papel do alvo tem de caber no do ator, para a
        gestão de contas não ser um caminho de escalada (o auditor e um CEO com
        governação reservada deixam de ser alcançáveis por quem não os contém).
    """
    roles_protegidos: list[RoleUtilizador] = [RoleUtilizador.ADMIN]
    if atual.role == RoleUtilizador.SUBADMIN:
        roles_protegidos.append(RoleUtilizador.SUBADMIN)
    if alvo.role in roles_protegidos:
        _recusar_gestao(atual, "Sem permissão para gerir este utilizador.", request)
    _exigir_pode_gerir_papel(atual, alvo.role, request)


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


def listar_equipa(
    db: Session,
    empresa_id: uuid.UUID,
) -> list[MembroEquipaSchema]:
    """
    Pessoas ativas da empresa, de todos os papéis, para os seletores dos módulos.

    Distinta de `listar_implementadores`: aquela serve a delegação de controlos e
    por isso só devolve implementadores. Módulos como a Formação precisam de poder
    escolher qualquer pessoa — incluindo o órgão de gestão, que é justamente quem
    a formação obrigatória visa.
    """
    membros = db.exec(
        select(Utilizador).where(
            Utilizador.empresa_id == empresa_id,
            Utilizador.ativo.is_(True),  # type: ignore[attr-defined]
            Utilizador.deleted_at.is_(None),  # type: ignore[attr-defined]
        )
    ).all()
    schemas = [MembroEquipaSchema.model_validate(u) for u in membros]
    return sorted(schemas, key=lambda m: m.nome.casefold())


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


class EmailJaRegistado(HTTPException):
    """O email já pertence a alguém.

    Existe como classe própria para os DOIS caminhos que chegam aqui darem
    exatamente a mesma resposta: a consulta prévia (o caso sequencial) e a
    restrição única da base (o caso em que dois pedidos chegam ao mesmo tempo).
    Se as respostas divergissem, o cliente conseguiria distinguir «este email já
    existia» de «este email foi criado no mesmo instante» — e isso é um oráculo
    sobre atividade alheia que não tem razão nenhuma para existir.
    """

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este email já está registado.",
        )


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
    - SubAdmin não pode criar admin nem subadmin (pares/superiores).
    - Ninguém cria uma conta cujo papel faça mais do que o próprio (contenção).
    """
    # SubAdmin não pode criar Admin ou SubAdmin (hierarquia de gestão).
    if criador.role == RoleUtilizador.SUBADMIN and dados.role in (
        RoleUtilizador.ADMIN,
        RoleUtilizador.SUBADMIN,
    ):
        _recusar_gestao(
            criador,
            "Sub-administradores não podem criar utilizadores com este perfil.",
            request,
        )
    # E não cria um papel cujas capacidades não estejam contidas nas suas (um
    # subadministrador não abre uma conta de auditor, nem de um CEO a quem a empresa
    # reservou uma decisão de gestão que ele não tem).
    _exigir_pode_gerir_papel(criador, dados.role, request)

    # Verifica email único (global — emails são únicos na plataforma).
    #
    # Esta consulta é conforto, não garantia: entre ela e a escrita cabe outro
    # pedido. Quem garante a unicidade é a restrição da base de dados, e é por
    # isso que a falha dela é tratada logo a seguir em vez de subir como erro.
    existente = db.exec(
        select(Utilizador).where(Utilizador.email == dados.email)
    ).first()
    if existente:
        raise EmailJaRegistado()

    exigir_password_valida(dados.password, db=db, empresa_id=empresa_id)

    novo = Utilizador(
        empresa_id=empresa_id,
        email=dados.email,
        nome=cifrar_pii(dados.nome),
        password_hash=hash_password(dados.password),
        role=dados.role,
        ativo=True,
    )
    db.add(novo)
    try:
        db.flush()
    except IntegrityError:
        # Dois pedidos com o mesmo email ao mesmo tempo: ambos passaram a
        # consulta acima e só um sobrevive à restrição única. Perder essa corrida
        # é um desfecho PREVISTO, não uma avaria — quem perde tem de receber a
        # mesma recusa que receberia em sequência. Sem isto, a resposta era 500:
        # dizia ao cliente que a culpa foi do servidor e deixava um traceback nos
        # registos por uma situação perfeitamente normal.
        #
        # O rollback é obrigatório: depois de uma IntegrityError a sessão fica
        # inutilizável, e qualquer operação seguinte falharia por arrasto.
        db.rollback()
        raise EmailJaRegistado()

    # Adesão à empresa. É o caminho mais usado dos três que criam
    # utilizadores — sem ele a tabela ficaria a conhecer só quem existia antes
    # da migração.
    registar_adesao(db, novo, criado_por_id=criador.id)

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
    _verificar_permissao_gestao(utilizador_atual, alvo, request)

    # Gerir a conta de OUTRA pessoa (o nome, o papel ou o estado) exige a
    # hierarquia e a contenção — senão um subadministrador editava o nome do
    # administrador, que a permissão de gestão acima, por si só, deixava passar.
    # Editar o próprio perfil não passa por aqui.
    if alvo.id != utilizador_atual.id:
        _verificar_hierarquia_roles(utilizador_atual, alvo, request)

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
            _recusar_gestao(utilizador_atual, "Apenas administradores podem alterar o papel.", request)
        if dados.role == RoleUtilizador.ADMIN:
            _recusar_gestao(
                utilizador_atual,
                "Não é possível promover um utilizador a admin por este endpoint.",
                request,
            )
        # SubAdmin não pode promover para SubAdmin
        if utilizador_atual.role == RoleUtilizador.SUBADMIN and dados.role == RoleUtilizador.SUBADMIN:
            _recusar_gestao(utilizador_atual, "Sub-administradores não podem atribuir este perfil.", request)
        # O papel do alvo já foi conferido acima (é outra pessoa); falta o papel
        # NOVO caber no do ator, senão promovia alguém para além de si.
        _exigir_pode_gerir_papel(utilizador_atual, dados.role, request)
        if alvo.id == utilizador_atual.id:
            _recusar_gestao(utilizador_atual, "Não pode alterar o seu próprio role.", request)
        role_anterior = alvo.role
        alvo.role = dados.role
        houve_alteracao_role = True

    if dados.ativo is not None and dados.ativo != alvo.ativo:
        if utilizador_atual.role not in (RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN):
            _recusar_gestao(utilizador_atual, "Apenas administradores podem alterar o estado da conta.", request)
        if alvo.id == utilizador_atual.id and not dados.ativo:
            _recusar_gestao(utilizador_atual, "Não pode desativar a sua própria conta.", request)
        # A hierarquia e a contenção sobre o alvo já foram conferidas no topo.
        ativo_anterior = alvo.ativo
        alvo.ativo = dados.ativo
        houve_alteracao_estado = True
        if not dados.ativo:
            # Como em `desativar_utilizador`: sem isto, ao reativar a conta as
            # sessões antigas (também as de quem as tivesse roubado) voltavam a
            # renovar-se.
            terminar_sessoes(db, alvo.id)

    if not (
        houve_alteracao_nome
        or houve_alteracao_role
        or houve_alteracao_estado
    ):
        return UtilizadorSchema.model_validate(alvo)

    alvo.updated_at = datetime.now(timezone.utc)
    db.add(alvo)

    if houve_alteracao_nome:
        # Isto é uma alteração ao utilizador, não à empresa. As linhas gravadas
        # antes desta correção mantêm o código antigo — a trilha não se
        # reescreve — e são reconhecidas na leitura pela entidade afetada.
        registar_acao(
            db,
            acao=Acao.UTILIZADOR_NOME_ALTERADO,
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
        _recusar_gestao(admin, "Não pode alterar o seu próprio role.", request)

    # Verifica hierarquia e contenção sobre o papel ATUAL do alvo
    _verificar_hierarquia_roles(admin, alvo, request)

    # SubAdmin não pode promover para Admin ou SubAdmin (hierarquia de gestão)
    if admin.role == RoleUtilizador.SUBADMIN and dados.novo_role in (
        RoleUtilizador.ADMIN,
        RoleUtilizador.SUBADMIN,
    ):
        _recusar_gestao(admin, "Sub-administradores não podem atribuir este perfil.", request)

    # E o papel NOVO também tem de estar contido no do ator (não se promove
    # ninguém para além de si).
    _exigir_pode_gerir_papel(admin, dados.novo_role, request)

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
        _recusar_gestao(admin, "Não pode desativar a sua própria conta.", request)

    # Verifica hierarquia: Admin não gere Admin; SubAdmin não gere Admin/SubAdmin
    _verificar_hierarquia_roles(admin, alvo, request)

    if not alvo.ativo:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Utilizador já está desativado.",
        )

    alvo.ativo = False
    alvo.updated_at = datetime.now(timezone.utc)
    db.add(alvo)

    # Terminar as sessões e os pedidos de recuperação do utilizador desativado.
    terminar_sessoes(db, alvo.id)

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
    _verificar_hierarquia_roles(admin, alvo, request)

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
) -> int:
    """
    Utilizador altera a sua própria password.
    Requer a password atual para confirmação.

    Termina todas as sessões da conta — quem a roubou não continua a renovar a
    sessão depois de o dono mudar a password. Quem mudou recebe uma sessão nova
    (no router). Devolve quantas sessões terminou.
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

    exigir_password_valida(dados.nova_password, db=db, empresa_id=utilizador_atual.empresa_id)

    # Garante que a nova password é diferente da atual
    if verificar_password(dados.nova_password, utilizador_atual.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A nova password não pode ser igual à atual.",
        )

    utilizador_atual.password_hash = hash_password(dados.nova_password)
    # Se havia uma password temporária (um reset do administrador), a pessoa
    # acabou de escolher a sua.
    utilizador_atual.password_temporaria_ativa = False
    utilizador_atual.updated_at = datetime.now(timezone.utc)
    db.add(utilizador_atual)
    terminadas = terminar_sessoes(db, utilizador_atual.id)

    registar_acao(
        db,
        acao=Acao.PASSWORD_ALTERADA,
        resultado=ResultadoAcao.SUCESSO,
        empresa_id=utilizador_atual.empresa_id,
        utilizador_id=utilizador_atual.id,
        dados_novos={"sessoes_terminadas": terminadas},
        request=request,
    )
    return terminadas


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
      - o nome nas cópias do sidecar premium (antes de tudo o resto; se o
        sidecar estiver configurado e não responder, 503 e nada muda)

    O que é MANTIDO (base legal Art. 17(3)(b) RGPD):
      - id (UUID — necessário para referências de AuditLog)
      - empresa_id, role, created_at, updated_at

    Os AuditLogs são mantidos com o utilizador_id original — o UUID persiste
    como identificador anónimo sem ligar a dados pessoais.
    """
    alvo = _get_utilizador_ou_404(db, utilizador_id, empresa_id)

    if alvo.id == admin.id:
        _recusar_gestao(admin, "Não pode anonimizar a sua própria conta.", request)

    # Verifica hierarquia: Admin não gere Admin; SubAdmin não gere Admin/SubAdmin
    _verificar_hierarquia_roles(admin, alvo, request)

    if alvo.anonimizado_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Utilizador já foi anonimizado.",
        )

    # O sidecar premium guarda cópias do nome (responsáveis, donos, avaliadores,
    # quem decidiu). Vai primeiro: se não responder, nada muda aqui — senão a
    # conta ficava anonimizada e o nome continuava nessas linhas, sem ninguém
    # saber. Repetir é seguro (a troca é idempotente).
    from app.premium.anonimizacao_client import SidecarIndisponivel, anonimizar_pessoa
    from app.shared.i18n import MsgsI18n, locale_de_request, traduzir

    try:
        anonimizar_pessoa(str(alvo.empresa_id), str(alvo.id), nome_substituto=NOME_ANONIMIZADO)
    except SidecarIndisponivel:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=traduzir(MsgsI18n.ANONIMIZACAO_PREMIUM_INDISPONIVEL, locale_de_request(request)),
        )

    agora = datetime.now(timezone.utc)

    # Desativa e anonimiza dados pessoais.
    #
    # O nome vai CIFRADO, como qualquer outro nome: a coluna é lida por
    # `decifrar_pii` em todos os ecrãs, e um texto em claro não decifra — a
    # listagem da empresa inteira passava a responder 500 depois de anonimizar
    # uma única pessoa.
    alvo.email = f"{alvo.id}@anonimizado.invalid"
    alvo.nome = cifrar_pii(NOME_ANONIMIZADO)
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

    # Cópias desnormalizadas do nome, guardadas noutras tabelas para poupar
    # joins. Anonimizar a conta e deixar o nome nelas seria cumprir o pedido
    # só na tabela em que ninguém o vê.
    for relatorio in db.exec(
        select(RelatorioAuditoria).where(RelatorioAuditoria.auditor_id == alvo.id)
    ).all():
        relatorio.auditor_nome = cifrar_pii(NOME_ANONIMIZADO)
        db.add(relatorio)
    for participante in db.exec(
        select(ParticipanteFormacao).where(ParticipanteFormacao.utilizador_id == alvo.id)
    ).all():
        participante.nome = cifrar_pii(NOME_ANONIMIZADO)
        db.add(participante)

    # Uma conta anonimizada não pode continuar com sessões vivas: o refresh
    # renovaria o acesso de alguém que, para a aplicação, deixou de existir.
    terminar_sessoes(db, alvo.id)
    for codigo in db.exec(
        select(CodigoBackup2FA).where(CodigoBackup2FA.utilizador_id == alvo.id)
    ).all():
        db.delete(codigo)

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


def _gerar_password_temporaria(tamanho: int) -> str:
    """Gera uma password temporária para o reset administrativo.

    Repete até a regra da plataforma a aceitar: é a mesma função que valida as
    passwords escolhidas, por isso a temporária nunca fica aquém delas.
    """
    alfabeto = string.ascii_letters + string.digits + "!@#$%&*_-"
    while True:
        pwd = "".join(secrets.choice(alfabeto) for _ in range(tamanho))
        if validar_forca_password(pwd, minimo=tamanho)[0]:
            return pwd


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
        _recusar_gestao(admin, "Use o ecrã de perfil para alterar a sua própria password.", request)

    # Verifica hierarquia: Admin não gere Admin; SubAdmin não gere Admin/SubAdmin
    _verificar_hierarquia_roles(admin, alvo, request)

    if not alvo.ativo:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Não é possível resetar password de utilizador desativado.",
        )

    # Nunca abaixo do mínimo da empresa, que pode passar dos 16.
    password_temporaria = _gerar_password_temporaria(max(16, password_min(db, empresa_id)))
    alvo.password_hash = hash_password(password_temporaria)
    alvo.password_temporaria_ativa = True
    alvo.updated_at = datetime.now(timezone.utc)
    db.add(alvo)

    terminar_sessoes(db, alvo.id)

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
        _recusar_gestao(admin, "Use o seu perfil para gerir o seu próprio MFA.", request)

    # Verifica hierarquia: Admin não gere Admin; SubAdmin não gere Admin/SubAdmin
    _verificar_hierarquia_roles(admin, alvo, request)

    alvo.totp_ativo = False
    alvo.totp_secret_cifrado = None
    alvo.updated_at = datetime.now(timezone.utc)
    db.add(alvo)

    codigos = db.exec(
        select(CodigoBackup2FA).where(CodigoBackup2FA.utilizador_id == alvo.id)
    ).all()
    for codigo in codigos:
        db.delete(codigo)

    terminar_sessoes(db, alvo.id)

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
