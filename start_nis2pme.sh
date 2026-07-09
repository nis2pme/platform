#!/bin/sh
# ============================================================
# NIS2PME — Instalador / arranque (edição GHCR)
#
# Descarrega as imagens pré-construídas do GitHub Container Registry,
# gera um .env (IP do servidor + password da BD + modo TLS) e arranca
# a stack com Docker Compose. Sem build.
#
# Uso mais simples (descarrega e corre de uma vez):
#   curl -fsSL https://raw.githubusercontent.com/nis2pme/platform/main/start_nis2pme.sh | bash
#
# Uso mais seguro (inspecionar primeiro):
#   curl -fsSL .../start_nis2pme.sh -o start_nis2pme.sh
#   less start_nis2pme.sh
#   sh start_nis2pme.sh
#
# Re-executar é seguro: um .env existente é mantido intacto.
# ============================================================
set -e

RAW_BASE="https://raw.githubusercontent.com/nis2pme/platform/main"
INSTALL_DIR="${NIS2PME_DIR:-$(pwd)/nis2pme}"

# ------------------------------------------------------------
# 0. Idioma / Language
# ------------------------------------------------------------
LANG_SEL="pt"
if [ -t 0 ]; then
    printf "Idioma / Language:  [1] Português   [2] English   (1): "
    read -r _lang
    [ "$_lang" = "2" ] && LANG_SEL="en"
fi

# t "texto-pt" "text-en"  -> imprime conforme o idioma escolhido
t() {
    if [ "$LANG_SEL" = "en" ]; then printf '%s' "$2"; else printf '%s' "$1"; fi
}

# ------------------------------------------------------------
# 1. Verificar Docker Engine
# ------------------------------------------------------------
_check_docker() {
    if command -v docker > /dev/null 2>&1 && docker info > /dev/null 2>&1; then
        if docker compose version > /dev/null 2>&1; then
            return 0
        fi
        echo "$(t "ERRO: Docker instalado mas falta o plugin Compose v2." "ERROR: Docker is installed but the Compose v2 plugin is missing.")"
        echo "$(t "      Instale 'docker-compose-plugin' para a sua distribuição." "      Install 'docker-compose-plugin' for your distribution.")"
        exit 1
    fi

    echo ""
    echo "=============================================="
    echo "$(t "  ERRO: Docker Engine não encontrado" "  ERROR: Docker Engine not found")"
    echo "$(t "  O NIS2PME requer Docker Engine 20.10+ com Compose v2" "  NIS2PME requires Docker Engine 20.10+ with Compose v2")"
    echo "=============================================="
    echo ""

    DISTRO_ID=""
    [ -f /etc/os-release ] && DISTRO_ID=$(. /etc/os-release && echo "$ID")

    case "$DISTRO_ID" in
        ubuntu|debian|raspbian)
            echo "  $(t "Instalar Docker (Ubuntu/Debian):" "Install Docker (Ubuntu/Debian):")"
            echo "    curl -fsSL https://get.docker.com | sh"
            echo "    sudo usermod -aG docker \$USER && newgrp docker"
            ;;
        rhel|centos|rocky|almalinux|ol|fedora)
            echo "  $(t "Instalar Docker (família RHEL/Fedora):" "Install Docker (RHEL/Fedora family):")"
            echo "    sudo dnf -y install dnf-plugins-core"
            echo "    sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo"
            echo "    sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin"
            echo "    sudo systemctl enable --now docker"
            echo "    sudo usermod -aG docker \$USER && newgrp docker"
            ;;
        *)
            echo "  $(t "Ver o guia oficial:" "See the official guide:") https://docs.docker.com/engine/install/"
            echo "    curl -fsSL https://get.docker.com | sh"
            ;;
    esac
    echo ""
    echo "  $(t "(Depois de instalar, corra este script novamente.)" "(After installing, run this script again.)")"
    echo "=============================================="
    exit 1
}

_check_docker

# ------------------------------------------------------------
# 2. Preparar o diretório de instalação
# ------------------------------------------------------------
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
echo "[nis2pme] $(t "Diretório de instalação" "Install directory"): $INSTALL_DIR"

# Deteção de instalação existente: o .env vive na pasta de instalação e só é gerado na
# primeira execução. A sua presença distingue uma atualização de uma instalação nova.
EXISTING_INSTALL=0
[ -f .env ] && EXISTING_INSTALL=1
if [ "$EXISTING_INSTALL" = 1 ]; then
    echo ""
    echo "[nis2pme] $(t "Instalação existente detetada — modo atualização." "Existing installation detected — update mode.")"
    echo "          $(t ".env, base de dados, uploads e secrets são preservados." ".env, database, uploads and secrets are preserved.")"
fi

