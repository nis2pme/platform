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
from ipaddress import IPv4Address, ip_address
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

# Cabeçalhos de segurança comuns (sem HSTS — HSTS só no modo custom).
# Uma única fonte: em nginx, um add_header dentro de uma location CANCELA todos
# os herdados do server, por isso quem define add_header tem de repetir estes.
# Mantê-los aqui numa lista evita cópias que divergem em silêncio.
#
# REGRA DE FRONTEIRA: estes cabeçalhos entram nas locations que servem a SPA, e
# NUNCA ao nível do server. Quem responde por /api/ é o middleware do backend, e
# uma location sem add_header próprio herdaria os do server — os dois conjuntos
# chegavam ao cliente ao mesmo tempo, com X-Frame-Options e CSP em contradição
# (SAMEORIGIN vs DENY, 'self' vs 'none'). Perante cabeçalhos duplicados e
# divergentes os browsers não se comportam todos da mesma maneira, e há motores
# que ignoram o cabeçalho — a proteção desaparecia onde se julgava existir.
#
# Cada camada trata do que serve: o nginx da SPA, o backend da API.
_SEC_HEADERS_LINHAS = (
    'add_header X-Frame-Options "SAMEORIGIN" always;',
    'add_header X-Content-Type-Options "nosniff" always;',
    'add_header Referrer-Policy "strict-origin-when-cross-origin" always;',
    "add_header Content-Security-Policy \"default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
    "connect-src 'self'; frame-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self'; object-src 'none'\" always;",
)

_HSTS_TEXTO = 'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;'


def _sec_headers(indentacao: str = "    ") -> str:
    """Os cabeçalhos de segurança, com a indentação do bloco onde vão entrar."""
    return "\n".join(f"{indentacao}{linha}" for linha in _SEC_HEADERS_LINHAS)


# ---------------------------------------------------------------------------
# Templates nginx
# ---------------------------------------------------------------------------

# Versão do template: incrementar quando as configs geradas mudarem de layout.
# O carimbo vai no cabeçalho de cada config gerada; no arranque, uma config
# NOSSA com carimbo antigo é regenerada (preservando modo TLS e certificados).
#
# v5: os cabeçalhos de segurança saíram do nível server para as locations da
# SPA. Ao nível do server caíam por herança nas locations de /api/, e chegavam
# ao cliente juntamente com os do backend — dois X-Frame-Options e duas CSP em
# contradição.
# v6: a location dos backups perdeu a barra final, que a punha a discutir com a
# aplicação sobre a forma do caminho — cada uma a redirecionar para a outra.
# v7: `server_tokens off` (a versão do nginx deixa de ir em cada resposta) e
# `proxy_redirect` a repor o esquema real nas redireções vindas da aplicação,
# que saíam em `http://` e faziam o cliente descer a ligação a texto simples.
# v8: o modo proxy tinha ficado sem `server_tokens off` (só o HTTP e o HTTPS o
# tinham) e o HTTPS deixa de emitir session tickets — com a chave de tickets
# em memória e sem rotação, um ticket antigo permitia retomar sessões passadas.
# v9: o index.html passa a ir com `Cache-Control: no-cache`. Sem isso o browser
# podia reutilizar um index.html de uma versão anterior, a apontar para ficheiros
# que a atualização já tirou do servidor.
_CONFIG_STAMP = "(template v9)"


def _loc_assets(hsts: bool = False) -> str:
    """Location dos ficheiros estáticos com hash de conteúdo no nome (Vite).

    Duas razões para existir:
      - o nome muda sempre que o conteúdo muda, por isso o ficheiro pode ficar
        em cache indefinidamente; sem isto o browser revalida tudo a cada visita;
      - `try_files ... =404` impede que um pedido a um ficheiro inexistente caia
        no fallback da SPA e receba o index.html com estado 200 — o browser
        tentaria interpretar HTML como JavaScript e falharia de forma opaca.

    Repete os cabeçalhos de segurança de propósito: o add_header abaixo cancela
    os herdados do server (comportamento do nginx, não engano).
    """
    # Um único Cache-Control: a diretiva `expires` do nginx emitiria um segundo
    # cabeçalho com o mesmo nome — válido, mas confuso para quem ler a config.
    linhas = [
        "    location ~* \\.(js|css|woff2?|ttf|eot|svg|png|jpg|jpeg|gif|ico)$ {",
        "        try_files $uri =404;",
        "        access_log off;",
        '        add_header Cache-Control "public, max-age=31536000, immutable" always;',
    ]
    if hsts:
        linhas.append(f"        {_HSTS_TEXTO}")
    linhas.append(_sec_headers("        "))
    linhas.append("    }")
    return "\n".join(linhas)


