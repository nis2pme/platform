"""
Configuração TLS do nginx no contexto on-prem.

Três modos (TLS_MODE, decidido no installer, alterável depois no wizard):
  - self-signed : edge termina TLS com certificado autoassinado (sem HSTS).
  - custom      : edge termina TLS com certificado de confiança (com HSTS).
  - proxy       : TLS tratado a montante (Cloudflare/Traefik/Nginx); o edge serve
                  HTTP e **preserva** o X-Forwarded-Proto recebido (cookies Secure).

O volume é montado em /app/nginx_config (backend) e /run/nginx_config (frontend).
Em modo saas o TLS é sempre tratado a montante (Cloudflare) — este módulo não atua.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from ipaddress import AddressValueError, IPv4Address
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

# Diretório partilhado entre backend e frontend via volume Docker
NGINX_CONFIG_DIR = Path(os.environ.get("NGINX_CONFIG_DIR", "/app/nginx_config"))

# Marcador (volume persistente) — TLS já inicializado a partir de TLS_MODE no
# primeiro arranque. Depois disso, alterações são geridas pelo wizard e os
# reinícios não sobrescrevem a configuração existente.
_TLS_INIT_MARKER = Path("/app/data/.tls_initialized")

def _flag_ativa(nome: str, default: bool = False) -> bool:
    """Lê uma flag booleana do ambiente (mesma convenção do pydantic-settings)."""
    valor = os.environ.get(nome)
    if valor is None:
        return default
    return valor.strip().lower() in ("1", "true", "yes", "on")


# Confiar no CF-Connecting-IP para o real_ip/auditoria SÓ quando a app está atrás de um Cloudflare
# Tunnel/proxy de confiança que injeta o header. É a MESMA flag que o backend usa para resolver o IP
# do cliente — o nginx limita-se a APLICAR a decisão na única camada onde o IP do peer ainda existe;
# quem decide é o backend. Em on-prem direto (default False) não se emite real_ip: senão um cliente
# forjava o CF-Connecting-IP e o nginx envenenava o X-Real-IP que o backend consome (CWE-348).
_TRUST_CF = _flag_ativa("TRUST_CLOUDFLARE_HEADERS", False)

# REAL_IP_FROM: a fonte de confiança quando _TRUST_CF. O ideal é o IP EXATO do gateway da bridge do
# frontend (ex. 172.21.0.1/32) — só o docker-proxy apresenta esse IP. O default é largo (toda a gama
# de bridges) para não quebrar instalações com sub-redes diferentes; aperta-se via env REAL_IP_FROM.
_REAL_IP_FROM = os.environ.get("REAL_IP_FROM", "172.16.0.0/12")
_REAL_IP = (
    f"""\
    set_real_ip_from {_REAL_IP_FROM};
    real_ip_header    CF-Connecting-IP;
    real_ip_recursive on;
"""
    if _TRUST_CF
    else ""
)

# Auditoria de IP nos logs do nginx: $realip_remote_addr = peer TCP real (deve ser o gateway da
# bridge); $remote_addr = após real_ip (= CF-Connecting-IP). Sem CF o cf= ficaria sempre vazio e o
# access_log não pode referenciar um log_format inexistente — por isso os dois andam juntos na flag.
_LOG_FORMAT = (
    """\
log_format cf_audit '$realip_remote_addr -> $remote_addr cf=$http_cf_connecting_ip '
                    '"$request" $status xff="$http_x_forwarded_for" ua="$http_user_agent"';
"""
    if _TRUST_CF
    else ""
)
_ACCESS_LOG = "    access_log /var/log/nginx/access.log cf_audit;" if _TRUST_CF else ""

# Cabeçalhos de segurança comuns (sem HSTS — HSTS só no modo custom)
_SEC_HEADERS = """\
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; frame-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'" always;"""

# ---------------------------------------------------------------------------
# Templates nginx
# ---------------------------------------------------------------------------

# HTTP simples (legado — modo "none"). Mantido por compatibilidade.
_NGINX_HTTP = f"""\
# NIS2PME — Nginx config (HTTP)
# Gerado automaticamente. Nao editar manualmente.
{_LOG_FORMAT}
server {{
    listen 80;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml text/javascript;
    gzip_min_length 1000;

{_REAL_IP}
{_ACCESS_LOG}
{_SEC_HEADERS}

    location /api/ {{
        # Re-resolver o upstream em runtime (DNS embebido do Docker) — senão o
        # nginx cacheia o IP no arranque e parte se o backend mudar de IP.
        resolver 127.0.0.11 valid=10s ipv6=off;
        set $nis2pme_backend backend;
        proxy_pass         http://$nis2pme_backend:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_connect_timeout 10s;
        proxy_send_timeout 120s;
        client_max_body_size 15M;
    }}

    location / {{
        try_files $uri $uri/ /index.html;
    }}

    location ~ /\\. {{
        deny all;
        access_log off;
        log_not_found off;
    }}
}}
"""