_fetch() {
    # $1 = caminho remoto sob RAW_BASE, $2 = destino local
    if command -v curl > /dev/null 2>&1; then
        curl -fsSL "$RAW_BASE/$1" -o "$2"
    elif command -v wget > /dev/null 2>&1; then
        wget -qO "$2" "$RAW_BASE/$1"
    else
        echo "$(t "ERRO: nem curl nem wget disponíveis para descarregar" "ERROR: neither curl nor wget is available to download") $1"
        exit 1
    fi
}

if [ "$EXISTING_INSTALL" = 0 ]; then
    # Instalação nova: descarregar o compose se ainda não existir.
    if [ ! -f docker-compose.yml ]; then
        echo "[nis2pme] $(t "A descarregar docker-compose.yml..." "Downloading docker-compose.yml...")"
        _fetch "docker-compose.yml" "docker-compose.yml"
    fi
else
    # Atualização: refrescar o compose para a versão publicada, senão uma versão nova que
    # acrescente serviços/variáveis obrigatórias correria contra a topologia antiga. Só
    # substitui se houver diferenças e guarda sempre um backup .bak antes (os end-users
    # configuram via .env, não via compose; o .bak cobre a edição manual rara).
    echo "[nis2pme] $(t "A verificar atualizações ao docker-compose.yml..." "Checking for docker-compose.yml updates...")"
    _fetch "docker-compose.yml" "docker-compose.yml.new"
    if [ ! -f docker-compose.yml ]; then
        mv docker-compose.yml.new docker-compose.yml
    elif cmp -s docker-compose.yml docker-compose.yml.new; then
        rm -f docker-compose.yml.new
    else
        cp docker-compose.yml docker-compose.yml.bak
        mv docker-compose.yml.new docker-compose.yml
        echo "[nis2pme] $(t "docker-compose.yml atualizado (backup em docker-compose.yml.bak)." "docker-compose.yml updated (backup at docker-compose.yml.bak).")"
    fi
fi

# ------------------------------------------------------------
# 3. Criar .env na primeira execução (IP + password BD + TLS)
# ------------------------------------------------------------
if [ ! -f .env ]; then
    echo "[nis2pme] $(t "Primeira execução — a gerar configuração..." "First run — generating configuration...")"

    # Password aleatória da BD (128 bits)
    if command -v openssl > /dev/null 2>&1; then
        DB_PASSWORD=$(openssl rand -hex 16)
    else
        DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_hex(16))")
    fi

    # Detetar o IP principal do servidor
    DETECTED_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1); exit}')
    [ -z "$DETECTED_IP" ] && DETECTED_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -z "$DETECTED_IP" ] && DETECTED_IP="localhost"

    # --- Menu TLS (segurança da ligação) ---
    TLS_MODE="self-signed"
    TLS_CERT_LINE=""
    TLS_KEY_LINE=""
    APP_URL="https://${DETECTED_IP}"

    if [ -t 0 ]; then
        echo ""
        echo "$(t "Segurança da ligação (HTTPS):" "Connection security (HTTPS):")"
        echo "  1) $(t "Já tenho um certificado SSL" "I already have an SSL certificate")"
        echo "  2) $(t "Acedo através de proxy/firewall que já faz HTTPS (Cloudflare, Traefik, Nginx…)" "I access through a proxy/firewall that already does HTTPS (Cloudflare, Traefik, Nginx…)")"
        echo "  3) $(t "Não tenho — gerar certificado temporário (o browser avisa na 1.ª vez; é normal)" "I don't have one — generate a temporary certificate (browser warns on first visit; this is normal)")"
        printf "%s [1/2/3] (3): " "$(t "Opção" "Option")"
        read -r _tls

        case "$_tls" in
            1)
                TLS_MODE="custom"
                while :; do
                    printf "  %s: " "$(t "Caminho do certificado (.crt/.pem)" "Certificate path (.crt/.pem)")"
                    read -r _cert
                    printf "  %s: " "$(t "Caminho da chave privada (.key/.pem)" "Private key path (.key/.pem)")"
                    read -r _key
                    if [ -r "$_cert" ] && [ -r "$_key" ] \
                        && grep -q "BEGIN CERTIFICATE" "$_cert" 2>/dev/null \
                        && grep -q "PRIVATE KEY" "$_key" 2>/dev/null; then
                        mkdir -p certs
                        cp "$_cert" certs/cert.pem
                        cp "$_key" certs/key.pem
                        chmod 600 certs/key.pem 2>/dev/null || true
                        TLS_CERT_LINE="TLS_CERT_PATH=/app/host_certs/cert.pem"
                        TLS_KEY_LINE="TLS_KEY_PATH=/app/host_certs/key.pem"
                        break
                    fi
                    echo "  $(t "Ficheiro inválido ou ilegível. Tente novamente (ou Ctrl+C para sair)." "Invalid or unreadable file. Try again (or Ctrl+C to abort).")"
                done
                ;;
            2)
                TLS_MODE="proxy"
                printf "  %s: " "$(t "Endereço público (ex: https://nis2pme.empresa.pt) [Enter = https://${DETECTED_IP}]" "Public address (e.g. https://nis2pme.company.com) [Enter = https://${DETECTED_IP}]")"
                read -r _pub
                [ -n "$_pub" ] && APP_URL="$_pub"
                ;;
            *)
                TLS_MODE="self-signed"
                ;;
        esac
    fi

    # Garantir que ./certs existe para o bind-mount (mesmo vazio nos modos sem cert)
    mkdir -p certs

    cat > .env << ENVEOF
