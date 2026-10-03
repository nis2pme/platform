"""
Schemas do módulo Sistema — configuração do servidor de saída de correio.

A password nunca sai da aplicação: o ecrã recebe apenas se existe uma guardada.
Por isso `smtp_password` a None significa "manter a que está" e "" significa
"deixar de usar autenticação" — sem esta distinção, mudar a porta apagava a
password de quem deixasse o campo vazio.
"""
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.shared.utils import normalizar_host_rede


class _EmailSmtpBase(BaseModel):
    """Campos de ligação ao servidor SMTP, partilhados pela gravação e pelo teste."""

    smtp_host: Optional[str] = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None  # None = manter a guardada; "" = limpar
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

    def _validar_ligacao(self) -> None:
        """Regras comuns a quem vai mesmo ligar-se ao servidor."""
        if not self.smtp_host:
            raise ValueError("O endereço do servidor é obrigatório.")
        if not self.smtp_from_email:
            raise ValueError("O email de origem é obrigatório.")
        if self.smtp_tls and self.smtp_ssl:
            raise ValueError(
                "STARTTLS e TLS implícito excluem-se: escolha uma das formas "
                "de cifrar a ligação."
            )
        self.smtp_host = normalizar_host_rede(self.smtp_host)


class EmailConfigSchema(BaseModel):
    """Resposta do GET /sistema/email. Sem a password — só se existe uma."""

    ativo: bool
    notificacoes: bool
    provedor: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_from_email: str
    smtp_from_name: str
    smtp_tls: bool
    smtp_ssl: bool
    password_definida: bool


class EmailConfigUpdateSchema(_EmailSmtpBase):
    """Payload do PUT /sistema/email."""

    ativo: bool
    notificacoes: bool = True
    # Desligar o email não apaga por si as credenciais do .env; quem quiser que
    # deixem de lá estar pede-o explicitamente.
    limpar_credenciais: bool = False

    @model_validator(mode="after")
    def _validar(self) -> "EmailConfigUpdateSchema":
        if self.ativo:
            self._validar_ligacao()
        return self


class EmailTesteSchema(_EmailSmtpBase):
    """
    Payload do POST /sistema/email/testar — a configuração a experimentar.

    Não persiste nada: a configuração em vigor continua intacta enquanto o
    administrador afina a nova. O destinatário nunca vem daqui; é sempre o
    endereço de quem está autenticado.
    """

    @model_validator(mode="after")
    def _validar(self) -> "EmailTesteSchema":
        self._validar_ligacao()
        return self


class EmailTesteRespostaSchema(BaseModel):
    """Resposta ao teste de envio."""

    ok: bool
    destinatario: str
