"""
Router de auditoria — NIS2PME.
Expõe o histórico de ações da empresa ao admin/auditor.

Prefixo base: /api (incluído em main.py)
Prefixo do router: /audit-logs
"""
import uuid
from datetime import datetime, timezone
import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, or_
from sqlmodel import select

from app.auth.models import RoleUtilizador, Utilizador
from app.shared.audit import Acao, AuditLog
from app.shared.dependencies import (
    SessionDep,
    get_current_user,
    require_role,
)
from app.shared.pii import decifrar_pii

router = APIRouter(prefix="/audit-logs", tags=["Auditoria"])

AdminOuAuditorDep = Depends(
    require_role(RoleUtilizador.ADMIN, RoleUtilizador.SUBADMIN, RoleUtilizador.AUDITOR)
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AuditLogSchema(BaseModel):
    created_at: datetime
    utilizador_nome: str | None = None
    acao: str
    dados_anteriores: str | None = None
    dados_novos: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    resultado: str

    model_config = {"from_attributes": True}


class AuditLogResumoSchema(BaseModel):
    id: uuid.UUID
    created_at: datetime
    utilizador_nome: str | None = None
    acao: str
    alvo_resumo: str
    mudanca_resumo: str
    ip_address: str | None = None
    resultado: str


class ListaAuditLogsSchema(BaseModel):
    total: int
    total_falhas: int
    total_auth_falha: int
    total_logins_sucesso: int
    total_ips_distintos: int
    logs: list[AuditLogResumoSchema]


OFFSET_COM_ESPACO_RE = re.compile(
    r"^(?P<prefixo>.+T.+)\s(?P<offset>\d{2}:\d{2})$"
)


def _parse_json_seguro(valor: str | None) -> Any:
    if not valor:
        return None
    try:
        return json.loads(valor)
    except (TypeError, ValueError):
        return None


def _parse_datetime_query(
    valor: str | None,
    campo: str,
) -> datetime | None:
    if valor is None:
        return None

    normalizado = valor.strip()
    if not normalizado:
        return None

    if normalizado.endswith("Z"):
        normalizado = f"{normalizado[:-1]}+00:00"

    match = OFFSET_COM_ESPACO_RE.match(normalizado)
    if match:
        normalizado = (
            f"{match.group('prefixo')}+{match.group('offset')}"
        )

    try:
        parsed = datetime.fromisoformat(normalizado)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Formato inválido para {campo}. Use ISO 8601, por "
                "exemplo 2026-04-10T05:55:07+00:00."
            ),
        ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def _chave_tecnica_id(chave: str) -> bool:
    return chave == "id" or chave.endswith("_id")


def _is_uuid_like(valor: Any) -> bool:
    if not isinstance(valor, str):
        return False
    try:
        uuid.UUID(valor)
    except ValueError:
        return False
    return True


def _limpar_objeto_para_ui(valor: Any) -> Any:
    if isinstance(valor, list):
        return [_limpar_objeto_para_ui(item) for item in valor]
    if isinstance(valor, dict):
        return {
            chave: _limpar_objeto_para_ui(item)
            for chave, item in valor.items()
            if not _chave_tecnica_id(chave)
        }
    if _is_uuid_like(valor):
        return "—"
    return valor


def _valor_display(valor: Any) -> str:
    if valor in (None, ""):
        return "—"
    if isinstance(valor, bool):
        return "sim" if valor else "não"
    if isinstance(valor, (dict, list)):
        return json.dumps(_limpar_objeto_para_ui(valor), ensure_ascii=False)
    if _is_uuid_like(valor):
        return "—"
    return str(valor)


# Campos PII que devem ser mascarados na UI quando o utilizador foi anonimizado
_CAMPOS_PII_UTILIZADOR = {
    "email", "nome", "password_hash", "totp_secret_cifrado",
    "implementador_email", "implementador_nome",
}