# Modo proxy: TLS a montante. Serve HTTP, SEM redirect para HTTPS, e preserva o
# X-Forwarded-Proto do proxy (cai em $scheme se ausente) para os cookies Secure
# refletirem a ligação real do browser ao proxy.
# NOTA: assume que apenas o proxy de confiança alcança este edge (o backend não
# está exposto diretamente). Caso contrário um cliente poderia forjar o header.
_NGINX_PROXY = f"""\
# NIS2PME — Nginx config (proxy / TLS a montante)
# Gerado automaticamente. Nao editar manualmente.
{_LOG_FORMAT}
map $http_x_forwarded_proto $nis2pme_proto {{
    default $scheme;
    "~.+"   $http_x_forwarded_proto;
}}

server {{
    listen 80;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml text/javascript;
    gzip_min_length 1000;

{_REAL_IP}
{_ACCESS_LOG}
{_SEC_HEADERS}

    location /api/ {{
        # Re-resolver o upstream em runtime (DNS embebido do Docker) — senão o
        # nginx cacheia o IP no arranque e parte se o backend mudar de IP.
        resolver 127.0.0.11 valid=10s ipv6=off;
        set $nis2pme_backend backend;
        proxy_pass         http://$nis2pme_backend:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $nis2pme_proto;
        proxy_read_timeout 120s;
        proxy_connect_timeout 10s;
        proxy_send_timeout 120s;
        client_max_body_size 15M;
    }}

    location / {{
        try_files $uri $uri/ /index.html;
    }}

    location ~ /\\. {{
        deny all;
        access_log off;
        log_not_found off;
    }}
}}
"""

# HTTPS — {{HSTS}} é substituído pela linha de HSTS (custom) ou por vazio (self-signed).
_NGINX_HTTPS_TPL = """\
# NIS2PME — Nginx config (HTTPS)
# Gerado automaticamente. Nao editar manualmente.
__LOG_FORMAT__
# Redirect HTTP -> HTTPS
server {
    listen 80;
    server_name _;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    ssl_certificate     /etc/nginx/certs/cert.pem;
    ssl_certificate_key /etc/nginx/certs/key.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256;
    ssl_prefer_server_ciphers off;
    ssl_session_timeout 1d;
    ssl_session_cache   shared:MozSSL:10m;

    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml text/javascript;
    gzip_min_length 1000;

__REAL_IP__
__ACCESS_LOG__
__HSTS__
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; frame-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'" always;

    location /api/ {
        # Re-resolver o upstream em runtime (DNS embebido do Docker) — senão o
        # nginx cacheia o IP no arranque e parte se o backend mudar de IP.
        resolver 127.0.0.11 valid=10s ipv6=off;
        set $nis2pme_backend backend;
        proxy_pass         http://$nis2pme_backend:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_connect_timeout 10s;
        proxy_send_timeout 120s;
        client_max_body_size 15M;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }

    location ~ /\\. {
        deny all;
        access_log off;
        log_not_found off;
    }
}
"""

_HSTS_LINE = '    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;'


def _config_https(com_hsts: bool) -> str:
    """Constrói a config HTTPS, com ou sem cabeçalho HSTS."""
    cfg = _NGINX_HTTPS_TPL.replace("__HSTS__", _HSTS_LINE if com_hsts else "")
    cfg = cfg.replace("__LOG_FORMAT__", _LOG_FORMAT)
    cfg = cfg.replace("__ACCESS_LOG__", _ACCESS_LOG)
    return cfg.replace("__REAL_IP__", _REAL_IP)


# ---------------------------------------------------------------------------
# Geração de certificado autoassinado
# ---------------------------------------------------------------------------

