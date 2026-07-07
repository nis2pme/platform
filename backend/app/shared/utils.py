"""
Utilitários partilhados: geração de tokens, validação de passwords,
locale e helpers de autenticação.
"""
import hashlib
import re
import secrets
from typing import Any

from fastapi import HTTPException, status
from jose import JWTError, jwt


# ---------------------------------------------------------------------------
# Rede — IP do cliente (resistente a spoofing)
# ---------------------------------------------------------------------------

def obter_ip_cliente(request: Any) -> str:
    """Resolve o IP real do cliente de forma resistente a spoofing (CWE-348).

    Fonte única partilhada pela auditoria e pelo rate limiting:
    - CF-Connecting-IP só é considerado quando TRUST_CLOUDFLARE_HEADERS=True
      (deployment atrás de Cloudflare Tunnel, onde o edge injecta o header e o
      cliente final não o consegue forjar).
    - Caso contrário usa-se X-Real-IP, que o Nginx define com $remote_addr — valor
      que o cliente não controla. É a fonte fidedigna no modo on-prem.
    - Fallback: endereço do socket (ligação directa / desenvolvimento).
    """
    from app.config import get_settings

    if get_settings().TRUST_CLOUDFLARE_HEADERS:
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.split(",")[0].strip()[:45]

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.split(",")[0].strip()[:45]

    client = getattr(request, "client", None)
    if client:
        return str(client.host)[:45]

    return "unknown"


def pedido_e_seguro(request: Any) -> bool:
    """True se o pedido chegou via HTTPS (CWE-614 / CWE-1004).

    Usa o X-Forwarded-Proto definido pelo Nginx (`$scheme`, não controlável pelo
    cliente) e cai no esquema do próprio pedido. Permite definir o atributo Secure
    dos cookies em função da ligação real, em vez de o inferir de uma string de
    configuração (APP_URL) — o cookie fica Secure exactamente quando se serve HTTPS,
    sem reinício e independentemente de o endereço ser IP ou domínio.
    """
    xfp = request.headers.get("x-forwarded-proto", "")
    if xfp and xfp.split(",")[0].strip().lower() == "https":
        return True
    url = getattr(request, "url", None)
    return getattr(url, "scheme", None) == "https"


def host_e_dominio(host: str | None) -> bool:
    """True se `host` for um nome de domínio (não um IP literal nem localhost).

    Usado para decidir se é seguro enviar HSTS: num deployment por IP — forçosamente
    com certificado self-signed — o HSTS impediria o click-through do aviso de
    certificado e poderia trancar o acesso ao browser. Espera um hostname já sem porta
    (ex.: `urlparse(APP_URL).hostname`).
    """
    if not host:
        return False
    import ipaddress

    h = host.strip().lower()
    if h == "localhost":
        return False
    try:
        ipaddress.ip_address(h)
        return False  # é um IP literal
    except ValueError:
        return True  # é um nome de domínio


# ---------------------------------------------------------------------------
# Tokens seguros
# ---------------------------------------------------------------------------

def decodificar_jwt_temp(token: str, secret: str, tipo_esperado: str, algoritmo: str = "HS256") -> dict:
    """Valida e decodifica um token temporário JWT. Lança 401 se inválido ou tipo errado.

    Genérico: o segredo é passado pelo chamador, não fixo neste módulo.
    """
    try:
        payload = jwt.decode(token, secret, algorithms=[algoritmo])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token temporário inválido ou expirado.",
        )
    if payload.get("type") != tipo_esperado:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token temporário inválido para esta operação.",
        )
    return payload


def gerar_token_opaco(nbytes: int = 32) -> str:
    """
    Gera um token opaco criptograficamente seguro (URL-safe base64).
    Usado para refresh tokens e reset de password.

    Returns:
        Token em texto limpo — para enviar ao cliente UMA vez.
        Armazena sempre o hash SHA-256, nunca o token em texto limpo.
    """
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """
    Calcula o SHA-256 hex do token para armazenamento seguro na DB.
    Não usar bcrypt aqui — SHA-256 é suficiente para tokens com entropia alta.
    """
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Validação de password
# ---------------------------------------------------------------------------

# Requisitos: min 8 chars, pelo menos 1 maiúscula, 1 minúscula, 1 dígito, 1 especial
_SENHA_RE = re.compile(
    r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>\/?]).{8,}$"
)


def validar_forca_password(password: str) -> tuple[bool, str]:
    """
    Valida a força da password segundo as regras NIS2PME.

    Returns:
        Tuple (valida, mensagem_erro).
        Se valida=True, mensagem_erro é string vazia.
    """
    if len(password) < 8:
        return False, "A password deve ter pelo menos 8 caracteres."
    if not re.search(r"[a-z]", password):
        return False, "A password deve conter pelo menos uma letra minúscula."
    if not re.search(r"[A-Z]", password):
        return False, "A password deve conter pelo menos uma letra maiúscula."
    if not re.search(r"\d", password):
        return False, "A password deve conter pelo menos um dígito."
    if not re.search(r"[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>\/?]", password):
        return False, "A password deve conter pelo menos um carácter especial."
    return True, ""


# ---------------------------------------------------------------------------
# Códigos de backup 2FA
# ---------------------------------------------------------------------------

def gerar_codigos_backup(n: int = 10) -> list[str]:
    """
    Gera n códigos de backup para 2FA no formato XXXX-XXXX-XXXX.
    Cada código tem 12 chars alfanuméricos (sem ambiguidade: sem 0/O, 1/I/l).

    Returns:
        Lista de n códigos em texto limpo — mostrar ao utilizador UMA vez.
        Armazenar sempre os hashes bcrypt na DB.
    """
    # Alphabet sem caracteres ambíguos
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codigos = []
    for _ in range(n):
        parte1 = "".join(secrets.choice(alphabet) for _ in range(4))
        parte2 = "".join(secrets.choice(alphabet) for _ in range(4))
        parte3 = "".join(secrets.choice(alphabet) for _ in range(4))
        codigos.append(f"{parte1}-{parte2}-{parte3}")
    return codigos


def normalizar_codigo_backup(codigo: str) -> str:
    """Normaliza um código de backup: uppercase e remove hífens e espaços."""
    return codigo.upper().replace("-", "").replace(" ", "")


# ---------------------------------------------------------------------------
# Locale helpers
# ---------------------------------------------------------------------------

def parse_accept_language(header: str | None) -> str | None:
    """
    Extrai o primary language tag do header Accept-Language.

    Ex: 'pt-PT,pt;q=0.9,en;q=0.8' → 'pt'
    Ex: 'en' → 'en'
    Ex: None → None
    """
    if not header:
        return None
    # Primeiro tag (antes da vírgula), sem qualidade nem região
    primary = header.split(",")[0].split(";")[0].strip()
    lang = primary.split("-")[0].lower()
    return lang if lang else None


def resolver_locale(
    empresa: Any,
    framework: Any,
    *,
    request: Any | None = None,
) -> str:
    """Resolve o locale para operações de dados.

    Ordem de preferência: Accept-Language header → empresa.locale_preferido → framework.default_locale.
    Ponto único partilhado entre relatorios, controlos, evidencias e plano_prioritario.
    """
    if request is not None:
        locale_header = parse_accept_language(
            getattr(request, "headers", {}).get("accept-language")
        )
        if locale_header:
            return locale_header
    return getattr(empresa, "locale_preferido", None) or getattr(framework, "default_locale", "pt")