def _sanitizar_pii_em_dados(dados_json: str | None) -> str | None:
    """Substitui campos PII por '[anonimizado]' na representação de display.

    Não altera a BD — aplica-se apenas na camada de apresentação,
    conforme RGPD Art. 17(3)(b): o registo da ação é retido mas os dados
    pessoais identificativos são mascarados.
    """
    if not dados_json:
        return dados_json
    try:
        dados = json.loads(dados_json)
        if isinstance(dados, dict):
            for campo in _CAMPOS_PII_UTILIZADOR:
                if campo in dados:
                    dados[campo] = "[anonimizado]"
        return json.dumps(dados, ensure_ascii=False)
    except (TypeError, ValueError):
        return dados_json


def _log_referencia_anonimizado(
    log: AuditLog,
    anonimizados_ids: set[uuid.UUID],
) -> bool:
    """Verifica se o log referencia um utilizador anonimizado.

    Verifica três vetores:
    1. O actor (utilizador_id) foi anonimizado.
    2. A entidade do log é um Utilizador anonimizado.
    3. Algum campo *_id nos JSON do log aponta para um utilizador anonimizado
       (ex: implementador_id em logs de delegação).
    """
    if not anonimizados_ids:
        return False
    if log.utilizador_id and log.utilizador_id in anonimizados_ids:
        return True
    if (
        log.entidade_tipo == "Utilizador"
        and log.entidade_id is not None
        and log.entidade_id in anonimizados_ids
    ):
        return True
    for dados_json in (log.dados_anteriores, log.dados_novos):
        dados = _parse_json_seguro(dados_json)
        if isinstance(dados, dict):
            for chave, valor in dados.items():
                if chave.endswith("_id") and _is_uuid_like(valor):
                    try:
                        if uuid.UUID(valor) in anonimizados_ids:
                            return True
                    except ValueError:
                        pass
    return False


def _get_resumo_alvo(log: AuditLog, dados_ant: str | None = None, dados_nov: str | None = None) -> str:
    novos = _parse_json_seguro(dados_nov if dados_nov is not None else log.dados_novos) or {}
    anteriores = _parse_json_seguro(dados_ant if dados_ant is not None else log.dados_anteriores) or {}

    if log.acao == Acao.UTILIZADOR_DELEGACAO_ATRIBUIDA:
        controlo = novos.get("controlo_codigo") or anteriores.get(
            "controlo_codigo"
        ) or "—"
        implementador = (
            novos.get("implementador_nome")
            or novos.get("implementador_email")
            or "—"
        )
        return f"{controlo} → {implementador}"

    if log.acao.startswith("evidencia."):
        return (
            novos.get("titulo")
            or anteriores.get("titulo")
            or novos.get("ficheiro_nome")
            or anteriores.get("ficheiro_nome")
            or "—"
        )

    nome_ou_titulo = (
        novos.get("nome")
        or anteriores.get("nome")
        or novos.get("titulo")
        or anteriores.get("titulo")
        or novos.get("controlo_codigo")
        or anteriores.get("controlo_codigo")
        or novos.get("ficheiro_nome")
        or anteriores.get("ficheiro_nome")
    )
    if nome_ou_titulo:
        return nome_ou_titulo
    # email só como fallback final; se foi mascarado, não é informativo — omite
    email = novos.get("email") or anteriores.get("email")
    if email and email != "[anonimizado]":
        return email
    return "—"