# A versão exata do nginx no cabeçalho `Server` só serve a quem procura alvos
# para uma vulnerabilidade conhecida dessa versão. Não é segredo — descobre-se
# por outras vias — mas oferecê-la em cada resposta poupa trabalho ao lado
# errado. Fica ao nível do http (este ficheiro é incluído lá dentro), para valer
# em todos os server blocks sem ter de se repetir em cada um.
_SERVER_TOKENS = "server_tokens off;"


def _proxy_redirect(proto_var: str = "$scheme") -> str:
    """Repõe o esquema real nas redireções que a aplicação gera.

    A aplicação não sabe que o pedido chegou por TLS: quem termina o TLS é este
    nginx, e o processo de trás recebe uma ligação em claro. Quando ela gera uma
    redireção — por exemplo para normalizar a barra final de um caminho — escreve
    `http://`, e o cliente que a siga desce a ligação para texto simples antes de
    voltar a subir. Aqui reescreve-se o esquema com o da ligação real do cliente,
    que este bloco conhece.

    Corrige-se aqui, e não a mandar a aplicação confiar no `X-Forwarded-Proto`,
    porque essa confiança muda também a origem do IP do cliente — que alimenta o
    rate limiting e o registo de auditoria — e é uma decisão de postura, não uma
    correção de caminho. Com `$scheme` a valer "http", a reescrita é identidade e
    não faz nada.
    """
    return f"        proxy_redirect http:// {proto_var}://;"


def _loc_spa(hsts: bool = False) -> str:
    """Locations da SPA — e o sítio onde os cabeçalhos da SPA vivem.

    Qualquer rota desconhecida serve o index.html, porque o encaminhamento é
    feito no browser pelo router do Vue.

    O index.html tem uma location própria, onde acaba sempre (o `try_files` e o
    `index` redirecionam para ela por dentro): vai com `Cache-Control: no-cache`,
    para o browser confirmar a cada visita se há versão nova. Os outros ficheiros
    têm o hash no nome e ficam em cache; o index.html é o único que diz quais
    carregar, e um antigo em cache apontaria para ficheiros que já não existem.

    Os cabeçalhos de segurança são declarados aqui, e não ao nível do server,
    para não caírem por herança nas locations de /api/ — ver a nota em
    `_SEC_HEADERS_LINHAS`. A location do index.html define um add_header, logo
    repete-os.
    """
    linhas = [
        "    location / {",
        "        try_files $uri $uri/ /index.html;",
    ]
    if hsts:
        linhas.append(f"        {_HSTS_TEXTO}")
    linhas.append(_sec_headers("        "))
    linhas.append("    }")
    linhas.append("")
    linhas.append("    location = /index.html {")
    linhas.append('        add_header Cache-Control "no-cache" always;')
    if hsts:
        linhas.append(f"        {_HSTS_TEXTO}")
    linhas.append(_sec_headers("        "))
    linhas.append("    }")
    return "\n".join(linhas)


def _loc_dossie(proto_var: str = "$scheme") -> str:
    """Location dedicada ao dossiê de auditoria: gerar+descarregar um .nis2pme
    com evidências pode demorar bem mais do que os timeouts do /api/ geral.
    Sem barra final: apanha /api/dossie E /api/dossie/... ."""
    return f"""\
    location /api/dossie {{
        resolver 127.0.0.11 valid=10s ipv6=off;
        set $nis2pme_backend_dsr backend;
        proxy_pass         http://$nis2pme_backend_dsr:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto {proto_var};
{_proxy_redirect(proto_var)}
        proxy_read_timeout 1800s;
        proxy_connect_timeout 10s;
        proxy_send_timeout 1800s;
        client_max_body_size 256m;
        proxy_buffering off;
        # O parecer (até 256 MiB) passa à medida que chega, como os backups: com o
        # buffering, o nginx guardava-o inteiro no disco dele antes de o backend
        # poder recusar um pedido sem sessão.
        proxy_request_buffering off;
    }}"""


def _loc_backups(proto_var: str = "$scheme") -> str:
    """Location dedicada aos backups: importação de .nbk grandes e operações
    demoradas (inspecionar/restaurar decifram e aplicam dumps completos) —
    limites próprios, mais largos do que os do /api/ geral.

    Sem barra final, como a do dossiê: apanha /api/backups E /api/backups/... .
    Com barra final, um pedido a /api/backups entrava num ciclo de redireções —
    o nginx acrescentava a barra, a aplicação (que regista a rota sem ela)
    tirava-a outra vez, e o cliente ficava a saltar entre as duas para sempre.
    """
    return f"""\
    location /api/backups {{
        resolver 127.0.0.11 valid=10s ipv6=off;
        set $nis2pme_backend_bkp backend;
        proxy_pass         http://$nis2pme_backend_bkp:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto {proto_var};
{_proxy_redirect(proto_var)}
        proxy_read_timeout 1800s;
        proxy_connect_timeout 10s;
        proxy_send_timeout 1800s;
        client_max_body_size 4g;
        proxy_request_buffering off;
    }}"""


