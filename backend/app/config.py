"""
Configuração centralizada da aplicação via variáveis de ambiente.
Usa pydantic-settings para validação automática dos valores.
"""
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Todas as configurações são lidas de variáveis de ambiente (ou .env).
    Valores sem default SÃO OBRIGATÓRIOS — a app não arranca sem eles.
    """

    # --- Aplicação ---
    APP_NAME: str = "NIS2PME"
    APP_VERSION: str = "0.2.0"
    DEBUG: bool = False
    # Activar Swagger UI / ReDoc explicitamente, independente do flag DEBUG.
    # Em produção deve ser False mesmo que DEBUG fique True acidentalmente (CWE-215).
    ENABLE_API_DOCS: bool = False
    DEPLOYMENT_MODE: str = "onprem"  # "saas" | "onprem"
    APP_URL: str = "http://localhost:5173"

    # --- Proxy / rede ---
    # Confiar no header CF-Connecting-IP para determinar o IP do cliente.
    # SÓ activar quando a aplicação está atrás de Cloudflare Tunnel (o edge injecta
    # o header de forma fidedigna). Em on-prem com acesso directo via Nginx DEVE
    # ficar False — caso contrário qualquer cliente forja o IP nos audit logs e
    # contorna o rate limiting (CWE-348). On-prem usa o X-Real-IP definido pelo Nginx.
    TRUST_CLOUDFLARE_HEADERS: bool = False

    # --- TLS (apenas on-prem) ---
    # Em saas o TLS é sempre tratado a montante (Cloudflare) e estas variáveis são
    # ignoradas. Em on-prem, a postura é decidida no installer e aplicada no 1.º
    # arranque (pode ser alterada depois no wizard):
    #   self-signed : edge gera certificado autoassinado (default; sem HSTS).
    #   custom      : edge usa o certificado de confiança em TLS_CERT_PATH/TLS_KEY_PATH (com HSTS).
    #   proxy       : TLS a montante; edge serve HTTP e preserva o X-Forwarded-Proto recebido.
    TLS_MODE: str = "self-signed"
    TLS_CERT_PATH: str = ""   # caminho do certificado (modo custom)
    TLS_KEY_PATH: str = ""    # caminho da chave privada (modo custom)

    @field_validator("TLS_MODE")
    @classmethod
    def validar_tls_mode(cls, v: str) -> str:
        """Garante que TLS_MODE é um dos valores aceites."""
        v = (v or "self-signed").strip().lower()
        if v not in ("self-signed", "proxy", "custom"):
            raise ValueError("TLS_MODE deve ser 'self-signed', 'proxy' ou 'custom'")
        return v

    # --- Verificação de atualizações ---
    # A app verifica periodicamente se há nova versão; o pedido envia apenas um
    # identificador anónimo da instância + a versão (nunca dados de clientes).
    # Desligável (VERIFY_UPDATES=false).
    VERIFY_UPDATES: bool = True
    UPDATE_CHECK_URL: str = "https://update.nis2pme.pt/v1/check-updates"

    # --- Base de dados ---
    DATABASE_URL: str
    # SQL echo separado de DEBUG para não expor queries com parâmetros em produção (CWE-532)
    SQL_ECHO: bool = False

    # --- JWT (tenants) ---
    JWT_SECRET_KEY: str = ""
    JWT_REFRESH_SECRET_KEY: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # COOKIE_SECURE: derivado automaticamente de APP_URL.
    # True se APP_URL usa https:// (domínio ou IP com SSL) — garante cookie httpOnly só enviado em HTTPS.
    # False se APP_URL usa http:// (acesso por IP sem SSL, desenvolvimento local).
    # NÃO configurar manualmente — definir APP_URL correctamente e este valor é calculado.
    @property
    def COOKIE_SECURE(self) -> bool:  # type: ignore[override]
        return self.APP_URL.startswith("https://")

    # --- TOTP (cifra Fernet em repouso) ---
    TOTP_ENCRYPTION_KEY: str = ""  # Auto-gerado no entrypoint se não definido

    # --- CORS ---
    # Default derivado de APP_URL: mesmo origen — frontend e backend estao na mesma porta via nginx.
    CORS_ORIGINS: list[str] = []

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        """Aceita JSON array, string separada por vírgulas, ou string vazia (auto-deriva depois)."""
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("["):
                import json
                try:
                    return json.loads(v)
                except json.JSONDecodeError:
                    return []
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    # --- Uploads de evidências ---
    UPLOADS_DIR: str = "uploads"                        # directório raiz (relativo ao cwd)
    MAX_UPLOAD_SIZE_MB: int = 10                        # tamanho máximo por ficheiro
    ALLOWED_UPLOAD_MIME_TYPES: list[str] = [
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/plain",
        "application/zip",
    ]

    @field_validator("ALLOWED_UPLOAD_MIME_TYPES", mode="before")
    @classmethod
    def parse_mime_types(cls, v):
        """Aceita JSON array ou string separada por vírgulas."""
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json
                return json.loads(v)
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    # --- Email ---
    # Desativar email completamente (dev local sem SMTP/Brevo).
    # Quando False: sem validação de credenciais no arranque, sem envio — o link de reset é apenas logado.
    EMAIL_ENABLED: bool = False
    # Provedor: "smtp" (default) ou "brevo"
    EMAIL_PROVIDER: str = "smtp"

    # Brevo API (usado quando EMAIL_PROVIDER="brevo")
    BREVO_API_KEY: str = Field(default="", repr=False)  # repr=False impede exposição em logs (CWE-532)

    @field_validator("BREVO_API_KEY", mode="before")
    @classmethod
    def normalizar_brevo_api_key(cls, v: str) -> str:
        """
        Remove espaços/newlines acidentais na chave — causa comum de 401.
        As chaves Brevo começam sempre com 'xkeysib-'.
        """
        if isinstance(v, str):
            v = v.strip()
        return v

    # SMTP (usado quando EMAIL_PROVIDER="smtp")
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = Field(default="", repr=False)  # repr=False impede exposição em logs (CWE-532)
    SMTP_FROM_EMAIL: str = "noreply@nis2pme.pt"
    SMTP_FROM_NAME: str = "NIS2PME"
    SMTP_TLS: bool = True

    @field_validator("EMAIL_PROVIDER")
    @classmethod
    def validar_email_provider(cls, v: str) -> str:
        """Garante que EMAIL_PROVIDER é um dos valores suportados."""
        if v not in ("smtp", "brevo"):
            raise ValueError("EMAIL_PROVIDER deve ser 'smtp' ou 'brevo'")
        return v

    @model_validator(mode="after")
    def validar_configuracao_email(self) -> "Settings":
        """
        Valida que as credenciais necessárias para o provedor de email
        estão configuradas. Falha no arranque com mensagem clara em vez
        de 401/timeout em runtime durante o envio.
        Ignorado quando EMAIL_ENABLED=false (desenvolvimento local sem email).
        """
        if not self.EMAIL_ENABLED:
            return self
        if self.EMAIL_PROVIDER == "brevo":
            if not self.BREVO_API_KEY:
                raise ValueError(
                    "EMAIL_PROVIDER=brevo mas BREVO_API_KEY está vazia. "
                    "Configure BREVO_API_KEY no ficheiro .env."
                )
            if not self.BREVO_API_KEY.startswith("xkeysib-"):
                raise ValueError(
                    "BREVO_API_KEY com formato inválido — deve começar com 'xkeysib-'. "
                    "Confirme que copiou a chave de API Transacional em "
                    "https://app.brevo.com/settings/keys/api (não a chave de campanhas)."
                )
        if self.EMAIL_PROVIDER == "smtp" and not self.SMTP_HOST:
            raise ValueError(
                "EMAIL_PROVIDER=smtp mas SMTP_HOST está vazio. "
                "Configure SMTP_HOST no ficheiro .env (ou mude EMAIL_PROVIDER=brevo)."
            )
        # APP_URL com localhost gera links de email inacessíveis em staging/prod.
        # Só é aceitável em desenvolvimento local (EMAIL_PROVIDER=smtp sem host real).
        if "localhost" in self.APP_URL and self.EMAIL_PROVIDER == "brevo":
            raise ValueError(
                f"APP_URL='{self.APP_URL}' contém 'localhost' mas EMAIL_PROVIDER=brevo. "
                "Os links de reset de password enviados por email serão inacessíveis. "
                "Configure APP_URL com o domínio público (ex: https://nis2pme.pt) no .env."
            )
        return self

    @model_validator(mode="after")
    def derivar_cors_e_validar_secrets(self) -> "Settings":
        """
        1. Se CORS_ORIGINS estiver vazio, deriva-o de APP_URL (mesmo-origem via nginx).
        2. Valida que todos os secrets estão presentes — devem ter sido injectados pelo
           entrypoint.sh (auto-gerado em /app/data/auto-secrets.env) ou definidos no .env.
           Falhar aqui é mais útil que erros crípticos em runtime.
        """
        # --- Derivar CORS de APP_URL se não definido ---
        if not self.CORS_ORIGINS:
            self.CORS_ORIGINS = [self.APP_URL.rstrip("/")]

        # --- Validar secrets obrigatórios ---
        # Em desenvolvimento local sem entrypoint (dev directo) aceita-se string vazia
        # apenas se DEPLOYMENT_MODE != onprem.
        if self.DEPLOYMENT_MODE == "onprem":
            campos_obrigatorios = {
                "JWT_SECRET_KEY": self.JWT_SECRET_KEY,
                "JWT_REFRESH_SECRET_KEY": self.JWT_REFRESH_SECRET_KEY,
                "TOTP_ENCRYPTION_KEY": self.TOTP_ENCRYPTION_KEY,
                "EVIDENCE_ENCRYPTION_KEY": self.EVIDENCE_ENCRYPTION_KEY,
                "PII_ENCRYPTION_KEY": self.PII_ENCRYPTION_KEY,
            }
            em_falta = [k for k, v in campos_obrigatorios.items() if not v]
            if em_falta:
                raise ValueError(
                    f"Secrets em falta: {', '.join(em_falta)}. "
                    "Em modo on-prem, o entrypoint.sh gera estes valores automaticamente. "
                    "Se estás a executar o backend fora do Docker, copia os valores de "
                    "/app/data/auto-secrets.env para o teu .env."
                )
        return self

    @field_validator("DEPLOYMENT_MODE")
    @classmethod
    def validar_deployment_mode(cls, v: str) -> str:
        """Garante que DEPLOYMENT_MODE é um dos valores aceites."""
        if v not in ("saas", "onprem"):
            raise ValueError(
                "DEPLOYMENT_MODE deve ser 'saas' ou 'onprem'"
            )
        return v

    # --- Cifra de ficheiros de evidência (Fernet) ---
    EVIDENCE_ENCRYPTION_KEY: str = ""  # Auto-gerado no entrypoint se não definido

    # --- Cifra de campos PII em repouso (Fernet) ---
    PII_ENCRYPTION_KEY: str = ""  # Auto-gerado no entrypoint se não definido

    @field_validator("JWT_SECRET_KEY", "JWT_REFRESH_SECRET_KEY")
    @classmethod
    def validar_chaves_jwt(cls, v: str, info) -> str:
        """Impede uso de chaves óbvias ou demasiado curtas. Vazio é aceite (auto-gerado no entrypoint)."""
        if v and (v.startswith("CHANGE_ME") or len(v) < 32):
            raise ValueError(
                f"{info.field_name} deve ter pelo menos 32 caracteres "
                "e não pode ser o valor padrão."
            )
        return v

    @field_validator("TOTP_ENCRYPTION_KEY")
    @classmethod
    def validar_totp_encryption_key(cls, v: str) -> str:
        """Valida que TOTP_ENCRYPTION_KEY é uma chave Fernet válida. Vazio é aceite (auto-gerado)."""
        import base64

        if not v:
            return v  # auto-gerado no entrypoint
        if v.startswith("CHANGE_ME"):
            raise ValueError(
                "TOTP_ENCRYPTION_KEY não pode ser o valor padrão. "
                "Gerar com: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        try:
            decoded = base64.urlsafe_b64decode(v)
            if len(decoded) != 32:
                raise ValueError("Comprimento incorrecto")
        except Exception:
            raise ValueError(
                "TOTP_ENCRYPTION_KEY inválida — deve ser uma chave Fernet (base64 url-safe, 32 bytes). "
                "Gerar com: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        return v

    @field_validator("EVIDENCE_ENCRYPTION_KEY")
    @classmethod
    def validar_evidence_encryption_key(cls, v: str) -> str:
        """Valida formato da chave Fernet de cifra de evidências (se definida)."""
        import base64

        if not v:
            return v  # Vazio é aceite (cifra desativada)
        if v.startswith("CHANGE_ME"):
            raise ValueError(
                "EVIDENCE_ENCRYPTION_KEY não pode ser o valor padrão. "
                "Gerar com: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        try:
            decoded = base64.urlsafe_b64decode(v)
            if len(decoded) != 32:
                raise ValueError("Comprimento incorreto")
        except Exception:
            raise ValueError(
                "EVIDENCE_ENCRYPTION_KEY inválida — deve ser uma chave Fernet (base64 url-safe, 32 bytes). "
                "Gerar com: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        return v

    @field_validator("PII_ENCRYPTION_KEY")
    @classmethod
    def validar_pii_encryption_key(cls, v: str) -> str:
        """Valida formato da chave Fernet de cifra de campos PII (se definida)."""
        import base64

        if not v:
            return v  # Vazio é aceite (sem cifra PII — apenas em desenvolvimento)
        if v.startswith("CHANGE_ME"):
            raise ValueError(
                "PII_ENCRYPTION_KEY não pode ser o valor padrão. "
                "Gerar com: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        try:
            decoded = base64.urlsafe_b64decode(v)
            if len(decoded) != 32:
                raise ValueError("Comprimento incorreto")
        except Exception:
            raise ValueError(
                "PII_ENCRYPTION_KEY inválida — deve ser uma chave Fernet (base64 url-safe, 32 bytes). "
                "Gerar com: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        return v

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """
    Retorna singleton das settings (cached).
    Usar como dependência FastAPI: Depends(get_settings).
    """
    return Settings()
