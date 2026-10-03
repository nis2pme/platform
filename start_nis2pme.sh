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

# Versão a instalar/atualizar: uma tag de release (ex.: NIS2PME_VERSION=v0.4.0) fixa
# o compose a essa versão e exige o checksum publicado ao lado; "main" é o que
# está publicado agora, sem checksum — o que sempre foi.
NIS2PME_VERSION="${NIS2PME_VERSION:-main}"
RAW_BASE="https://raw.githubusercontent.com/nis2pme/platform/${NIS2PME_VERSION}"
# O diretório de instalação é escolhido na secção 2: uma instalação existente é
# PROCURADA antes de se cair no default, porque o default depende de onde o
# comando foi lançado (ver o comentário lá em baixo).

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
# 2. Escolher e preparar o diretório de instalação
# ------------------------------------------------------------
# Uma instalação existente tem de ser ENCONTRADA, não adivinhada. O default
# ($(pwd)/nis2pme) depende de onde o comando é lançado, mas os containers têm
# nome fixo e o volume de dados é o mesmo: correr o script de outro sítio criava
# uma pasta nova, com .env novo e password de base de dados nova, por cima do
# MESMO volume. O PostgreSQL só aplica a password quando CRIA o cluster — num
# volume que já tem dados ignora-a — e a app ficava em crash-loop com
# "password authentication failed", com o container da base de dados a aparecer
# "healthy" (o pg_isready do healthcheck não autentica).
# Ordem de escolha:
#   1) NIS2PME_DIR, se o operador o definiu — a vontade dele manda;
#   2) a pasta que os containers existentes têm montada em /app/.env;
#   3) a pasta atual, se já for uma instalação (evita criar ./nis2pme/nis2pme);
#   4) o default de sempre.
_dir_montado_nos_containers() {
    docker inspect nis2pme_backend \
        --format '{{range .Mounts}}{{if eq .Destination "/app/.env"}}{{.Source}}{{end}}{{end}}' \
        2>/dev/null | head -1 || true
}

if [ -n "$NIS2PME_DIR" ]; then
    INSTALL_DIR="$NIS2PME_DIR"
else
    INSTALL_DIR="$(pwd)/nis2pme"
    _env_dos_containers="$(_dir_montado_nos_containers)"
    if [ -n "$_env_dos_containers" ] && [ -f "$_env_dos_containers" ]; then
        INSTALL_DIR="$(dirname "$_env_dos_containers")"
        echo "[nis2pme] $(t "Instalação existente encontrada (é a que os containers usam)." "Existing installation found (the one the containers use).")"
    elif [ -f "$(pwd)/.env" ] && [ -f "$(pwd)/docker-compose.yml" ]; then
        INSTALL_DIR="$(pwd)"
    fi
fi

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

# Travão de segurança: pasta sem .env mas com um volume de dados já criado nesta
# máquina. Gerar aqui um .env novo daria uma password de base de dados nova que o
# PostgreSQL nunca chega a aplicar (o cluster já existe) — a app não voltaria a
# arrancar. Nesse caso é preciso decidir, não adivinhar.
if [ "$EXISTING_INSTALL" = 0 ]; then
    _vol_dados=$(docker volume ls -q 2>/dev/null | grep -E '(^|_)nis2pme_pgdata$' | head -1 || true)
    if [ -n "$_vol_dados" ] && [ "${NIS2PME_FORCE_NEW:-0}" != "1" ]; then
        echo ""
        echo "=============================================="
        echo "  $(t "ERRO: já existe uma base de dados NIS2PME nesta máquina" "ERROR: a NIS2PME database already exists on this machine")"
        echo "        $(t "volume" "volume"): $_vol_dados"
        echo ""
        echo "  $(t "Esta pasta não tem .env, por isso seria gerada uma password nova —" "This folder has no .env, so a new password would be generated —")"
        echo "  $(t "mas a password da base de dados só se define quando ela é criada." "but the database password is only set when the database is created.")"
        echo "  $(t "A app ficaria com 'password authentication failed'." "The app would end up with 'password authentication failed'.")"
        echo ""
        echo "  $(t "Escolha uma destas:" "Pick one of these:")"
        echo "   1) $(t "Atualizar a instalação existente — corra o script a partir da pasta dela" "Update the existing installation — run the script from its folder")"
        echo "      $(t "ou indique-a:" "or point at it:")  NIS2PME_DIR=/caminho/para/nis2pme sh start_nis2pme.sh"
        echo "      $(t "Para a encontrar:" "To find it:")  find / -name .env -path '*nis2pme*' 2>/dev/null"
        echo "   2) $(t "Instalar mesmo de raiz aqui, reaproveitando essa base de dados:" "Install from scratch here, reusing that database:")"
        echo "      NIS2PME_FORCE_NEW=1 sh start_nis2pme.sh"
        echo "      $(t "(a seguir será preciso alinhar a password — o script explica como)" "(you will then have to align the password — the script explains how)")"
        echo "=============================================="
        exit 1
    fi
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