def _gerar_certificado_autoassinado(app_url: str) -> tuple[bytes, bytes]:
    """
    Gera um par certificado/chave RSA-2048 autoassinado, válido por 10 anos.
    Inclui SAN para o hostname/IP extraído de APP_URL, localhost e 127.0.0.1.
    Retorna (cert_pem, key_pem) como bytes.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    hostname = urlparse(app_url).hostname or "nis2pme.local"

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NIS2PME"),
    ])

    # SANs: hostname principal + localhost + 127.0.0.1
    san_entries: list = [x509.DNSName("localhost"), x509.IPAddress(IPv4Address("127.0.0.1"))]
    try:
        san_entries.insert(0, x509.IPAddress(IPv4Address(hostname)))
    except AddressValueError:
        san_entries.insert(0, x509.DNSName(hostname))

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


# ---------------------------------------------------------------------------
# Validação de certificado próprio
# ---------------------------------------------------------------------------

def _validar_certificado_proprio(cert_pem: str, key_pem: str) -> None:
    """
    Valida que cert_pem e key_pem são um par válido e não expirado.
    Lança HTTPException 422 em caso de erro.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    # Validar certificado
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.strip().encode())
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Certificado PEM inválido. Verifique que o ficheiro está no formato correcto (-----BEGIN CERTIFICATE-----).",
        )

    # Verificar expiração
    if cert.not_valid_after_utc < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Certificado expirado em {cert.not_valid_after_utc.strftime('%Y-%m-%d')}. Renove o certificado antes de continuar.",
        )

    # Validar chave privada
    try:
        key = load_pem_private_key(key_pem.strip().encode(), password=None)
    except TypeError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Chave privada protegida por password. Remova a password antes de fazer upload (openssl rsa -in key.pem -out key_sem_password.pem).",
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Chave privada PEM inválida. Verifique que o ficheiro está no formato correcto (-----BEGIN PRIVATE KEY----- ou -----BEGIN RSA PRIVATE KEY-----).",
        )

    # Verificar que cert e chave correspondem (comparar chave pública)
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    cert_pub = cert.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    key_pub = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    if cert_pub != key_pub:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="O certificado e a chave privada não correspondem. Certifique-se de que fazem parte do mesmo par.",
        )


# ---------------------------------------------------------------------------
# Inspeção do certificado atualmente instalado (para o estado no wizard)
# ---------------------------------------------------------------------------

def inspecionar_certificado_ativo() -> dict | None:
    """
    Lê o certificado instalado no volume e devolve metadados, ou None se não
    houver (ex.: modo proxy). `autoassinado` deriva do próprio certificado
    (issuer == subject), por isso a mensagem é sempre honesta.
    """
    cert_file = NGINX_CONFIG_DIR / "certs" / "cert.pem"
    if not cert_file.is_file():
        return None
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
        return {
            "autoassinado": cert.issuer == cert.subject,
            "expira_em": cert.not_valid_after_utc.date().isoformat(),
            "emissor": cert.issuer.rfc4514_string(),
        }
    except Exception:  # noqa: BLE001 — nunca partir o endpoint de estado
        return None


# ---------------------------------------------------------------------------
# Função principal
# ---------------------------------------------------------------------------

