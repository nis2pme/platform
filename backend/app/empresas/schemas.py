"""
Schemas Pydantic para o módulo de empresas (tenant).
Cobre leitura e atualização de dados da empresa pelo admin.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.empresas.models import DimensaoEmpresa, NivelQNRCS, TipoEntidade
from app.shared.validacao import SemNulos, teto_em_bytes


class EmpresaSchema(BaseModel):
    """Representação pública de uma empresa (tenant)."""

    id: uuid.UUID
    nome: str
    nif: str | None
    email: str | None
    website: str | None
    setor: str | None
    dimensao: DimensaoEmpresa | None
    tipo_entidade: TipoEntidade
    nivel_qnrcs: NivelQNRCS | None
    ativo: bool
    onboarding_completo: bool
    locale_preferido: str = "pt"
    created_at: datetime
    updated_at: datetime
    # A1: só existem on-prem. Em SaaS a política é fixa e **não visível** — o
    # `redigir_config_saas` abaixo esvazia-os na leitura, e a escrita é recusada
    # no serviço. Um separador escondido só no frontend deixaria a restrição
    # decorativa: quem chamasse a API a direito continuava a ver e a gravar.
    config_seguranca: dict[str, Any] | None = None
    config_notificacoes: dict[str, Any] | None = None

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def decifrar_pii(cls, data):
        """Decifra campos PII cifrados em repouso antes da validação.

        Copia os atributos para um dict em vez de mutar a entidade ORM recebida:
        mutar o objeto persistente faria o commit automático do get_session regravar
        os campos DECIFRADOS (em claro) na base — corrompia a cifra em repouso e partia
        o carregamento seguinte (texto simples não decifra → InvalidToken).
        """
        from app.shared.pii import decifrar_pii
        from app.shared.politica_seguranca import config_efetiva, configuravel

        _PII_CAMPOS = ("nome", "nif", "email", "website")
        if not isinstance(data, dict):
            data = {k: getattr(data, k) for k in cls.model_fields if hasattr(data, k)}
        for campo in _PII_CAMPOS:
            if data.get(campo) is not None:
                data[campo] = decifrar_pii(data[campo])

        # A1 — em SaaS a política do tenant é fixa e não visível. A redação vive
        # dentro deste validador, e não num segundo `mode="before"`, porque a
        # ordem entre dois validadores do mesmo modo não é garantida e é este que
        # converte a entidade ORM num dict: o outro correria antes, receberia o
        # objeto e não teria onde escrever.
        if not configuravel():
            data["config_seguranca"] = None
            data["config_notificacoes"] = None
        else:
            data["config_seguranca"] = config_efetiva(data.get("config_seguranca"))
        return data


class AtualizarEmpresaSchema(SemNulos):
    """Campos atualizáveis da empresa (admin only)."""

    nao_anulaveis = frozenset({"nome", "tipo_entidade", "locale_preferido"})

    nome: str | None = None
    nif: str | None = None
    email: str | None = None
    website: str | None = None
    setor: str | None = Field(None, max_length=100)
    dimensao: DimensaoEmpresa | None = None
    tipo_entidade: TipoEntidade | None = None
    nivel_qnrcs: NivelQNRCS | None = None
    locale_preferido: str | None = Field(None, max_length=10)
    config_seguranca: dict[str, Any] | None = None
    config_notificacoes: dict[str, Any] | None = None

    @field_validator("nome", "nif", "email", "website")
    @classmethod
    def _cabe_cifrado(cls, v: str | None, info) -> str | None:
        # Cifrados em repouso numa coluna de 500: o criptograma de mais de ~300
        # bytes não cabe.
        return teto_em_bytes(v, 300, info.field_name)

    @field_validator("locale_preferido")
    @classmethod
    def _lingua_suportada(cls, v: str | None) -> str | None:
        if v is not None and v.split("-")[0].lower() not in ("pt", "en"):
            raise ValueError("língua não suportada")
        return v
