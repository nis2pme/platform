"""
Schemas Pydantic para o módulo de empresas (tenant).
Cobre leitura e atualização de dados da empresa pelo admin.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, model_validator

from app.empresas.models import DimensaoEmpresa, NivelQNRCS, TipoEntidade


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
    config_seguranca: dict[str, Any] | None = None
    config_notificacoes: dict[str, Any] | None = None

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def decifrar_pii(cls, data):
        """Decifra campos PII cifrados em repouso antes da validação."""
        from app.shared.pii import decifrar_pii
        _PII_CAMPOS = ("nome", "nif", "email", "website")
        if hasattr(data, "__class__") and hasattr(data, "nome"):
            # Objeto SQLModel
            for campo in _PII_CAMPOS:
                val = getattr(data, campo, None)
                if val is not None:
                    object.__setattr__(data, campo, decifrar_pii(val))
        elif isinstance(data, dict):
            for campo in _PII_CAMPOS:
                if campo in data and data[campo] is not None:
                    data[campo] = decifrar_pii(data[campo])
        return data


class AtualizarEmpresaSchema(BaseModel):
    """Campos atualizáveis da empresa (admin only)."""

    nome: str | None = None
    nif: str | None = None
    email: str | None = None
    website: str | None = None
    setor: str | None = None
    dimensao: DimensaoEmpresa | None = None
    tipo_entidade: TipoEntidade | None = None
    nivel_qnrcs: NivelQNRCS | None = None
    locale_preferido: str | None = None
    config_seguranca: dict[str, Any] | None = None
    config_notificacoes: dict[str, Any] | None = None
