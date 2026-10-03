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


@dataclass(frozen=True)
class EstadoLicenca:
    """Estado agregado da licença para o cartão no UI (não é por-feature).

    `estado` é uma string estável que o frontend mapeia por i18n:
      "ativa" | "em_grace" | "expirada" | "invalida" | "gerida" | "sem_premium"
      | "indisponivel" (sidecar inalcançável — fail-soft, não é erro do UI).
    """

    estado: str
    plano: str = ""
    expires_at: str | None = None       # RFC3339
    grace_ate: str | None = None        # RFC3339
    dias_restantes: int = 0
    # Código de instalação (on-prem): o que o cliente envia ao fornecedor para
    # obter/renovar a licença. Vazio em SaaS ou sem sidecar.
    codigo_instalacao: str = ""
    instance_id: str = ""
    # Validação pelo serviço (on-prem): "ok" | "em_falta" | "so_leitura" |
    # "nao_validada" | "revogada" | "airgap".
    heartbeat_estado: str = ""
    heartbeat_ultimo_ok: str | None = None
    dias_sem_heartbeat: int = 0
    # Os módulos premium estão só de leitura (a licença deixou de valer, ou falta
    # a validação pelo serviço), e porquê: "expirada" | "revogada" |
    # "nao_validada" | "sem_validacao". Os dados leem-se e exportam-se sempre.
    so_leitura: bool = False
    so_leitura_motivo: str = ""

    @classmethod
    def simples(cls, estado: str) -> "EstadoLicenca":
        """Estado sem datas (ex.: "sem_premium", "indisponivel")."""
        return cls(estado=estado)


# ── Assistente IA — schemas devolvidos ao frontend ───────────────────────────

class EstadoAnaliseIA(str, Enum):
    PENDENTE = "pendente"
    PROCESSANDO = "processando"
    CONCLUIDO = "concluido"
    ERRO = "erro"


@dataclass
class LicencaInstalada:
    """Resultado de instalar/validar um ficheiro de licença (do sidecar)."""

    aceite: bool
    codigo_erro: str = ""
    detalhe: str = ""
    customer: str = ""
    nif_licenca: str = ""
    plano: str = ""
    expires_at: str | None = None
    grace_dias: int = 0
    modulos: list[str] = field(default_factory=list)
    license_id: str = ""
    instalada: bool = False


class InstalarLicencaIn(BaseModel):
    """O envelope colado pelo administrador (texto JSON `{payload_b64, sig}`)."""

    envelope: str = Field(min_length=20, max_length=65536)
    # true = só validar e mostrar o resumo (o administrador confirma a seguir).
    so_validar: bool = False


class LicencaInstaladaSchema(BaseModel):
    aceite: bool
    customer: str = ""
    nif_licenca: str = ""
    plano: str = ""
    expires_at: str | None = None
    grace_dias: int = 0
    modulos: list[str] = Field(default_factory=list)
    license_id: str = ""
    instalada: bool = False


class EstadoLicencaSchema(BaseModel):
    """Estado da licença devolvido ao frontend (cartão de licença)."""

    estado: str  # "ativa"|"em_grace"|"expirada"|"invalida"|"gerida"|"sem_premium"|"indisponivel"
    plano: str = ""
    expires_at: str | None = None
    grace_ate: str | None = None
    dias_restantes: int = 0
    codigo_instalacao: str = ""
    instance_id: str = ""
    heartbeat_estado: str = ""
    heartbeat_ultimo_ok: str | None = None
    dias_sem_heartbeat: int = 0
    # Módulos premium só de leitura, e porquê (o cartão explica o que fazer).
    so_leitura: bool = False
    so_leitura_motivo: str = ""
    # On-prem sem NIF na empresa: não há código de instalação até o definir.
    empresa_sem_nif: bool = False


class PrazoAcessoSchema(BaseModel):
    """Fim do trial (SaaS) ou da licença (on-prem), para a faixa no topo da app."""

    tipo: str = ""            # "trial" | "licenca" | "" (nada a avisar)
    nivel: str = ""           # "" | "info" | "aviso" | "urgente"
    termina_em: datetime | None = None
    tolerancia_ate: datetime | None = None
    dias: int | None = None
    em_tolerancia: bool = False
    expirada: bool = False
    fechavel: bool = True
    heartbeat: str = ""       # on-prem: "em_falta" | "so_leitura" | "nao_validada" | "revogada" | ""
    dias_sem_heartbeat: int = 0
    # Página dos planos (SaaS), se a plataforma a tiver configurada.
    planos_url: str = ""


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