def _get_resumo_mudanca(log: AuditLog, dados_ant: str | None = None, dados_nov: str | None = None) -> str:
    anteriores = _limpar_objeto_para_ui(
        _parse_json_seguro(dados_ant if dados_ant is not None else log.dados_anteriores) or {}
    )
    novos = _limpar_objeto_para_ui(_parse_json_seguro(dados_nov if dados_nov is not None else log.dados_novos) or {})
    chaves = list(dict.fromkeys([*anteriores.keys(), *novos.keys()]))
    alteradas = [
        chave
        for chave in chaves
        if json.dumps(anteriores.get(chave), sort_keys=True)
        != json.dumps(novos.get(chave), sort_keys=True)
    ]

    if alteradas:
        resumo = [
            f"{chave}: {_valor_display(anteriores.get(chave))} → {_valor_display(novos.get(chave))}"
            for chave in alteradas[:2]
        ]
        if len(alteradas) > 2:
            resumo.append(f"+{len(alteradas) - 2}")
        return " • ".join(resumo)

    if novos:
        chave = next(iter(novos))
        return f"{chave}: {_valor_display(novos.get(chave))}"

    return "—"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=ListaAuditLogsSchema,
    summary="Listar registos de auditoria da empresa (admin/auditor)",
    dependencies=[AdminOuAuditorDep],
)
def listar_audit_logs(
    db: SessionDep,
    utilizador_atual: Utilizador = Depends(get_current_user),
    q: str | None = Query(None, description="Pesquisa textual simples"),
    data_inicio_raw: str | None = Query(
        None,
        alias="data_inicio",
        description="Filtrar registos criados a partir desta data/hora",
    ),
    data_fim_raw: str | None = Query(
        None,
        alias="data_fim",
        description="Filtrar registos criados até esta data/hora",
    ),
    acao: str | None = Query(
        None, description="Filtrar por fragmento de ação, ex: 'login'"
    ),
    resultado: str | None = Query(None, description="'sucesso' ou 'falha'"),
    entidade_tipo: str | None = Query(
        None, description="Filtrar por entidade, ex: 'ControloEmpresa'"
    ),
    entidade_id: uuid.UUID | None = Query(
        None, description="Filtrar por ID exato da entidade"
    ),
    utilizador_id: uuid.UUID | None = Query(
        None, description="Filtrar por utilizador que executou a ação"
    ),
    limite: int = Query(100, le=500, ge=1),
    offset: int = Query(0, ge=0),
):
    """
    Devolve os registos de auditoria da empresa autenticada.
    Os logs são imutáveis por design — retenção mínima 12 meses
    (NIS2/DL 125/2025).
    """
    empresa_id = utilizador_atual.empresa_id
    data_inicio = _parse_datetime_query(data_inicio_raw, "data_inicio")
    data_fim = _parse_datetime_query(data_fim_raw, "data_fim")

    if (
        data_inicio is not None
        and data_fim is not None
        and data_fim < data_inicio
    ):
        raise HTTPException(
            status_code=400,
            detail="A data final não pode ser anterior à data inicial.",
        )

    stmt = select(AuditLog).where(
        AuditLog.empresa_id == empresa_id,
        AuditLog.acao != Acao.REFRESH_TOKEN,
    )
    count_stmt = select(func.count()).select_from(AuditLog).where(
        AuditLog.empresa_id == empresa_id,
        AuditLog.acao != Acao.REFRESH_TOKEN,
    )
    # Stats globais — calculados sem paginação e sem os filtros de pesquisa,
    # para refletir sempre o estado real do audit log da empresa.
    count_falhas_stmt = select(func.count()).select_from(AuditLog).where(
        AuditLog.empresa_id == empresa_id,
        AuditLog.acao != Acao.REFRESH_TOKEN,
        AuditLog.resultado == "falha",
    )
    # Conta falhas de autenticação (password + MFA) — usado para o aviso de segurança.
    # Inclui LOGIN_FALHA e FA2_FALHOU; exclui erros de LLM, cron, etc.
    count_auth_falha_stmt = select(func.count()).select_from(AuditLog).where(
        AuditLog.empresa_id == empresa_id,
        AuditLog.acao.in_([Acao.LOGIN_FALHA, Acao.FA2_FALHOU]),  # type: ignore[union-attr]
    )
    count_logins_stmt = select(func.count()).select_from(AuditLog).where(
        AuditLog.empresa_id == empresa_id,
        AuditLog.acao == Acao.LOGIN_SUCESSO,
    )
    # IPs cifrados com Fernet (não determinístico) — COUNT(DISTINCT) no SQL
    # não funciona; desciframos em Python e contamos IPs únicos em plain text.
    all_ips_stmt = select(AuditLog.ip_address).where(
        AuditLog.empresa_id == empresa_id,
        AuditLog.acao != Acao.REFRESH_TOKEN,
        AuditLog.ip_address.isnot(None),  # type: ignore[union-attr]
    )

    if acao:
        stmt = stmt.where(AuditLog.acao.contains(acao))  # type: ignore[union-attr]
        count_stmt = count_stmt.where(AuditLog.acao.contains(acao))  # type: ignore[union-attr]
    if q:
        termo = f"%{q.strip()}%"
        criterio_q = or_(
            AuditLog.acao.ilike(termo),
            AuditLog.entidade_tipo.ilike(termo),
            AuditLog.dados_anteriores.ilike(termo),
            AuditLog.dados_novos.ilike(termo),
        )
        stmt = stmt.where(criterio_q)
        count_stmt = count_stmt.where(criterio_q)
    if data_inicio is not None:
        stmt = stmt.where(AuditLog.created_at >= data_inicio)
        count_stmt = count_stmt.where(AuditLog.created_at >= data_inicio)
    if data_fim is not None:
        stmt = stmt.where(AuditLog.created_at <= data_fim)
        count_stmt = count_stmt.where(AuditLog.created_at <= data_fim)
    if resultado:
        stmt = stmt.where(AuditLog.resultado == resultado)
        count_stmt = count_stmt.where(AuditLog.resultado == resultado)
    if entidade_tipo:
        stmt = stmt.where(AuditLog.entidade_tipo == entidade_tipo)
        count_stmt = count_stmt.where(AuditLog.entidade_tipo == entidade_tipo)
    if entidade_id:
        stmt = stmt.where(AuditLog.entidade_id == entidade_id)
        count_stmt = count_stmt.where(AuditLog.entidade_id == entidade_id)
    if utilizador_id:
        stmt = stmt.where(AuditLog.utilizador_id == utilizador_id)
        count_stmt = count_stmt.where(AuditLog.utilizador_id == utilizador_id)

    total: int = db.exec(count_stmt).one()  # type: ignore[assignment]
    total_falhas: int = db.exec(count_falhas_stmt).one()  # type: ignore[assignment]
    total_auth_falha: int = db.exec(count_auth_falha_stmt).one()  # type: ignore[assignment]
    total_logins_sucesso: int = db.exec(count_logins_stmt).one()  # type: ignore[assignment]
    # Descifrar IPs e contar únicos (Fernet não determinístico)
    ips_cifrados: list[str | None] = list(db.exec(all_ips_stmt).all())
    total_ips_distintos: int = len(
        {decifrar_pii(ip) for ip in ips_cifrados if ip}
    )
    logs = db.exec(
        stmt.order_by(desc(AuditLog.created_at))  # type: ignore[arg-type]
        .offset(offset)
        .limit(limite)
    ).all()

    result: list[AuditLogResumoSchema] = []
    utilizadores_map: dict[uuid.UUID, Utilizador] = {}

    utilizador_ids = {
        log.utilizador_id for log in logs if log.utilizador_id is not None
    }
    # Recolhe entidade_ids de logs sobre utilizadores (para verificar anonimização)
    entidade_utilizador_ids = {
        log.entidade_id
        for log in logs
        if log.entidade_tipo == "Utilizador" and log.entidade_id is not None
    }
    # Recolhe UUIDs referenciados nos campos *_id dos dados JSON (ex: implementador_id)
    uuid_em_dados: set[uuid.UUID] = set()
    for log in logs:
        for dados_json in (log.dados_anteriores, log.dados_novos):
            dados = _parse_json_seguro(dados_json)
            if isinstance(dados, dict):
                for chave, valor in dados.items():
                    if chave.endswith("_id") and _is_uuid_like(valor):
                        try:
                            uuid_em_dados.add(uuid.UUID(valor))
                        except ValueError:
                            pass
    todos_ids = utilizador_ids | entidade_utilizador_ids | uuid_em_dados
    if todos_ids:
        utilizadores_map = {
            utilizador.id: utilizador
            for utilizador in db.exec(
                select(Utilizador).where(Utilizador.id.in_(list(todos_ids)))
            ).all()
        }

    # IDs de utilizadores já anonimizados (para mascarar PII nos dados de log)
    anonimizados_ids: set[uuid.UUID] = {
        uid for uid, u in utilizadores_map.items() if u.anonimizado_at is not None
    }

    for log in logs:
        nome: str | None = None
        if log.utilizador_id:
            u = utilizadores_map.get(log.utilizador_id)
            if u:
                nome = decifrar_pii(u.nome)

        # Mascarar PII se o log referencia um utilizador anonimizado (RGPD)
        eh_sobre_anonimizado = _log_referencia_anonimizado(log, anonimizados_ids)
        dados_ant = _sanitizar_pii_em_dados(log.dados_anteriores) if eh_sobre_anonimizado else log.dados_anteriores
        dados_nov = _sanitizar_pii_em_dados(log.dados_novos) if eh_sobre_anonimizado else log.dados_novos

        result.append(
            AuditLogResumoSchema(
                id=log.id,
                created_at=log.created_at,
                utilizador_nome=nome,
                acao=log.acao,
                alvo_resumo=_get_resumo_alvo(log, dados_ant, dados_nov),
                mudanca_resumo=_get_resumo_mudanca(log, dados_ant, dados_nov),
                ip_address=decifrar_pii(log.ip_address),
                resultado=log.resultado,
            )
        )

    return ListaAuditLogsSchema(
        total=total,
        total_falhas=total_falhas,
        total_auth_falha=total_auth_falha,
        total_logins_sucesso=total_logins_sucesso,
        total_ips_distintos=total_ips_distintos,
        logs=result,
    )


