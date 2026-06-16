"""
Router do módulo de empresas.
Expõe dados e configuração do tenant autenticado.

Prefixo base: /api (incluído em main.py)
Prefixo do router: /empresas
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth.models import RoleUtilizador
from app.empresas.models import Empresa
from app.empresas.schemas import AtualizarEmpresaSchema, EmpresaSchema
from app.shared.audit import ResultadoAcao, registar_acao
from app.shared.dependencies import CurrentUserDep, SessionDep, require_role
from app.shared.pii import cifrar_pii

router = APIRouter(prefix="/empresas", tags=["Empresa"])

AdminDep = Depends(require_role(RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN))


# ---------------------------------------------------------------------------
# GET /empresas/me — dados da empresa do utilizador autenticado
# ---------------------------------------------------------------------------

@router.get(
    "/me",
    response_model=EmpresaSchema,
    summary="Dados da empresa atual",
)
def get_empresa_atual(
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
):
    """
    Devolve os dados da empresa à qual o utilizador autenticado pertence.
    Qualquer role autenticado pode aceder.
    """
    empresa = db.get(Empresa, utilizador_atual.empresa_id)
    if not empresa or not empresa.ativo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empresa não encontrada.",
        )
    return EmpresaSchema.model_validate(empresa)


# ---------------------------------------------------------------------------
# PATCH /empresas/me — atualizar dados da empresa
# ---------------------------------------------------------------------------

@router.patch(
    "/me",
    response_model=EmpresaSchema,
    summary="Atualizar dados da empresa (admin)",
    dependencies=[AdminDep],
)
def atualizar_empresa(
    dados: AtualizarEmpresaSchema,
    request: Request,
    db: SessionDep,
    utilizador_atual: CurrentUserDep,
):
    """
    Atualiza os dados da empresa.
    Apenas disponível para administradores.
    """
    empresa = db.get(Empresa, utilizador_atual.empresa_id)
    if not empresa or not empresa.ativo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empresa não encontrada.",
        )

    dados_anteriores = {
        "setor": empresa.setor,
        "dimensao": empresa.dimensao,
        "tipo_entidade": empresa.tipo_entidade,
        "nivel_qnrcs": empresa.nivel_qnrcs,
    }

    # Campos PII que precisam de cifra antes de guardar
    _PII_CAMPOS = {"nome", "nif", "email", "website"}

    # Aplicar apenas campos enviados
    for campo, valor in dados.model_dump(exclude_unset=True).items():
        if campo in _PII_CAMPOS and valor is not None:
            setattr(empresa, campo, cifrar_pii(valor))
        else:
            setattr(empresa, campo, valor)

    empresa.updated_at = datetime.now(timezone.utc)

    db.add(empresa)
    db.flush()  # escreve as alterações no DB dentro da transação antes de retornar

    registar_acao(
        db=db,
        acao="empresa.dados_atualizados",
        utilizador_id=utilizador_atual.id,
        empresa_id=empresa.id,
        entidade_tipo="Empresa",
        entidade_id=empresa.id,
        dados_anteriores=dados_anteriores,
        dados_novos=dados.model_dump(exclude_unset=True),
        request=request,
        resultado=ResultadoAcao.SUCESSO,
    )

    # Regenerar plano se o nível de conformidade da empresa mudou.
    # gerar_plano() faz db.commit() internamente — inclui empresa + audit log.
    if "nivel_qnrcs" in dados.model_dump(exclude_unset=True):
        from app.plano_prioritario.service import gerar_plano, plano_existe
        if plano_existe(db, empresa.id):
            gerar_plano(db, empresa)

    return EmpresaSchema.model_validate(empresa)