# NIS2PME — configuração gerada automaticamente ($(date))
#
# APP_URL: endereço que os utilizadores abrem no browser.
#   Para aplicar uma alteração:  docker compose down  &&  docker compose up -d
APP_URL=${APP_URL}

# Password da base de dados (gerada automaticamente — não alterar após instalação)
DB_PASSWORD=${DB_PASSWORD}

# Modo TLS: self-signed | proxy | custom
TLS_MODE=${TLS_MODE}
${TLS_CERT_LINE}
${TLS_KEY_LINE}
ENVEOF

    echo "[nis2pme] $(t "Criado .env" "Created .env")"
    echo "          APP_URL = ${APP_URL}   (TLS_MODE=${TLS_MODE})"
    echo ""
    echo "  $(t "⚠  Confirme que APP_URL está correto antes de os utilizadores acederem." "⚠  Verify APP_URL is correct before users connect.")"
    echo "     $(t "Edite" "Edit") ${INSTALL_DIR}/.env"
    echo ""
    if [ -t 0 ]; then
        printf "  %s" "$(t "Prima Enter para continuar, ou Ctrl+C para editar o .env primeiro: " "Press Enter to continue, or Ctrl+C to edit .env first: ")"
        read -r _
    fi
fi

# Garantir ./certs em re-execuções também (o bind-mount do compose precisa dele)
mkdir -p certs

# O .env é montado dentro do container backend (uid 10001, sem DAC_OVERRIDE — cap_drop:
# [ALL] no compose). Se o dono/permissões não corresponderem a esse uid (ex.: ficheiro
# criado por root com chmod 600), a app não consegue ler nem escrever /app/.env
# ("Permission denied"). Alinhar sempre, mesmo em re-execuções, para auto-corrigir
# instalações existentes.
chown 10001:10001 .env 2>/dev/null || true
chmod 600 .env 2>/dev/null || true

# ------------------------------------------------------------
# 4. Descarregar imagens e arrancar
# ------------------------------------------------------------
# Numa atualização (instalação existente) e em modo interativo, confirmar antes de puxar
# imagens novas e recriar containers. Em pipe (curl|bash) procede sem bloquear.
if [ "$EXISTING_INSTALL" = 1 ] && [ -t 0 ]; then
    printf "  %s" "$(t "Atualizar para as imagens mais recentes? [S/n]: " "Update to the latest images? [Y/n]: ")"
    read -r _upd
    case "$_upd" in
        [Nn]*)
            echo "[nis2pme] $(t "Atualização cancelada. Nada foi alterado nos containers." "Update cancelled. Containers were left unchanged.")"
            exit 0
            ;;
    esac
fi

echo "[nis2pme] $(t "A descarregar imagens do GHCR e a arrancar..." "Pulling images from GHCR and starting...")"
docker compose pull
docker compose up -d

APP_URL_VAL=$(grep '^APP_URL=' .env | cut -d'=' -f2- | tr -d '"'"'" | tr -d ' ')
TLS_MODE_VAL=$(grep '^TLS_MODE=' .env | cut -d'=' -f2- | tr -d ' ')

echo ""
echo "=============================================="
if [ "$EXISTING_INSTALL" = 1 ]; then
    echo "  $(t "O NIS2PME foi atualizado e está a reiniciar." "NIS2PME has been updated and is restarting.")"
    echo ""
    echo "  $(t "Abrir" "Open"): ${APP_URL_VAL:-https://localhost}"
    echo "  $(t "As migrações de base de dados correm automaticamente no arranque." "Database migrations run automatically on startup.")"
else
    echo "  $(t "O NIS2PME está a arrancar." "NIS2PME is starting.")"
    echo ""
    echo "  $(t "Abrir" "Open"): ${APP_URL_VAL:-https://localhost}"
    echo "  $(t "A primeira visita abre o assistente de configuração." "The first visit opens the setup wizard.")"
fi
if [ "$TLS_MODE_VAL" = "self-signed" ]; then
    echo ""
    echo "  $(t "Nota: usa um certificado temporário — o browser mostra um aviso na 1.ª" "Note: it uses a temporary certificate — the browser shows a warning on the")"
    echo "  $(t "vez. É normal: clique em 'Avançado' → 'Prosseguir'." "first visit. This is normal: click 'Advanced' → 'Proceed'.")"
fi
echo ""
echo "  $(t "Registos" "Logs"):  docker compose logs -f     ($(t "dentro de" "inside") ${INSTALL_DIR})"
echo "  $(t "Parar" "Stop"):     docker compose down"
echo "=============================================="