# HTTP simples (legado — modo "none"). Mantido por compatibilidade.
_NGINX_HTTP = f"""\
# NIS2PME — Nginx config (HTTP)
# Gerado automaticamente. Nao editar manualmente. {_CONFIG_STAMP}
{_SERVER_TOKENS}
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
{_proxy_redirect()}
        proxy_read_timeout 120s;
        proxy_connect_timeout 10s;
        proxy_send_timeout 120s;
        client_max_body_size 15M;
    }}

{_loc_backups()}

{_loc_dossie()}

{_loc_spa()}

    location ~ /\\. {{
        deny all;
        access_log off;
        log_not_found off;
    }}

{_loc_assets()}
}}
"""

# Modo proxy: TLS a montante. Serve HTTP, SEM redirect para HTTPS, e preserva o
# X-Forwarded-Proto do proxy (cai em $scheme se ausente) para os cookies Secure
# refletirem a ligação real do browser ao proxy.
# NOTA: assume que apenas o proxy de confiança alcança este edge (o backend não
# está exposto diretamente). Caso contrário um cliente poderia forjar o header.
_NGINX_PROXY = f"""\
# NIS2PME — Nginx config (proxy / TLS a montante)
# Gerado automaticamente. Nao editar manualmente. {_CONFIG_STAMP}
{_SERVER_TOKENS}
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
{_proxy_redirect("$nis2pme_proto")}
        proxy_read_timeout 120s;
        proxy_connect_timeout 10s;
        proxy_send_timeout 120s;
        client_max_body_size 15M;
    }}

{_loc_backups("$nis2pme_proto")}

{_loc_dossie("$nis2pme_proto")}

{_loc_spa()}

    location ~ /\\. {{
        deny all;
        access_log off;
        log_not_found off;
    }}

{_loc_assets()}
}}
"""

# HTTPS — {{HSTS}} é substituído pela linha de HSTS (custom) ou por vazio (self-signed).
_NGINX_HTTPS_TPL = """\
# NIS2PME — Nginx config (HTTPS)
# Gerado automaticamente. Nao editar manualmente. __STAMP__
__SERVER_TOKENS__
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
    ssl_session_tickets off;

    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml text/javascript;
    gzip_min_length 1000;

__REAL_IP__
__ACCESS_LOG__

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
        proxy_redirect http:// $scheme://;
        proxy_read_timeout 120s;
        proxy_connect_timeout 10s;
        proxy_send_timeout 120s;
        client_max_body_size 15M;
    }

__LOC_BACKUPS__

__LOC_DOSSIE__

__LOC_SPA__

    location ~ /\\. {
        deny all;
        access_log off;
        log_not_found off;
    }

__LOC_ASSETS__
}
"""


def _config_https(com_hsts: bool) -> str:
    """Constrói a config HTTPS, com ou sem cabeçalho HSTS.

    O HSTS acompanha os restantes cabeçalhos: entra nas locations da SPA, nunca
    ao nível do server. Nas respostas de /api/ quem o decide é o backend, que já
    o condiciona a ligação mesmo cifrada e a endereço de domínio — um HSTS numa
    instalação self-signed por IP trancaria o acesso ao primeiro aviso de
    certificado.
    """
    cfg = _NGINX_HTTPS_TPL.replace("__STAMP__", _CONFIG_STAMP)
    cfg = cfg.replace("__SERVER_TOKENS__", _SERVER_TOKENS)
    cfg = cfg.replace("__LOC_BACKUPS__", _loc_backups())
    cfg = cfg.replace("__LOC_DOSSIE__", _loc_dossie())
    cfg = cfg.replace("__LOC_SPA__", _loc_spa(hsts=com_hsts))
    # A location dos assets define add_header, logo perde os herdados — inclui
    # o HSTS pela mesma razão que os restantes cabeçalhos.
    cfg = cfg.replace("__LOC_ASSETS__", _loc_assets(hsts=com_hsts))
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

    # SANs: hostname principal + localhost + 127.0.0.1. O hostname pode ser um
    # nome, um IPv4 ou um IPv6 (o `APP_URL` traz o IPv6 sem os parênteses).
    san_entries: list = [x509.DNSName("localhost"), x509.IPAddress(IPv4Address("127.0.0.1"))]
    try:
        san_entries.insert(0, x509.IPAddress(ip_address(hostname)))
    except ValueError:
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