_fetch_compose() {
    # Descarrega o compose para $1 e, numa versão fixada, confere-o contra o
    # checksum publicado ao lado (docker-compose.yml.sha256). Um ficheiro
    # trocado a meio do caminho não chega a correr.
    _fetch "docker-compose.yml" "$1"
    if [ "$NIS2PME_VERSION" != "main" ]; then
        _fetch "docker-compose.yml.sha256" "$1.sha256" || {
            echo "[nis2pme] $(t "ERRO: a versão $NIS2PME_VERSION não publica checksum do compose." "ERROR: version $NIS2PME_VERSION does not publish a compose checksum.")"
            exit 1
        }
        esperado=$(awk '{print $1}' "$1.sha256")
        obtido=$(sha256sum "$1" | awk '{print $1}')
        rm -f "$1.sha256"
        if [ "$esperado" != "$obtido" ]; then
            echo "[nis2pme] $(t "ERRO: o docker-compose.yml descarregado não corresponde ao checksum publicado." "ERROR: the downloaded docker-compose.yml does not match the published checksum.")"
            rm -f "$1"
            exit 1
        fi
    fi
}

if [ "$EXISTING_INSTALL" = 0 ]; then
    # Instalação nova: descarregar o compose se ainda não existir.
    if [ ! -f docker-compose.yml ]; then
        echo "[nis2pme] $(t "A descarregar docker-compose.yml..." "Downloading docker-compose.yml...")"
        _fetch_compose "docker-compose.yml"
    fi
else
    # Atualização: refrescar o compose para a versão publicada, senão uma versão nova que
    # acrescente serviços/variáveis obrigatórias correria contra a topologia antiga. Só
    # substitui se houver diferenças e guarda sempre um backup .bak antes (os end-users
    # configuram via .env, não via compose; o .bak cobre a edição manual rara).
    echo "[nis2pme] $(t "A verificar atualizações ao docker-compose.yml..." "Checking for docker-compose.yml updates...")"
    _fetch_compose "docker-compose.yml.new"
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
# 3b. Sanidade do .env (falhas silenciosas que só aparecem no arranque)
# ------------------------------------------------------------
# Lê uma variável do .env. Tira o \r, os espaços à volta e as aspas que o
# formato usa quando o valor tem espaços — mas nunca mexe no interior do valor.
# Vazio se a variável não existir.
_ler_env() {
    grep -m1 "^$1=" .env 2>/dev/null | cut -d'=' -f2- | tr -d '\r' \
        | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
              -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/" || true
}

# CRLF: um .env editado no Windows leva um \r no fim de cada valor. A password da
# base de dados passa a ter um byte a mais e o PostgreSQL recusa a ligação, sem
# que nada aponte para o ficheiro. Normalizar (com cópia de segurança).
# A deteção é por comparação de bytes: há ambientes onde o `grep` trata o ficheiro
# como texto e descarta o \r, dando um falso "está limpo".
if ! tr -d '\r' < .env | cmp -s - .env; then
    cp .env .env.bak-crlf
    chmod 600 .env.bak-crlf 2>/dev/null || true   # tem a password da BD lá dentro
    tr -d '\r' < .env.bak-crlf > .env
    chown 10001:10001 .env 2>/dev/null || true
    chmod 600 .env 2>/dev/null || true
    echo "[nis2pme] $(t "AVISO: o .env tinha fins-de-linha do Windows — corrigido (cópia em .env.bak-crlf)." "WARNING: .env had Windows line endings — fixed (copy at .env.bak-crlf).")"
fi