@router.get(
    "/{log_id}",
    response_model=AuditLogSchema,
    summary="Detalhe de um registo de auditoria",
    dependencies=[AdminOuAuditorDep],
)
def get_audit_log(
    log_id: uuid.UUID,
    db: SessionDep,
    utilizador_atual: Utilizador = Depends(get_current_user),
):
    """Devolve o detalhe completo de um registo de auditoria do tenant."""
    log = db.get(AuditLog, log_id)
    if not log or log.empresa_id != utilizador_atual.empresa_id:
        raise HTTPException(status_code=404, detail="Registo não encontrado.")

    nome: str | None = None
    if log.utilizador_id:
        utilizador = db.get(Utilizador, log.utilizador_id)
        if utilizador:
            nome = decifrar_pii(utilizador.nome)

    # Recolhe todos os IDs relevantes do log para verificar anonimização (RGPD)
    ids_a_verificar: set[uuid.UUID] = set()
    if log.utilizador_id:
        ids_a_verificar.add(log.utilizador_id)
    if log.entidade_id:
        ids_a_verificar.add(log.entidade_id)
    for dados_json in (log.dados_anteriores, log.dados_novos):
        dados = _parse_json_seguro(dados_json)
        if isinstance(dados, dict):
            for chave, valor in dados.items():
                if chave.endswith("_id") and _is_uuid_like(valor):
                    try:
                        ids_a_verificar.add(uuid.UUID(valor))
                    except ValueError:
                        pass
    anonimizados_detalhe: set[uuid.UUID] = set()
    if ids_a_verificar:
        anonimizados_detalhe = {
            u.id
            for u in db.exec(
                select(Utilizador).where(
                    Utilizador.id.in_(list(ids_a_verificar)),  # type: ignore[arg-type]
                    Utilizador.anonimizado_at.isnot(None),  # type: ignore[union-attr]
                )
            ).all()
        }
    dados_ant = log.dados_anteriores
    dados_nov = log.dados_novos
    if _log_referencia_anonimizado(log, anonimizados_detalhe):
        dados_ant = _sanitizar_pii_em_dados(dados_ant)
        dados_nov = _sanitizar_pii_em_dados(dados_nov)

    return AuditLogSchema(
        created_at=log.created_at,
        utilizador_nome=nome,
        acao=log.acao,
        dados_anteriores=dados_ant,
        dados_novos=dados_nov,
        ip_address=decifrar_pii(log.ip_address),
        user_agent=decifrar_pii(log.user_agent),
        resultado=log.resultado,
    )