def configurar_nginx_https(
    modo: str,
    cert_pem: str | None,
    key_pem: str | None,
    app_url: str,
) -> dict:
    """
    Escreve o nginx.conf (e certificados se aplicável) no volume partilhado
    e sinaliza o container de nginx para recarregar a configuração.
    modo: 'none' | 'proxy' | 'self_signed' | 'custom'.
    """
    config_dir = NGINX_CONFIG_DIR
    certs_dir = config_dir / "certs"

    if not config_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Diretório de configuração nginx não encontrado. "
                "Verifique que o volume 'nis2pme_nginx' está montado correctamente no docker-compose."
            ),
        )

    if modo == "none":
        (config_dir / "nginx.conf").write_text(_NGINX_HTTP, encoding="utf-8")
        _sinalizar_reload(config_dir)
        return {"modo": "none", "aviso": None}

    elif modo == "proxy":
        (config_dir / "nginx.conf").write_text(_NGINX_PROXY, encoding="utf-8")
        _sinalizar_reload(config_dir)
        return {
            "modo": "proxy",
            "aviso": (
                "A app serve HTTP interno; o HTTPS é tratado pelo proxy/firewall a montante. "
                "Garanta que apenas o proxy alcança esta instância e que o salto proxy↔app é local."
            ),
        }

    elif modo == "self_signed":
        certs_dir.mkdir(parents=True, exist_ok=True)
        cert_bytes, key_bytes = _gerar_certificado_autoassinado(app_url)
        (certs_dir / "cert.pem").write_bytes(cert_bytes)
        (certs_dir / "key.pem").write_bytes(key_bytes)
        os.chmod(certs_dir / "key.pem", 0o600)
        # Self-signed NÃO envia HSTS — senão o aviso do browser torna-se
        # não-contornável e o utilizador fica trancado fora.
        (config_dir / "nginx.conf").write_text(_config_https(com_hsts=False), encoding="utf-8")
        _sinalizar_reload(config_dir)
        hostname = urlparse(app_url).hostname or "servidor"
        return {
            "modo": "self_signed",
            "aviso": (
                f"Certificado autoassinado gerado. O browser mostrará um aviso de segurança na 1.ª vez "
                f"— é esperado. Protege contra escuta passiva, mas não contra um atacante ativo na rede; "
                f"para proteção completa, carregue um certificado de confiança. Aceda via https://{hostname}"
            ),
        }

    elif modo == "custom":
        if not cert_pem or not key_pem:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="cert_pem e key_pem são obrigatórios para modo=custom.",
            )
        _validar_certificado_proprio(cert_pem, key_pem)
        certs_dir.mkdir(parents=True, exist_ok=True)
        (certs_dir / "cert.pem").write_text(cert_pem.strip(), encoding="utf-8")
        (certs_dir / "key.pem").write_text(key_pem.strip(), encoding="utf-8")
        os.chmod(certs_dir / "key.pem", 0o600)
        # Cert de confiança -> HSTS ativo.
        (config_dir / "nginx.conf").write_text(_config_https(com_hsts=True), encoding="utf-8")
        _sinalizar_reload(config_dir)
        hostname = urlparse(app_url).hostname or "servidor"
        return {
            "modo": "custom",
            "aviso": f"Certificado instalado com sucesso. Aceda agora via https://{hostname}",
        }

    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Modo HTTPS inválido: {modo}. Use 'none', 'proxy', 'self_signed' ou 'custom'.",
        )


# ---------------------------------------------------------------------------
# Aplicação inicial do TLS no arranque (a partir de TLS_MODE)
# ---------------------------------------------------------------------------

def aplicar_tls_inicial() -> None:
    """
    Configura o TLS do nginx no PRIMEIRO arranque, a partir de TLS_MODE.
    - Apenas on-prem (em saas o TLS é tratado a montante por Cloudflare).
    - Idempotente: usa um marcador; reinícios não sobrescrevem alterações do wizard.
    - Falha-suave: se algo correr mal, a app continua (configura-se no wizard).
    """
    from app.config import get_settings

    settings = get_settings()
    if settings.DEPLOYMENT_MODE != "onprem":
        return
    if _TLS_INIT_MARKER.exists():
        return

    modo = (getattr(settings, "TLS_MODE", "self-signed") or "self-signed").strip().lower()
    app_url = settings.APP_URL

    try:
        if modo == "proxy":
            configurar_nginx_https("proxy", None, None, app_url)
        elif modo == "custom":
            cert_path = Path(settings.TLS_CERT_PATH or "")
            key_path = Path(settings.TLS_KEY_PATH or "")
            if cert_path.is_file() and key_path.is_file():
                try:
                    configurar_nginx_https(
                        "custom",
                        cert_path.read_text(encoding="utf-8"),
                        key_path.read_text(encoding="utf-8"),
                        app_url,
                    )
                except HTTPException as exc:
                    logger.warning(
                        "TLS_MODE=custom mas certificado inválido (%s). Fallback para autoassinado.",
                        getattr(exc, "detail", exc),
                    )
                    configurar_nginx_https("self_signed", None, None, app_url)
            else:
                logger.warning(
                    "TLS_MODE=custom mas TLS_CERT_PATH/TLS_KEY_PATH não encontrados (%s, %s). "
                    "Fallback para autoassinado.",
                    cert_path, key_path,
                )
                configurar_nginx_https("self_signed", None, None, app_url)
        else:  # self-signed (default)
            configurar_nginx_https("self_signed", None, None, app_url)
    except Exception as exc:  # noqa: BLE001 — nunca impedir o arranque
        logger.error("Falha a aplicar TLS inicial (%s). A app arranca; configure no wizard.", exc)
        return

    try:
        _TLS_INIT_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _TLS_INIT_MARKER.write_text(modo + "\n", encoding="utf-8")
    except OSError:
        pass


def _sinalizar_reload(config_dir: Path) -> None:
    """Cria ficheiro .reload que o entrypoint do nginx container monitoriza."""
    try:
        (config_dir / ".reload").touch()
    except OSError as e:
        logger.warning("Não foi possível sinalizar reload do nginx: %s", e)