DB_PW=$(_ler_env DB_PASSWORD)
DB_UTILIZADOR=$(_ler_env DB_USER); [ -n "$DB_UTILIZADOR" ] || DB_UTILIZADOR=nis2pme
DB_NOME=$(_ler_env DB_NAME);       [ -n "$DB_NOME" ]       || DB_NOME=nis2pme

if [ -z "$DB_PW" ]; then
    echo "[nis2pme] $(t "ERRO: DB_PASSWORD está vazio ou em falta no .env — a app não conseguiria ligar-se à base de dados." "ERROR: DB_PASSWORD is empty or missing from .env — the app could not connect to the database.")"
    echo "          $(t "Edite" "Edit") ${INSTALL_DIR}/.env"
    exit 1
fi

# O Docker Compose expande ${...} nos valores do .env: um '$' na password faz com
# que o valor que chega ao PostgreSQL não seja o que está escrito no ficheiro.
case "$DB_PW" in
    *'$'*)
        echo "[nis2pme] $(t "AVISO: DB_PASSWORD contém '\$' — o Compose expande-o e a password efetiva fica diferente." "WARNING: DB_PASSWORD contains '\$' — Compose expands it and the effective password differs.")"
        ;;
esac

# Fixar o nome do projeto Compose. Por defeito é o nome da pasta: mudar a pasta de
# sítio trocaria o prefixo dos volumes e a app arrancaria com dados vazios. Fixa-se
# o nome que JÁ está em uso (lido do container), nunca um nome novo.
if ! grep -q '^COMPOSE_PROJECT_NAME=' .env 2>/dev/null; then
    _projeto=$(docker inspect nis2pme_db --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)
    [ -n "$_projeto" ] || _projeto=$(basename "$INSTALL_DIR")
    if [ "$_projeto" = "nis2pme" ]; then
        printf '\n# Nome do projeto Docker Compose (fixado para que mover a pasta não troque os volumes)\nCOMPOSE_PROJECT_NAME=%s\n' "$_projeto" >> .env
        echo "[nis2pme] $(t "Nome do projeto Compose fixado em" "Compose project name pinned to"): $_projeto"
    fi
fi

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

# Numa atualização, um backup ANTES de mexer nas imagens: se a versão nova
# correr mal, há um ponto para onde voltar. Sem passphrase de backups (o
# operador nunca os ativou) avisa-se e pergunta-se; em pipe segue.
# As versões anteriores aos backups (0.3.x) não têm o módulo no contentor que
# está a correr — nesse caso tira-se um dump da base de dados, que é o que as
# migrações da versão nova vão alterar.
_continuar_sem_backup() {
    if [ -t 0 ]; then
        printf "  %s" "$(t "Continuar sem backup? [s/N]: " "Continue without a backup? [y/N]: ")"
        read -r _cont
        case "$_cont" in
            [SsYy]*) ;;
            *) echo "[nis2pme] $(t "Atualização cancelada." "Update cancelled.")"; exit 1 ;;
        esac
    fi
}

if [ "$EXISTING_INSTALL" = 1 ] && docker compose ps --status running backend > /dev/null 2>&1; then
    echo "[nis2pme] $(t "A criar um backup antes de atualizar..." "Creating a backup before updating...")"
    if ! docker compose exec -T backend python -c "import app.backup.criar" > /dev/null 2>&1 < /dev/null; then
        echo "[nis2pme] $(t "A versão instalada é anterior aos backups automáticos — a guardar um dump da base de dados." "The installed version predates automatic backups — saving a database dump.")"
        _pasta_dump="${INSTALL_DIR}/backups-pre-atualizacao"
        _ficheiro_dump="${_pasta_dump}/${DB_NOME}-$(date +%Y%m%d-%H%M%S).dump"
        ( umask 077; mkdir -p "$_pasta_dump" )
        if ( umask 077; docker exec nis2pme_db pg_dump -U "$DB_UTILIZADOR" -d "$DB_NOME" -Fc > "$_ficheiro_dump" ) 2>/dev/null && [ -s "$_ficheiro_dump" ]; then
            echo "[nis2pme] $(t "Dump da base de dados guardado em" "Database dump saved at"): $_ficheiro_dump"
            echo "          $(t "(só a base de dados: uploads e segredos ficam nos volumes, que a atualização não toca)" "(database only: uploads and secrets stay in the volumes, which the update does not touch)")"
        else
            rm -f "$_ficheiro_dump"
            echo "[nis2pme] $(t "AVISO: não foi possível guardar o dump da base de dados." "WARNING: the database dump could not be saved.")"
            _continuar_sem_backup
        fi
    elif docker compose exec -T backend python -m app.backup.criar < /dev/null; then
        echo "[nis2pme] $(t "Backup criado." "Backup created.")"
    else
        _rc=$?
        if [ "$_rc" = 2 ]; then
            echo "[nis2pme] $(t "AVISO: os backups não estão ativados (sem passphrase) — a atualizar sem backup." "WARNING: backups are not enabled (no passphrase) — updating without a backup.")"
        else
            echo "[nis2pme] $(t "AVISO: o backup falhou." "WARNING: the backup failed.")"
        fi
        _continuar_sem_backup
    fi
