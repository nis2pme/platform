"""
Tipos do lado-cliente do contrato premium (sem dependência de gRPC nem DB).

Inclui o resultado de verificação de entitlement e os schemas do Assistente IA
devolvidos ao frontend. Nota: aqui NÃO há modelo de tabela — o open-core não
guarda o job da IA; a store do job vive no sidecar (premium-data-db).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class Entitlement:
    """Resultado da verificação de um direito premium para um tenant."""

    feature: str
    enabled: bool
    limits: dict[str, str] = field(default_factory=dict)
    expires_at: str | None = None
    reason: str = ""

    @classmethod
    def disabled(cls, feature: str, reason: str = "premium_disabled") -> "Entitlement":
        """Atalho para um direito negado (premium off ou tenant sem o módulo)."""
        return cls(feature=feature, enabled=False, reason=reason)


# ── Assistente IA — schemas devolvidos ao frontend ───────────────────────────

class EstadoAnaliseIA(str, Enum):
    PENDENTE = "pendente"
    PROCESSANDO = "processando"
    CONCLUIDO = "concluido"
    ERRO = "erro"


class RelatorioGapsSchema(BaseModel):
    """Relatório estruturado de análise de gaps."""

    resumo_executivo: str = ""
    pontos_positivos: list[str] = Field(default_factory=list)
    lacunas_identificadas: list[str] = Field(default_factory=list)
    recomendacoes: list[str] = Field(default_factory=list)
    score_qualidade_documentacao: int = 0  # 0-100
    score_robustez_implementacao: int = 0  # 0-100
    nivel_confianca: str = ""               # "alto" | "medio" | "baixo"
    gerado_em: str = ""                      # RFC3339


class AnaliseIASchema(BaseModel):
    """Estado do job de análise IA, devolvido no polling do frontend."""

    id: uuid.UUID
    controlo_empresa_id: uuid.UUID
    estado: EstadoAnaliseIA
    relatorio: RelatorioGapsSchema | None = None
    # CÓDIGO de erro estável (não-PII); o frontend traduz via i18n (analise_ia.erros.*).
    erro_codigo: str | None = None
    created_at: datetime
    updated_at: datetime