def _san_cobre_hostname(cert, hostname: str) -> bool:
    """Diz se o certificado é válido para o nome (ou IP) por onde a app é servida.

    Um certificado que não cobre o hostname do `APP_URL` instala-se sem erro e
    o browser recusa-o na primeira visita — e a mensagem "instalado com
    sucesso" já foi dada. Conferir aqui evita trancar o operador fora.
    """
    from cryptography import x509

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    try:
        ip = ip_address(hostname)
    except ValueError:
        ip = None
    if ip is not None:
        return ip in san.get_values_for_type(x509.IPAddress)
    alvo = hostname.lower().rstrip(".")
    for nome in san.get_values_for_type(x509.DNSName):
        nome = nome.lower().rstrip(".")
        if nome == alvo:
            return True
        if nome.startswith("*.") and "." in alvo and alvo.split(".", 1)[1] == nome[2:]:
            return True
    return False


def _validar_certificado_proprio(cert_pem: str, key_pem: str, hostname: str | None = None) -> list[str]:
    """
    Valida que cert_pem e key_pem são um par válido, não expirado e — quando se
    conhece o hostname — emitido para ele. Lança HTTPException 422 em caso de
    erro; devolve avisos não bloqueantes (ex.: cadeia intermédia em falta).
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

    if hostname and not _san_cobre_hostname(cert, hostname):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"O certificado não é válido para '{hostname}' (o nome de APP_URL): os nomes "
                "que cobre (SAN) não o incluem. O browser recusá-lo-ia. Use um certificado "
                "emitido para esse nome ou corrija o APP_URL."
            ),
        )

    avisos: list[str] = []
    # Um certificado emitido por uma CA vem quase sempre com uma intermédia; sem
    # ela no PEM (fullchain) alguns browsers e todos os clientes sem cache de
    # intermédias recusam a ligação. Não se recusa — há CAs que assinam direto
    # da raiz — mas diz-se.
    if cert.issuer != cert.subject and cert_pem.count("-----BEGIN CERTIFICATE-----") == 1:
        avisos.append(
            "O PEM traz só um certificado e não é autoassinado: se a CA usa uma "
            "intermédia, cole o fullchain (certificado + intermédias), senão alguns "
            "browsers vão recusar a ligação."
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
    return avisos


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
        hostname = urlparse(app_url).hostname or "servidor"
        avisos = _validar_certificado_proprio(cert_pem, key_pem, hostname=urlparse(app_url).hostname)
        certs_dir.mkdir(parents=True, exist_ok=True)
        (certs_dir / "cert.pem").write_text(cert_pem.strip(), encoding="utf-8")
        (certs_dir / "key.pem").write_text(key_pem.strip(), encoding="utf-8")
        os.chmod(certs_dir / "key.pem", 0o600)
        # Cert de confiança -> HSTS ativo.
        (config_dir / "nginx.conf").write_text(_config_https(com_hsts=True), encoding="utf-8")
        _sinalizar_reload(config_dir)
        return {
            "modo": "custom",
            "aviso": " ".join(
                [f"Certificado instalado com sucesso. Aceda agora via https://{hostname}", *avisos]
            ),
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
        # Já inicializado — mas se o TEMPLATE evoluiu desde que a config foi
        # gerada (carimbo antigo), regenerá-la preservando o modo em vigor.
        try:
            _atualizar_config_gerada()
        except Exception as exc:  # noqa: BLE001 — nunca impedir o arranque
            logger.warning("Atualização da config nginx gerada falhou: %s", exc)
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


def _atualizar_config_gerada() -> None:
    """Regenera uma nginx.conf gerada por uma versão anterior deste template
    (ex.: sem a location dos backups), preservando o modo TLS em vigor e os
    certificados. Configs que não sejam nossas nunca são tocadas."""
    conf = NGINX_CONFIG_DIR / "nginx.conf"
    if not conf.is_file():
        return
    atual = conf.read_text(encoding="utf-8", errors="replace")
    if "Gerado automaticamente" not in atual or _CONFIG_STAMP in atual:
        return
    # O modo em vigor deteta-se pelo cabeçalho da própria config (o marcador
    # de inicialização pode estar desatualizado se o wizard mudou o modo).
    if "Nginx config (HTTPS)" in atual:
        nova = _config_https(com_hsts="Strict-Transport-Security" in atual)
    elif "proxy / TLS a montante" in atual:
        nova = _NGINX_PROXY
    elif "Nginx config (HTTP)" in atual:
        nova = _NGINX_HTTP
    else:
        return
    conf.write_text(nova, encoding="utf-8")
    _sinalizar_reload(NGINX_CONFIG_DIR)
    logger.info("nginx.conf regenerada para o template atual %s.", _CONFIG_STAMP)


def _sinalizar_reload(config_dir: Path) -> None:
    """Cria ficheiro .reload que o entrypoint do nginx container monitoriza."""
    try:
        (config_dir / ".reload").touch()
    except OSError as e:
        logger.warning("Não foi possível sinalizar reload do nginx: %s", e)