fi

# Espaço em disco antes de puxar imagens: um disco cheio a meio do pull deixa a
# instalação pior do que estava (e o PostgreSQL pára de aceitar escritas).
_raiz_docker=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
[ -d "$_raiz_docker" ] || _raiz_docker="."
_kb_livres=$(df -P "$_raiz_docker" 2>/dev/null | awk 'NR==2 {print $4}' || true)
case "$_kb_livres" in ''|*[!0-9]*) _kb_livres="" ;; esac
if [ -n "$_kb_livres" ] && [ "$_kb_livres" -lt 3145728 ]; then
    echo "[nis2pme] $(t "AVISO: menos de 3 GB livres em" "WARNING: less than 3 GB free on") $_raiz_docker ($((_kb_livres / 1024)) MB)."
    if [ -t 0 ]; then
        printf "  %s" "$(t "Continuar mesmo assim? [s/N]: " "Continue anyway? [y/N]: ")"
        read -r _esp
        case "$_esp" in
            [SsYy]*) ;;
            *) echo "[nis2pme] $(t "Cancelado." "Cancelled.")"; exit 1 ;;
        esac
    fi
fi

echo "[nis2pme] $(t "A descarregar imagens do GHCR e a arrancar..." "Pulling images from GHCR and starting...")"
docker compose pull
docker compose up -d

# ------------------------------------------------------------
# 5. Provar a password da base de dados contra o cluster real
# ------------------------------------------------------------
# O PostgreSQL só aplica POSTGRES_PASSWORD quando cria o cluster. Se o .env
# trouxer outra password, o backend entra em crash-loop e o container da base de
# dados aparece na mesma como "healthy" (o pg_isready do healthcheck não
# autentica). Provar a password aqui transforma isso numa mensagem clara — e num
# arranjo de uma tecla.
_password_bd_confere() {
    # A password vai por stdin (nunca na linha de comandos, que é legível no `ps`).
    # O teste é por TCP para dentro do próprio container: é a mesma regra do
    # pg_hba ("host ... scram-sha-256") que o backend apanha.
    printf '%s' "$DB_PW" | docker exec -i nis2pme_db sh -c \
        'PGPASSWORD=$(cat) psql -h 127.0.0.1 -U "$0" -d "$1" -c "SELECT 1"' \
        "$DB_UTILIZADOR" "$DB_NOME" > /dev/null 2>&1
}

_esperar_bd() {
    _i=0
    while [ "$_i" -lt 30 ]; do
        docker exec nis2pme_db pg_isready -U "$DB_UTILIZADOR" -d "$DB_NOME" > /dev/null 2>&1 && return 0
        _i=$((_i + 1))
        sleep 2
    done
    return 1
}

