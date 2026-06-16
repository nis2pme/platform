"""Schemas do módulo de verificação de atualizações."""
from typing import Optional

from pydantic import BaseModel


class UpdateStatusSchema(BaseModel):
    """Resposta ao GET /updates/status."""

    verificar_ativo: bool
    versao_atual: str
    ultima_versao: Optional[str] = None
    update_disponivel: bool = False
    security_critical: bool = False
    notes_url: Optional[str] = None


class UpdateConfigSchema(BaseModel):
    """Payload para POST /updates/config (ligar/desligar a verificação)."""

    verificar: bool


class UpdateConfigRespostaSchema(BaseModel):
    """Resposta ao POST /updates/config."""

    verificar_ativo: bool
