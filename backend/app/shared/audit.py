"""
Modelo AuditLog e helper registar_acao().
A tabela AuditLog é append-only — nunca UPDATE, nunca DELETE.
Retenção mínima: 12 meses (requisito NIS2 / DL 125/2025).
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from fastapi import Request
from sqlmodel import Field, Session, SQLModel


class ResultadoAcao(str, Enum):
    SUCESSO = "sucesso"
    FALHA = "falha"


class AuditLog(SQLModel, table=True):
    """
    Registo imutável de todas as ações relevantes na plataforma.
    Sem updated_at — registos nunca são alterados após criação.
    """

    __tablename__ = "audit_logs"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        index=True,
    )

    # Contexto do tenant (nullable para ações de plataforma)
    empresa_id: uuid.UUID | None = Field(default=None, index=True)
    # Utilizador responsável pela ação (nullable para ações de sistema)
    utilizador_id: uuid.UUID | None = Field(default=None, index=True)

    # Ação estruturada: "entidade.verbo", ex: "utilizador.login_sucesso"
    acao: str = Field(max_length=100, index=True)

    # Entidade afetada (opcional)
    entidade_tipo: str | None = Field(default=None, max_length=50)
    entidade_id: uuid.UUID | None = Field(default=None)

    # Estado antes/depois (JSON serializado como string) — sem dados pessoais sensíveis
    dados_anteriores: str | None = Field(default=None)  # JSON string
    dados_novos: str | None = Field(default=None)       # JSON string

    # Contexto HTTP (cifrado em repouso com PII_ENCRYPTION_KEY)
    ip_address: str | None = Field(default=None, max_length=200)  # cifrado
    user_agent: str | None = Field(default=None, max_length=700)  # cifrado

    resultado: ResultadoAcao = Field(default=ResultadoAcao.SUCESSO)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)


# ---------------------------------------------------------------------------
# Strings de ação padronizadas — única fonte de verdade
# ---------------------------------------------------------------------------

class Acao:
    """Constantes para o campo `acao` do AuditLog."""

    # Autenticação
    LOGIN_SUCESSO = "utilizador.login_sucesso"
    LOGIN_FALHA = "utilizador.login_falha"
    LOGOUT = "utilizador.logout"
    REFRESH_TOKEN = "utilizador.token_renovado"
    FA2_VERIFICADO = "utilizador.2fa_verificado"
    FA2_FALHOU = "utilizador.2fa_falhou"
    FA2_ATIVADO = "utilizador.2fa_ativado"
    FA2_DESATIVADO = "utilizador.2fa_desativado"
    BACKUP_CODE_USADO = "utilizador.backup_code_usado"
    PASSWORD_ALTERADA = "utilizador.password_alterada"
    PASSWORD_RESET_PEDIDO = "utilizador.password_reset_pedido"
    PASSWORD_RESET_CONFIRMADO = "utilizador.password_reset_confirmado"
    PASSWORD_RESET_ADMIN = "utilizador.password_reset_admin"
    MFA_RESET_ADMIN = "utilizador.mfa_reset_admin"

    # Gestão de utilizadores (apenas admin)
    UTILIZADOR_CRIADO = "utilizador.criado"
    UTILIZADOR_DESATIVADO = "utilizador.desativado"
    UTILIZADOR_REATIVADO = "utilizador.reativado"
    UTILIZADOR_ROLE_ALTERADO = "utilizador.role_alterado"
    UTILIZADOR_DELEGACAO_ATRIBUIDA = "utilizador.delegacao_atribuida"
    UTILIZADOR_DELEGACAO_REMOVIDA = "utilizador.delegacao_removida"
    UTILIZADOR_ANONIMIZADO = "utilizador.anonimizado"

    # Registo de empresa
    EMPRESA_REGISTADA = "empresa.registada"
    EMPRESA_DADOS_ATUALIZADOS = "empresa.dados_atualizados"
    EMPRESA_DADOS_EXPORTADOS = "empresa.dados_exportados"
    EMPRESA_ELIMINACAO_PEDIDA = "empresa.conta_eliminacao_pedida"

    # Controlos
    CONTROLO_ESTADO_ALTERADO = "controlo.estado_alterado"
    CONTROLO_NIVEL_ALTERADO = "controlo.nivel_maturidade_alterado"
    CONTROLO_CHECK_CONCLUIDO = "controlo.check_concluido"
    CONTROLO_CHECK_REVERTIDO = "controlo.check_revertido"
    CONTROLO_APROVADO = "controlo.aprovado"
    CONTROLO_NAO_APROVADO = "controlo.nao_aprovado"

    # Evidências
    EVIDENCIA_UPLOAD = "evidencia.upload"
    EVIDENCIA_ELIMINADA = "evidencia.eliminada"

    # Relatórios
    RELATORIO_EXPORTADO = "relatorio.exportado"

    # Relatórios de auditoria
    RELATORIO_AUDITORIA_CRIADO = "relatorio_auditoria.criado"


# ---------------------------------------------------------------------------
# Helper principal
# ---------------------------------------------------------------------------

# Chaves que NUNCA devem aparecer serializada nos audit logs (CWE-532)
_CHAVES_SENSIVEIS = frozenset({
    "password", "password_hash", "nova_password", "password_antiga",
    "totp_secret", "totp_secret_cifrado", "backup_codes",
    "token", "refresh_token", "access_token", "token_hash",
    "secret", "key", "api_key", "smtp_password",
})


def _sanitize_dados(dados: dict[str, Any] | None) -> dict[str, Any] | None:
    """
    Remove recursivamente chaves sensíveis de um dict antes de serializar para o AuditLog.
    Substitui o valor por '[REDACTED]' para manter a estrutura mas sem expor o dado.
    """
    if dados is None:
        return None
    resultado = {}
    for k, v in dados.items():
        if k.lower() in _CHAVES_SENSIVEIS:
            resultado[k] = "[REDACTED]"
        elif isinstance(v, dict):
            resultado[k] = _sanitize_dados(v)
        else:
            resultado[k] = v
    return resultado


def registar_acao(
    db: Session,
    *,
    acao: str,
    resultado: ResultadoAcao = ResultadoAcao.SUCESSO,
    empresa_id: uuid.UUID | None = None,
    utilizador_id: uuid.UUID | None = None,
    entidade_tipo: str | None = None,
    entidade_id: uuid.UUID | None = None,
    dados_anteriores: dict[str, Any] | None = None,
    dados_novos: dict[str, Any] | None = None,
    request: Request | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    force_commit: bool = False,
) -> AuditLog:
    """
    Regista uma ação no AuditLog de forma atómica.
    Nunca loga passwords, tokens ou dados pessoais sensíveis.

    Args:
        db: Sessão de base de dados ativa.
        acao: String estruturada da ação (usar constantes Acao.*).
        resultado: sucesso ou falha.
        empresa_id: UUID do tenant, se aplicável.
        utilizador_id: UUID do utilizador, se aplicável.
        entidade_tipo: Tipo da entidade afetada (ex: "Controlo").
        entidade_id: UUID da entidade afetada.
        dados_anteriores: Dict com estado anterior (sem dados sensíveis).
        dados_novos: Dict com estado novo (sem dados sensíveis).
        request: Objeto Request do FastAPI para extrair IP e User-Agent.
        ip_address: IP override (se request não disponível).
        user_agent: User-Agent override (se request não disponível).
        force_commit: Se True, usa sessão independente e faz commit imediato.
            Usar em casos de falha de autenticação, onde o caller lança
            HTTPException logo a seguir e a transação principal sofre rollback.
    """
    import json
    from app.shared.pii import cifrar_pii

    # Extrai IP e User-Agent do request se disponível
    if request is not None:
        ip = _extrair_ip(request)
        ua = request.headers.get("user-agent", "")[:500]
    else:
        ip = ip_address
        ua = user_agent

    entrada = AuditLog(
        empresa_id=empresa_id,
        utilizador_id=utilizador_id,
        acao=acao,
        entidade_tipo=entidade_tipo,
        entidade_id=entidade_id,
        dados_anteriores=json.dumps(_sanitize_dados(dados_anteriores)) if dados_anteriores else None,
        dados_novos=json.dumps(_sanitize_dados(dados_novos)) if dados_novos else None,
        ip_address=cifrar_pii(ip),
        user_agent=cifrar_pii(ua),
        resultado=resultado,
    )

    if force_commit:
        # Sessão independente: persiste o log mesmo que a transação principal
        # sofra rollback (ex: HTTPException lançada logo após esta chamada).
        from app.database import engine as _engine
        with Session(_engine) as audit_session:
            audit_session.add(entrada)
            audit_session.commit()
    else:
        db.add(entrada)
        # Não fazemos commit aqui — o caller (ou o middleware get_session) é responsável.
        # Isto garante que o log é parte da mesma transação que a ação principal.

    return entrada


def _extrair_ip(request: Request) -> str:
    """Resolve o IP real do cliente, resistente a spoofing (CWE-348).

    Delega na fonte única partilhada (app.shared.utils.obter_ip_cliente) para
    manter a mesma lógica na auditoria e no rate limiting.
    """
    from app.shared.utils import obter_ip_cliente

    return obter_ip_cliente(request)