if docker inspect nis2pme_db > /dev/null 2>&1 && _esperar_bd; then
    if ! _password_bd_confere; then
        echo ""
        echo "=============================================="
        echo "  $(t "A password da base de dados no .env não corresponde à base de dados." "The database password in .env does not match the database.")"
        echo ""
        echo "  $(t "A password é gravada quando a base de dados é criada; alterá-la no" "The password is stored when the database is created; changing it in")"
        echo "  $(t ".env depois disso não a muda. Isto costuma acontecer quando o .env" ".env afterwards does not change it. This usually happens when .env")"
        echo "  $(t "foi gerado de novo (script corrido noutra pasta) ou substituído." "was regenerated (script run from another folder) or replaced.")"
        echo ""
        echo "  $(t "O melhor arranjo é recuperar o .env original:" "The best fix is to recover the original .env:")"
        echo "    find / -name .env -path '*nis2pme*' 2>/dev/null"
        echo "  $(t "e copiar de lá a linha DB_PASSWORD=" "and copy the DB_PASSWORD= line from it")"
        echo ""
        echo "  $(t "Em alternativa, alinha-se a base de dados com o .env atual. Os dados" "Alternatively, align the database with the current .env. The data")"
        echo "  $(t "não são afetados: esta password não cifra nada (as chaves de cifra" "is not affected: this password encrypts nothing (the encryption keys")"
        echo "  $(t "vivem noutro volume)." "live in a different volume).")"
        echo "=============================================="
        _alinhar=0
        if [ -t 0 ]; then
            printf "  %s" "$(t "Alinhar agora a base de dados com o .env? [s/N]: " "Align the database with .env now? [y/N]: ")"
            read -r _resp
            case "$_resp" in [SsYy]*) _alinhar=1 ;; esac
        fi
        if [ "$_alinhar" = 1 ]; then
            # ALTER USER pela socket local (trust no pg_hba), com a instrução por
            # stdin. Aspas simples na password são duplicadas para não partir o SQL.
            _pw_sql=$(printf '%s' "$DB_PW" | sed "s/'/''/g")
            if printf "ALTER USER \"%s\" WITH PASSWORD '%s';\n" "$DB_UTILIZADOR" "$_pw_sql" \
                | docker exec -i nis2pme_db psql -q -U "$DB_UTILIZADOR" -d "$DB_NOME" -f - > /dev/null 2>&1 \
                && _password_bd_confere; then
                echo "[nis2pme] $(t "Password alinhada. A reiniciar o backend..." "Password aligned. Restarting the backend...")"
                docker compose up -d backend > /dev/null
            else
                echo "[nis2pme] $(t "ERRO: não foi possível alterar a password da base de dados." "ERROR: could not change the database password.")"
                exit 1
            fi
        else
            echo "[nis2pme] $(t "A app não vai conseguir arrancar enquanto isto não for resolvido." "The app will not start until this is resolved.")"
            exit 1
        fi
    fi
fi

# ------------------------------------------------------------
# 6. Confirmar que o backend ficou mesmo de pé
# ------------------------------------------------------------
# Dizer "está a arrancar" e sair deixava o operador a descobrir sozinho um
# crash-loop. Espera-se pelo healthcheck e, se falhar, mostram-se as últimas
# linhas do registo — que é o que qualquer diagnóstico vai pedir a seguir.
echo "[nis2pme] $(t "A aguardar que o backend fique operacional..." "Waiting for the backend to become healthy...")"
echo "          $(t "(na primeira instalação as migrações podem levar alguns minutos)" "(on a first install the migrations can take a few minutes)")"
_saudavel=0
_i=0
while [ "$_i" -lt 60 ]; do
    _estado=$(docker inspect nis2pme_backend --format '{{.State.Health.Status}}' 2>/dev/null || true)
    [ "$_estado" = "healthy" ] && { _saudavel=1; break; }
    _i=$((_i + 1))
    sleep 5
done

if [ "$_saudavel" = 1 ]; then
    # Em que revisão da base ficou a instalação: é a forma barata de confirmar
    # que as migrações correram.
    _rev=$(docker compose exec -T backend alembic current 2>/dev/null < /dev/null | tail -1)
    [ -n "$_rev" ] && echo "[nis2pme] $(t "Revisão da base de dados:" "Database revision:") $_rev"
else
    echo ""
    echo "=============================================="
    echo "  $(t "AVISO: o backend ainda não está operacional ao fim de 5 minutos." "WARNING: the backend is still not healthy after 5 minutes.")"
    echo "  $(t "Últimas linhas do registo:" "Last log lines:")"
    echo "=============================================="
    docker compose logs --tail 30 backend 2>/dev/null || true
    echo "=============================================="
    echo "  $(t "Registo completo:" "Full log:")  docker compose logs -f backend     ($(t "dentro de" "inside") ${INSTALL_DIR})"
    echo "=============================================="
fi

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
