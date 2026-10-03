"""
Schemas do módulo de setup inicial (on-prem).
"""
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.shared.utils import normalizar_host_rede


class SetupStatusSchema(BaseModel):
    """Resposta ao GET /setup/status."""

    configurado: bool
    deployment_mode: str
    # True apenas no SaaS de avaliação (trial) — seleciona a variante legal "saas-trial".
    is_trial: bool = False


class SetupIniciarSchema(BaseModel):
    """Resposta ao POST /setup/iniciar."""

    estado: str  # "disponivel" | "ocupado"


class SetupConfigurarSchema(BaseModel):
    """Payload para POST /setup/configurar."""

    # Empresa
    empresa_nome: str
    empresa_setor: str
    empresa_dimensao: str          # micro | pequena | media | grande
    empresa_tipo_entidade: str     # base | importante | essencial
    empresa_nivel_qnrcs: str       # basico | substancial | elevado

    # Administrador
    admin_nome: str
    admin_email: EmailStr
    admin_password: str

    # Consentimentos RGPD (obrigatórios)
    versao_termos: str = "1.0"
    aceitou_termos: bool
    aceitou_rgpd: bool

    # Preferência de verificação de atualizações (default ligado)
    verificar_atualizacoes: bool = True

    @field_validator("aceitou_termos", "aceitou_rgpd")
    @classmethod
    def validar_consentimentos(cls, v: bool) -> bool:
        if not v:
            raise ValueError("É necessário aceitar os termos para continuar.")
        return v


class SetupRespostaSchema(BaseModel):
    """Resposta bem-sucedida ao POST /setup/configurar.

    Não devolve sessão: devolve um token temporário para o enrolamento de 2FA
    obrigatório (o wizard conclui com /auth/login/setup-2fa/confirmar).
    """

    temp_token: str
    requires_2fa_setup: bool = True
    utilizador: dict
    # Presente apenas se SMTP não estiver configurado
    aviso_smtp: Optional[str] = None


from enum import Enum  # noqa: E402


class ModoHttps(str, Enum):
    NENHUM = "none"
    AUTOASSINADO = "self_signed"
    PROPRIO = "custom"


class SetupHttpsSchema(BaseModel):
    """Payload para POST /setup/configurar-https."""

    modo: ModoHttps
    cert_pem: Optional[str] = None   # PEM text — obrigatório para modo=custom
    key_pem: Optional[str] = None    # PEM text — obrigatório para modo=custom


class SetupHttpsRespostaSchema(BaseModel):
    """Resposta ao POST /setup/configurar-https."""

    modo: str
    aviso: Optional[str] = None


class SetupEmailSchema(BaseModel):
    """Payload para POST /setup/configurar-email."""

    usar_smtp: bool
    smtp_host: Optional[str] = None
    # Fora do intervalo dos portos TCP não há ligação possível: recusar aqui poupa
    # ao administrador um erro de envio que só apareceria muito mais tarde.
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from_email: Optional[EmailStr] = None
    smtp_from_name: str = "NIS2PME"
    smtp_tls: bool = True     # STARTTLS (porta 587)
    smtp_ssl: bool = False    # TLS implícito (porta 465)

    @field_validator("smtp_host", "smtp_user", "smtp_password", "smtp_from_name")
    @classmethod
    def _sem_carateres_de_controlo(cls, v: Optional[str]) -> Optional[str]:
        """Impede injeção de linhas no .env via CRLF (CWE-93 / CWE-74)."""
        if v is not None and any(ord(c) < 32 or ord(c) == 127 for c in v):
            raise ValueError(
                "Valor inválido: quebras de linha e carateres de controlo "
                "não são permitidos."
            )
        return v

    @model_validator(mode="after")
    def validar_smtp(self) -> "SetupEmailSchema":
        if self.usar_smtp:
            if not self.smtp_host:
                raise ValueError("smtp_host é obrigatório quando usar_smtp=true.")
            if not self.smtp_from_email:
                raise ValueError("smtp_from_email é obrigatório quando usar_smtp=true.")
            if self.smtp_tls and self.smtp_ssl:
                raise ValueError(
                    "smtp_tls (STARTTLS) e smtp_ssl (TLS implícito) excluem-se: "
                    "escolha uma das formas de cifrar a ligação."
                )
            self.smtp_host = normalizar_host_rede(self.smtp_host)
        return self


class SetupEmailRespostaSchema(BaseModel):
    """Resposta ao POST /setup/configurar-email."""

    email_ativo: bool
