"""
Router do módulo de empresas.
Expõe dados e configuração do tenant autenticado.

Prefixo base: /api (incluído em main.py)
Prefixo do router: /empresas
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.empresas.models import Empresa
from app.empresas.schemas import AtualizarEmpresaSchema, EmpresaSchema
from app.shared.audit import Acao, ResultadoAcao, registar_acao
from app.shared.capacidades import ClasseAcao, require_capability
from app.shared.dependencies import CurrentUserDep, SessionDep
from app.shared import politica_seguranca
from app.shared.pii import cifrar_pii

router = APIRouter(prefix="/empresas", tags=["Empresa"])

AdminDep = Depends(require_capability("empresa", ClasseAcao.GOVERNAR))


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
        # A1 — uma mudança de política de segurança tem de deixar o antes e o
        # depois no registo. Sem o anterior, ficava a saber-se que a política
        # mudou e não de onde veio, que é metade do que uma auditoria pergunta.
        "config_seguranca": empresa.config_seguranca,
        "config_notificacoes": empresa.config_notificacoes,
    }

    # Campos PII que precisam de cifra antes de guardar
    _PII_CAMPOS = {"nome", "nif", "email", "website"}

    enviados = dados.model_dump(exclude_unset=True)

    # A1 — a política do tenant só existe on-prem. Em SaaS a escrita é recusada
    # AQUI, no servidor: esconder o separador no frontend deixaria a restrição
    # decorativa, porque quem chamasse a API a direito continuava a gravá-la.
    _CONFIGS = {"config_seguranca", "config_notificacoes"}
    if _CONFIGS & set(enviados):
        if not politica_seguranca.configuravel():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="A política de segurança é fixa nesta modalidade.",
            )
        # Saneia antes de gravar: chaves desconhecidas fora, tipos normalizados e
        # clamps aplicados. O que fica na base tem de ser o que vai ser aplicado —
        # guardar um valor e aplicar outro é a origem exata deste achado.
        if "config_seguranca" in enviados:
            enviados["config_seguranca"] = politica_seguranca.sanear_config(
                enviados["config_seguranca"],
                chaves_validas=politica_seguranca.CHAVES_SEGURANCA,
            )
        if "config_notificacoes" in enviados:
            enviados["config_notificacoes"] = politica_seguranca.sanear_config(
                enviados["config_notificacoes"],
                chaves_validas=politica_seguranca.CHAVES_NOTIFICACOES,
            )

    # Aplicar apenas campos enviados
    for campo, valor in enviados.items():
        if campo in _PII_CAMPOS and valor is not None:
            setattr(empresa, campo, cifrar_pii(valor))
        else:
            setattr(empresa, campo, valor)

    empresa.updated_at = datetime.now(timezone.utc)

    db.add(empresa)
    db.flush()  # escreve as alterações no DB dentro da transação antes de retornar

    # Os campos PII ficam cifrados na tabela da empresa; repetir o valor em claro
    # aqui criaria uma segunda cópia por fora da cifra, que ninguém decifraria de
    # volta. Regista-se que mudaram — o valor corrente está na tabela.
    dados_novos = {
        campo: valor for campo, valor in enviados.items() if campo not in _PII_CAMPOS
    }
    pii_alterados = sorted(campo for campo in enviados if campo in _PII_CAMPOS)
    if pii_alterados:
        dados_novos["campos_alterados"] = pii_alterados

    registar_acao(
        db=db,
        acao=Acao.EMPRESA_DADOS_ATUALIZADOS,
        utilizador_id=utilizador_atual.id,
        empresa_id=empresa.id,
        entidade_tipo="Empresa",
        entidade_id=empresa.id,
        dados_anteriores=dados_anteriores,
        dados_novos=dados_novos,
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
