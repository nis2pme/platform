"""Schemas do módulo de verificação de atualizações."""
from typing import Optional

from pydantic import BaseModel, Field


class UpdateStatusSchema(BaseModel):
    """Resposta ao GET /updates/status."""

    verificar_ativo: bool
    versao_atual: str
    # Canal que a instalação segue: `stable` (clientes) ou `dev`.
    canal: str = "stable"
    ultima_versao: Optional[str] = None
    update_disponivel: bool = False
    security_critical: bool = False
    notes_url: Optional[str] = None
    # Atualizar pelo interface: há pacote assinado e o agente do anfitrião existe.
    atualizavel_pelo_ui: bool = False
    # Porque não se pode: agente_inativo | sem_pacote_assinado | origem_antiga | so_onprem.
    motivo_nao_atualizavel: Optional[str] = None
    backups_ativos: bool = False
    atualizacao_em_curso: bool = False


class UpdateConfigSchema(BaseModel):
    """Payload para POST /updates/config (ligar/desligar a verificação)."""

    verificar: bool


class UpdateConfigRespostaSchema(BaseModel):
    """Resposta ao POST /updates/config."""

    verificar_ativo: bool


class UpdateAplicarSchema(BaseModel):
    """Payload para POST /updates/aplicar. A password é a de quem pede."""

    versao: str = Field(max_length=16, pattern=r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
    password: str = Field(max_length=256)
    aceito_sem_backup: bool = False


class UpdateAplicarRespostaSchema(BaseModel):
    """Resposta ao POST /updates/aplicar."""

    pedido_id: str
    versao: str


class UpdateProgressoSchema(BaseModel):
    """Resposta ao GET /updates/progresso."""

    estado: str
    fase: Optional[str] = None
    percentagem: int = 0
    versao_alvo: Optional[str] = None
    versao_origem: Optional[str] = None
    codigo: Optional[str] = None
    pedido_id: Optional[str] = None
    backup: Optional[str] = None
