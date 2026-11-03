#!/bin/sh
# ============================================================
# NIS2PME — agente de atualização (corre no anfitrião, como root)
#
# Acionado por uma unidade systemd (.path) quando o backend deixa um pedido em
# <instalação>/atualizacao/pedido/pedido.txt. Não escuta em nenhuma porta e não
# executa nada que não venha assinado:
#   1. valida o pedido (formato estrito; tudo o que vem do backend é hostil);
#   2. verifica a assinatura Ed25519 do manifesto com a chave de /etc/nis2pme;
#   3. descarrega o compose, o aplicador e este agente e confere o SHA-256 de
#      cada um com o manifesto assinado;
#   4. entrega a atualização ao aplicador da versão nova.
# O manifesto traz o canal (`canal=`, por omissão stable) e tem de ser o desta instalação.
#
# Subcomandos (todos locais, para root):
#   (sem argumentos)         processa o pedido, se houver
#   --estado FASE [CODIGO]   regista o progresso (usado pelo aplicador)
#   --registar               marca o agente como presente
#   --fixar-versao X.Y.Z     sobe o piso de versões (nunca desce)
#   --definir-versao X.Y.Z   define o piso à força (o instalador, depois de uma atualização
#                            ou de uma reversão feitas pelo administrador como root)
#   --desinstalar            retira as unidades systemd e a configuração
# ============================================================
set -u
umask 077

AGENTE_VERSAO="2"
CONF="${NIS2PME_AGENTE_CONF:-/etc/nis2pme/agente.conf}"
AGENTE_CAMINHO="$0"

log() { printf '[nis2pme-agente] %s\n' "$*" >&2; }
agora() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# ---------------------------------------------------------------- configuração
# O conf é a única fonte de confiança: o .env da instalação é escrito pelo
# backend e por isso nada de segurança se lê dele.
carregar_conf() {
    [ -f "$CONF" ] && [ ! -L "$CONF" ] || { log "sem configuração em $CONF"; return 1; }
    _dono=$(stat -c '%u' "$CONF" 2>/dev/null) || return 1
    _modo=$(stat -c '%a' "$CONF" 2>/dev/null) || return 1
    if [ "$_dono" != "$(id -u)" ]; then
        log "a configuração tem de pertencer a quem corre o agente"; return 1
    fi
    # Escrita para grupo ou outros recusa-se: alguém trocaria a chave de confiança.
    _o=${_modo#"${_modo%?}"}
    _resto=${_modo%?}
    _g=${_resto#"${_resto%?}"}
    case "$_g$_o" in
        *[2367]*) log "a configuração não pode ser escrevível por outros"; return 1 ;;
    esac
    INSTALL_DIR=""; PROJECT="nis2pme"; PUBKEY=""; CANAL="stable"
    BASE_URL="https://raw.githubusercontent.com/nis2pme/platform"
    COMPOSE_FILES="docker-compose.yml"; STATE_DIR="/var/lib/nis2pme-agente"
    # Serviço de licenças do fornecedor (pedidos premium) e a CA dele, gravada pelo
    # instalador a partir do compose do núcleo.
    LICENCAS_URL="https://license.nis2pme.pt"; CA_FORNECEDOR="/etc/nis2pme/fornecedor-ca.pem"
    # Lê-se chave a chave, nunca como código: um valor com `;` ou `$(...)` não corre.
    while IFS= read -r _l || [ -n "$_l" ]; do
        _l=$(printf '%s' "$_l" | tr -d '\r')
        case "$_l" in ""|"#"*) continue ;; esac
        _k=${_l%%=*}; _v=${_l#*=}
        printf '%s' "$_v" | grep -Eq '^[A-Za-z0-9_./:-]*$' || { log "valor inválido em $_k"; return 1; }
        case "$_k" in
            INSTALL_DIR) INSTALL_DIR=$_v ;;
            PROJECT) PROJECT=$_v ;;
            PUBKEY) PUBKEY=$_v ;;
            CANAL) CANAL=$_v ;;
            BASE_URL) BASE_URL=$_v ;;
            COMPOSE_FILES) COMPOSE_FILES=$_v ;;
            STATE_DIR) STATE_DIR=$_v ;;
            LICENCAS_URL) LICENCAS_URL=$_v ;;
            CA_FORNECEDOR) CA_FORNECEDOR=$_v ;;
        esac
    done < "$CONF"
    [ -n "$INSTALL_DIR" ] && [ -d "$INSTALL_DIR" ] || { log "INSTALL_DIR inválido"; return 1; }
    # Os caminhos entram em argumentos sem aspas: nada de espaços nem metacarateres.
    case "$INSTALL_DIR$COMPOSE_FILES$STATE_DIR$CA_FORNECEDOR" in
        *[!A-Za-z0-9_./:-]*) log "caminhos com carateres não suportados"; return 1 ;;
    esac
    case "$LICENCAS_URL" in https://*|http://127.0.0.1*|http://localhost*) ;; *) log "LICENCAS_URL inválido"; return 1 ;; esac
    case "$PROJECT" in ""|*[!a-z0-9_-]*) log "PROJECT inválido"; return 1 ;; esac
    # O canal desta instalação (clientes: stable; instalações de desenvolvimento: dev).
    case "$CANAL" in stable|dev) ;; *) log "CANAL inválido (stable ou dev)"; return 1 ;; esac
    ESTADO_DIR="$INSTALL_DIR/atualizacao/estado"
    PEDIDO_DIR="$INSTALL_DIR/atualizacao/pedido"
    mkdir -p "$STATE_DIR" 2>/dev/null || true
    return 0
}

# ------------------------------------------------------------------ estado
escrever_ficheiro() {
    # $1 destino, conteúdo no stdin. Atómico: o backend nunca lê meio ficheiro.
    _tmp="$1.tmp.$$"
    cat > "$_tmp" && chmod 644 "$_tmp" && mv -f "$_tmp" "$1"
}

registar_presenca() {
    mkdir -p "$ESTADO_DIR" 2>/dev/null || return 0
    # A versão premium instalada e se o núcleo a usa: o backend só a lê para mostrar
    # e para perguntar por atualizações (a verdade fica aqui, na pasta do root).
    _pv=$(ler_piso_premium)
    versao_valida "$_pv" || _pv=""
    _pa=0
    [ -f "$STATE_DIR/premium_ativo" ] && _pa=1
    printf 'agente_versao=%s\nregistado=%s\npremium_versao=%s\npremium_ativo=%s\n' \
        "$AGENTE_VERSAO" "$(agora)" "$_pv" "$_pa" \
        | escrever_ficheiro "$ESTADO_DIR/agente.txt"
}

# PEDIDO_ID, VERSAO_ALVO, VERSAO_ORIGEM e BACKUP_REF vêm do ambiente.
registar_estado() {
    _fase="$1"; _codigo="${2:-}"
    mkdir -p "$ESTADO_DIR" 2>/dev/null || return 0
    {
        printf 'pedido_id=%s\n' "${PEDIDO_ID:-}"
        printf 'fase=%s\n' "$_fase"
        printf 'versao_alvo=%s\n' "${VERSAO_ALVO:-}"
        printf 'versao_origem=%s\n' "${VERSAO_ORIGEM:-}"
        printf 'codigo=%s\n' "$_codigo"
        printf 'backup=%s\n' "$(printf '%s' "${BACKUP_REF:-}" | tr -cd 'A-Za-z0-9._-')"
        printf 'atualizado=%s\n' "$(agora)"
    } | escrever_ficheiro "$ESTADO_DIR/estado.txt"
}

falhar() {
    registar_estado falhou "$1"
    log "falhou: $1"
    [ -z "${TRAB:-}" ] || rm -rf "$TRAB"
    exit 1
}

# ------------------------------------------------------------------ versões
# Só trios numéricos X.Y.Z: é o que o manifesto assinado pode conter.
versao_valida() { printf '%s' "$1" | grep -Eq '^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$'; }

# 0 se $1 > $2
versao_maior() {
    _a1=${1%%.*}; _r=${1#*.}; _a2=${_r%%.*}; _a3=${_r#*.}
    _b1=${2%%.*}; _r=${2#*.}; _b2=${_r%%.*}; _b3=${_r#*.}
    [ "$_a1" -gt "$_b1" ] && return 0; [ "$_a1" -lt "$_b1" ] && return 1
    [ "$_a2" -gt "$_b2" ] && return 0; [ "$_a2" -lt "$_b2" ] && return 1
    [ "$_a3" -gt "$_b3" ]
}

ler_piso() { cat "$STATE_DIR/ultima_versao" 2>/dev/null | head -n1 | tr -d '\r\n '; }

fixar_piso() {
    versao_valida "$1" || return 1
    _atual=$(ler_piso)
    if [ -n "$_atual" ] && versao_valida "$_atual" && ! versao_maior "$1" "$_atual"; then
        return 0
    fi
    printf '%s\n' "$1" | escrever_ficheiro "$STATE_DIR/ultima_versao"
}

# ------------------------------------------------------------ criptografia
base64url_decode() {
    # $1 texto base64url (sem padding) -> bytes no stdout
    _s=$(printf '%s' "$1" | tr '_-' '/+')
    case $(( ${#_s} % 4 )) in 2) _s="$_s==" ;; 3) _s="$_s=" ;; 1) return 1 ;; esac
    printf '%s' "$_s" | base64 -d 2>/dev/null
}

# 0 se a assinatura ($2, base64url) de ficheiro ($1) é válida para PUBKEY.
verificar_assinatura() {
    _d=$(mktemp -d "$STATE_DIR/sig.XXXXXX") || return 1
    _ok=1
    base64url_decode "$PUBKEY" > "$_d/raw" 2>/dev/null
    base64url_decode "$2" > "$_d/sig" 2>/dev/null
    if [ "$(wc -c < "$_d/raw")" -eq 32 ] && [ "$(wc -c < "$_d/sig")" -eq 64 ]; then
        # SubjectPublicKeyInfo do Ed25519: cabeçalho DER fixo + os 32 bytes.
        { printf '\060\052\060\005\006\003\053\145\160\003\041\000'; cat "$_d/raw"; } > "$_d/pub.der"
        if openssl pkeyutl -verify -pubin -inkey "$_d/pub.der" -keyform DER -rawin \
                -in "$1" -sigfile "$_d/sig" > /dev/null 2>&1; then
            _ok=0
        elif openssl pkeyutl -verify -pubin -inkey "$_d/pub.der" -keyform DER \
                -in "$1" -sigfile "$_d/sig" > /dev/null 2>&1; then
            # OpenSSL 1.1.1 não tem -rawin: o Ed25519 verifica-se sem ele.
            _ok=0
        fi
    fi
    rm -rf "$_d"
    return $_ok
}

sha256_de() {
    if command -v sha256sum > /dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

# ------------------------------------------------------------ manifesto
campo() { sed -n "s/^$1=\\(.*\\)\$/\\1/p" | head -n1 | tr -d '\r'; }

# Valor não vazio, com no máximo $2 caracteres, todos da classe $3. O comprimento
# confere-se à parte: o grep do busybox recusa repetições acima de {255}.
so_caracteres() {
    [ -n "$1" ] && [ "${#1}" -le "$2" ] || return 1
    printf '%s' "$1" | grep -Eq "^[$3]+\$"
}

# --------------------------------------------------------- ficheiros do compose
# Com o premium ativo (marca na pasta de estado, que é do root: a unidade não
# escreve no /etc), os overlays premium juntam-se aos do agente.conf.
OVERLAYS_PREMIUM="docker-compose.premium.yml docker-compose.onprem.yml"

com_overlays_premium() {
    _cf="$COMPOSE_FILES"
    for _o in $OVERLAYS_PREMIUM; do
        case ":$_cf:" in *":$_o:"*) ;; *) _cf="$_cf:$_o" ;; esac
    done
    printf '%s' "$_cf"
}

compose_efetivo() {
    if [ -f "$STATE_DIR/premium_ativo" ]; then com_overlays_premium; else printf '%s' "$COMPOSE_FILES"; fi
}

# docker compose com a lista dada ($1, separada por `:`) e o resto dos argumentos.
compose_com() {
    _lista="$1"; shift
    _args=""
    _ant=$IFS; IFS=:
    for _f in $_lista; do _args="$_args -f $INSTALL_DIR/$_f"; done
    IFS=$_ant
    # shellcheck disable=SC2086
    docker compose -p "$PROJECT" $_args "$@"
}

# ------------------------------------------------------------- descarga
descarregar() {
    # $1 nome no servidor, $2 destino, $3 sha256 esperado
    _url="$BASE_URL/v$VERSAO_ALVO/$1"
    case "$BASE_URL" in
        https://*) _proto="=https" ;;
        *) _proto="=https,http" ;;
    esac
    if command -v curl > /dev/null 2>&1; then
        curl -fsSL --proto "$_proto" --max-time 180 --max-filesize 4194304 -o "$2" "$_url" || return 2
    elif command -v wget > /dev/null 2>&1; then
        wget -q -T 180 -O "$2" "$_url" || return 2
    else
        return 2
    fi
    [ "$(sha256_de "$2")" = "$3" ] || return 3
}

# ------------------------------------------------------------- pedido
processar_pedido() {
    _pedido="$PEDIDO_DIR/pedido.txt"
    if [ ! -f "$_pedido" ] || [ -L "$_pedido" ]; then
        # Nada, ou algo que não é um ficheiro normal (ligação simbólica, pasta): retira-se,
        # senão a unidade voltava a acordar para sempre por causa dele.
        if [ -e "$_pedido" ] || [ -L "$_pedido" ]; then
            rm -rf "$_pedido"
            log "pedido que não era um ficheiro normal: retirado"
        fi
        registar_presenca
        return 0
    fi
    _bruto=$(head -c 16384 "$_pedido" 2>/dev/null)
    rm -f "$_pedido"

    PEDIDO_ID=$(printf '%s\n' "$_bruto" | campo pedido_id)
    VERSAO_ALVO=""
    VERSAO_ORIGEM=""
    BACKUP_REF=""
    export PEDIDO_ID VERSAO_ALVO VERSAO_ORIGEM BACKUP_REF
    printf '%s' "$PEDIDO_ID" | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' \
        || { PEDIDO_ID=""; falhar pedido_invalido; }
    # O que o backend pede: sem `acao`, a atualização de sempre.
    case "$(printf '%s\n' "$_bruto" | campo acao)" in
        "") processar_atualizacao ;;
        instalar_premium) processar_instalar_premium ;;
        ativar_premium) processar_ativar_premium ;;
        atualizar_premium) processar_atualizar_premium ;;
        acordar_premium) processar_acordar_premium ;;
        *) falhar pedido_invalido ;;
    esac
}

processar_atualizacao() {
    VERSAO_ALVO=$(printf '%s\n' "$_bruto" | campo versao)
    versao_valida "$VERSAO_ALVO" || { VERSAO_ALVO=""; falhar pedido_invalido; }
    registar_estado pedido_recebido

    _sem_backup=$(printf '%s\n' "$_bruto" | campo sem_backup)
    _assinatura=$(printf '%s\n' "$_bruto" | campo assinatura)
    _manifesto_b64=$(printf '%s\n' "$_bruto" | campo manifesto_b64)
    case "$_sem_backup" in 0|1) ;; *) falhar pedido_invalido ;; esac
    printf '%s' "$_assinatura" | grep -Eq '^[A-Za-z0-9_-]{86}$' || falhar pedido_invalido
    so_caracteres "$_manifesto_b64" 8192 'A-Za-z0-9+/=' || falhar pedido_invalido

    registar_estado a_verificar
    TRAB="$STATE_DIR/trabalho/$PEDIDO_ID"
    rm -rf "$TRAB"; mkdir -p "$TRAB" || falhar interno
    printf '%s' "$_manifesto_b64" | base64 -d > "$TRAB/manifesto.txt" 2>/dev/null || falhar pedido_invalido
    verificar_assinatura "$TRAB/manifesto.txt" "$_assinatura" || falhar assinatura_invalida

    # O manifesto já é de confiança; mesmo assim lê-se com o mesmo rigor.
    [ "$(head -n1 "$TRAB/manifesto.txt" | tr -d '\r')" = "nis2pme-manifesto 1" ] || falhar pedido_invalido
    _m_versao=$(campo versao < "$TRAB/manifesto.txt")
    _m_canal=$(campo canal < "$TRAB/manifesto.txt")
    # A raiz assina também os manifestos premium: um deles nunca serve de atualização
    # do núcleo, mesmo com a assinatura válida.
    [ "$(campo componente < "$TRAB/manifesto.txt")" = "" ] \
        || [ "$(campo componente < "$TRAB/manifesto.txt")" = "nucleo" ] || falhar pedido_invalido
    _m_min=$(campo min_origem < "$TRAB/manifesto.txt")
    _m_premium_min=$(campo premium_min < "$TRAB/manifesto.txt")
    _m_compose=$(campo compose_sha256 < "$TRAB/manifesto.txt")
    _m_script=$(campo script_sha256 < "$TRAB/manifesto.txt")
    _m_agente=$(campo agente_sha256 < "$TRAB/manifesto.txt")
    IMAGEM_BACKEND=$(campo imagem_backend < "$TRAB/manifesto.txt")
    IMAGEM_FRONTEND=$(campo imagem_frontend < "$TRAB/manifesto.txt")
    [ "$_m_versao" = "$VERSAO_ALVO" ] || falhar pedido_invalido
    # Um manifesto só serve ao canal para que foi assinado (sem `canal=`, é o estável):
    # um pacote de desenvolvimento nunca se aplica numa instalação de clientes, nem
    # o inverso, mesmo com assinatura válida.
    [ "${_m_canal:-stable}" = "$CANAL" ] || falhar canal_invalido
    versao_valida "$_m_min" || falhar pedido_invalido
    [ -z "$_m_premium_min" ] || versao_valida "$_m_premium_min" || falhar pedido_invalido
    for _h in "$_m_compose" "$_m_script" "$_m_agente"; do
        printf '%s' "$_h" | grep -Eq '^[0-9a-f]{64}$' || falhar pedido_invalido
    done
    for _i in "$IMAGEM_BACKEND" "$IMAGEM_FRONTEND"; do
        [ -z "$_i" ] || printf '%s' "$_i" | grep -Eq '^sha256:[0-9a-f]{64}$' || falhar pedido_invalido
    done

    # Anti-reversão: o piso é a maior versão que o agente já aplicou (ou que o
    # instalador registou). Nada se pergunta ao backend: é ele o que se desconfia.
    _piso=$(ler_piso)
    versao_valida "$_piso" || { log "sem piso de versões (agente nunca inicializado)"; falhar interno; }
    VERSAO_ORIGEM="$_piso"; export VERSAO_ORIGEM
    versao_maior "$VERSAO_ALVO" "$VERSAO_ORIGEM" || falhar versao_nao_superior
    if versao_maior "$_m_min" "$VERSAO_ORIGEM"; then falhar origem_antiga; fi
    # Um núcleo novo serve o premium da versão anterior; quando deixa de servir um
    # premium antigo, di-lo no manifesto e atualiza-se primeiro o premium.
    _pv=$(ler_piso_premium)
    if [ -n "$_m_premium_min" ] && versao_valida "$_pv" && versao_maior "$_m_premium_min" "$_pv"; then
        falhar premium_incompativel
    fi

    registar_estado a_descarregar
    descarregar docker-compose.yml "$TRAB/docker-compose.yml" "$_m_compose"
    _r=$?; [ $_r -eq 0 ] || { [ $_r -eq 3 ] && falhar hash_invalido; falhar descarga_falhou; }
    descarregar agente/aplicar-atualizacao.sh "$TRAB/aplicar-atualizacao.sh" "$_m_script"
    _r=$?; [ $_r -eq 0 ] || { [ $_r -eq 3 ] && falhar hash_invalido; falhar descarga_falhou; }
    descarregar agente/nis2pme-agente.sh "$TRAB/nis2pme-agente.sh" "$_m_agente"
    _r=$?; [ $_r -eq 0 ] || { [ $_r -eq 3 ] && falhar hash_invalido; falhar descarga_falhou; }

    registar_estado a_preparar
    env -i PATH="$PATH" HOME="${HOME:-/root}" LANG=C \
        INSTALL_DIR="$INSTALL_DIR" PROJECT="$PROJECT" COMPOSE_FILES="$(compose_efetivo)" \
        VERSAO="$VERSAO_ALVO" VERSAO_ORIGEM="$VERSAO_ORIGEM" PEDIDO_ID="$PEDIDO_ID" \
        SEM_BACKUP="$_sem_backup" NOVO_COMPOSE="$TRAB/docker-compose.yml" \
        AGENTE="$AGENTE_CAMINHO" STATE_DIR="$STATE_DIR" \
        IMAGEM_BACKEND="$IMAGEM_BACKEND" IMAGEM_FRONTEND="$IMAGEM_FRONTEND" \
        MIN_LIVRE_KB="${MIN_LIVRE_KB:-3145728}" ESPERA_MAX="${ESPERA_MAX:-120}" ESPERA_PASSO="${ESPERA_PASSO:-5}" \
        NIS2PME_AGENTE_CONF="$CONF" \
        sh "$TRAB/aplicar-atualizacao.sh"
    _rc=$?

    _fase_final=$(sed -n 's/^fase=\(.*\)$/\1/p' "$ESTADO_DIR/estado.txt" 2>/dev/null | head -n1)
    if [ "$_rc" -eq 0 ] && [ "$_fase_final" = "concluido" ]; then
        fixar_piso "$VERSAO_ALVO"
        # O agente novo, já conferido contra o manifesto, substitui este.
        if [ "$(sha256_de "$TRAB/nis2pme-agente.sh")" != "$(sha256_de "$AGENTE_CAMINHO")" ]; then
            _novo="$AGENTE_CAMINHO.novo"
            cp "$TRAB/nis2pme-agente.sh" "$_novo" && chmod 755 "$_novo" && mv -f "$_novo" "$AGENTE_CAMINHO" \
                || log "não foi possível atualizar o próprio agente (a versão anterior continua)"
        fi
    elif [ "$_fase_final" = "revertido" ]; then
        # A anterior foi reposta: o piso não sobe.
        :
    elif [ "$_fase_final" != "falhou" ]; then
        registar_estado falhou interno
    fi
    rm -rf "$TRAB"
    registar_presenca
    return "$_rc"
}

# ------------------------------------------------------------ premium
# Os ficheiros que o pacote premium pode trazer (o resto recusa-se).
FICHEIROS_PACOTE="imagem.tar docker-compose.premium.yml docker-compose.onprem.yml premium-db/papeis-premium-data.sh mtls/gen-certs.sh gen-secrets.sh premium.env"
# Os que ficam na pasta da instalação, e as chaves do .env que o pacote pode trazer.
FICHEIROS_INSTALADOS="docker-compose.premium.yml docker-compose.onprem.yml premium-db/papeis-premium-data.sh mtls/gen-certs.sh gen-secrets.sh"
CHAVES_ENV_PREMIUM="PREMIUM_ENVELOPE_PUBKEY PREMIUM_ENVELOPE_KID NIS2PME_TLS_CA_PEM"
# O uid com que o sidecar corre: só ele lê o pedido (que leva o segredo).
UID_SIDECAR="10001"

ler_piso_premium() { cat "$STATE_DIR/ultima_versao_premium" 2>/dev/null | head -n1 | tr -d '\r\n '; }

# POST ao serviço de licenças. $1 caminho, $2 corpo JSON, $3 destino; imprime o código HTTP.
servico_post() {
    case "$LICENCAS_URL" in https://*) _proto="=https" ;; *) _proto="=https,http" ;; esac
    _ca=""
    [ -f "$CA_FORNECEDOR" ] && _ca="--cacert $CA_FORNECEDOR"
    # shellcheck disable=SC2086
    curl -sS --proto "$_proto" $_ca --max-time 60 --max-filesize 65536 \
        -H 'Content-Type: application/json' -d "$2" -o "$3" -w '%{http_code}' "$LICENCAS_URL$1" 2>/dev/null
}

# Memória disponível e espaço no disco do Docker antes de descarregar.
conferir_recursos() {
    _min_mem="${PREMIUM_MIN_MEM_KB:-819200}"
    _disp=$(sed -n 's/^MemAvailable: *\([0-9]*\) kB$/\1/p' /proc/meminfo 2>/dev/null | head -n1)
    if [ -n "$_disp" ] && [ "$_disp" -lt "$_min_mem" ]; then falhar sem_memoria; fi
    _raiz=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
    [ -d "$_raiz" ] || _raiz="."
    _livres=$(df -P "$_raiz" 2>/dev/null | awk 'NR==2 {print $4}')
    case "$_livres" in ''|*[!0-9]*) _livres="" ;; esac
    if [ -n "$_livres" ] && [ "$_livres" -lt "${PREMIUM_MIN_LIVRE_KB:-2097152}" ]; then falhar sem_espaco; fi
}

# Grava CHAVE=VALOR no .env da instalação, por cima do ficheiro (mantém dono e modo).
definir_env() {
    _env="$INSTALL_DIR/.env"
    [ -f "$_env" ] || return 1
    _tmp="$STATE_DIR/env.$$"
    grep -v "^$1=" "$_env" > "$_tmp" || true
    printf '%s=%s\n' "$1" "$2" >> "$_tmp"
    cat "$_tmp" > "$_env"
    rm -f "$_tmp"
}

esperar_backend_saudavel() {
    _i=0
    while [ "$_i" -lt "${ESPERA_MAX:-120}" ]; do
        _cid=$(compose_com "$1" ps -q backend < /dev/null 2>/dev/null | head -n1)
        if [ -n "$_cid" ] && [ "$(docker inspect --format '{{.State.Health.Status}}' "$_cid" 2>/dev/null)" = "healthy" ]; then
            return 0
        fi
        _i=$((_i + 1))
        sleep "${ESPERA_PASSO:-5}"
    done
    return 1
}

# Pronto = o próprio sidecar diz que serve com os módulos locais (`premium-sidecar
# pronto`: 0 pronto, 3 a arrancar, 4 sem os módulos — migração falhada). A correr não
# chega. Falha-se logo com prova de falha (sem os módulos, ou três reinícios do mesmo
# contentor); a arrancar, espera-se até ao teto, porque uma migração longa é normal.
# Um binário anterior, sem o comando, conta como pronto quando fica estável: o mesmo
# contentor a correr, sem reinícios, em quatro sondagens seguidas. Lê-se o `Status`,
# não o `Running`: a reiniciar, o Docker dá Running=true e a contagem fica parada
# entre tentativas.
esperar_premium_pronto() {
    _i=0; _seguidas=0; _anterior=""; _cid_base=""; _base=0
    while [ "$_i" -lt "${ESPERA_MAX:-120}" ]; do
        _cid=$(compose_com "$1" ps -q premium < /dev/null 2>/dev/null | head -n1)
        _agora=""
        [ -z "$_cid" ] || _agora=$(docker inspect --format '{{.State.Status}} {{.RestartCount}}' "$_cid" 2>/dev/null)
        _reinicios=${_agora#* }
        case "$_reinicios" in
            ''|*[!0-9]*) ;;
            *)
                if [ "$_cid" != "$_cid_base" ]; then _cid_base=$_cid; _base=$_reinicios; fi
                [ $((_reinicios - _base)) -lt 3 ] || return 1 ;;
        esac
        if [ "${_agora%% *}" = "running" ]; then
            compose_com "$1" exec -T premium premium-sidecar pronto < /dev/null > /dev/null 2>&1
            case $? in
                0) return 0 ;;
                4) return 1 ;;
                3) _seguidas=0; _anterior="" ;;
                *)
                    if [ "$_cid $_agora" = "$_anterior" ]; then _seguidas=$((_seguidas + 1)); else _seguidas=1; fi
                    _anterior="$_cid $_agora"
                    [ "$_seguidas" -lt 4 ] || return 0 ;;
            esac
        else
            _seguidas=0; _anterior=""
        fi
        _i=$((_i + 1))
        sleep "${ESPERA_PASSO:-5}"
    done
    return 1
}

# A última migração aplicada à base premium, ou vazio se não se consegue saber.
migracoes_premium() {
    compose_com "$1" exec -T premium-data-db psql -U premium_data -d nis2pme_premium_data -tAc \
        'select coalesce(max(version), 0) from _sqlx_migrations' < /dev/null 2>/dev/null | tr -d '\r\n '
}

# Confere o manifesto premium que o serviço devolveu e deixa-o em $TRAB/manifesto.txt.
# $1 manifesto em base64, $2 assinatura, $3 ficheiro indicado, $4 token, $5 versão do
# núcleo instalada. Define VERSAO_ALVO, _m_pacote, _m_sha, _m_imagem e _m_imagem_id.
conferir_manifesto_premium() {
    so_caracteres "$1" 8192 'A-Za-z0-9+/=' || falhar pedido_invalido
    printf '%s' "$2" | grep -Eq '^[A-Za-z0-9_-]{86}$' || falhar pedido_invalido
    printf '%s' "$4" | grep -Eq '^[A-Za-z0-9_-]{43}$' || falhar pedido_invalido
    printf '%s' "$1" | base64 -d > "$TRAB/manifesto.txt" 2>/dev/null || falhar pedido_invalido
    verificar_assinatura "$TRAB/manifesto.txt" "$2" || falhar assinatura_invalida

    [ "$(head -n1 "$TRAB/manifesto.txt" | tr -d '\r')" = "nis2pme-manifesto 1" ] || falhar pedido_invalido
    [ "$(campo componente < "$TRAB/manifesto.txt")" = "premium" ] || falhar pedido_invalido
    _m_canal=$(campo canal < "$TRAB/manifesto.txt")
    [ "${_m_canal:-stable}" = "$CANAL" ] || falhar canal_invalido
    VERSAO_ALVO=$(campo versao < "$TRAB/manifesto.txt"); export VERSAO_ALVO
    _m_pacote=$(campo pacote < "$TRAB/manifesto.txt")
    _m_sha=$(campo pacote_sha256 < "$TRAB/manifesto.txt")
    _m_imagem=$(campo imagem < "$TRAB/manifesto.txt")
    _m_imagem_id=$(campo imagem_id < "$TRAB/manifesto.txt")
    _m_nucleo_min=$(campo nucleo_min < "$TRAB/manifesto.txt")
    versao_valida "$VERSAO_ALVO" || { VERSAO_ALVO=""; falhar pedido_invalido; }
    versao_valida "$_m_nucleo_min" || falhar pedido_invalido
    [ "$_m_pacote" = "premium-onprem-$VERSAO_ALVO.tar.gz" ] && [ "$3" = "$_m_pacote" ] || falhar pedido_invalido
    printf '%s' "$_m_sha" | grep -Eq '^[0-9a-f]{64}$' || falhar pedido_invalido
    printf '%s' "$_m_imagem_id" | grep -Eq '^sha256:[0-9a-f]{64}$' || falhar pedido_invalido
    printf '%s' "$_m_imagem" | grep -Eq "^[a-z0-9./-]+:onprem-$VERSAO_ALVO\$" || falhar pedido_invalido
    if versao_maior "$_m_nucleo_min" "$5"; then falhar nucleo_antigo; fi
    # Anti-reversão também no premium: um pacote antigo, mesmo assinado, não se repõe.
    _piso_p=$(ler_piso_premium)
    if versao_valida "$_piso_p" && ! versao_maior "$VERSAO_ALVO" "$_piso_p"; then falhar versao_nao_superior; fi
}

# Descarrega o pacote do manifesto conferido ($1 o token) e confere-lhe o SHA-256.
descarregar_pacote_premium() {
    case "$LICENCAS_URL" in https://*) _proto="=https" ;; *) _proto="=https,http" ;; esac
    _ca=""
    [ -f "$CA_FORNECEDOR" ] && _ca="--cacert $CA_FORNECEDOR"
    # shellcheck disable=SC2086
    curl -fsSL --proto "$_proto" $_ca --max-time 1800 --max-filesize 1073741824 \
        -o "$TRAB/pacote.tar.gz" "$LICENCAS_URL/pacotes/$_m_pacote?t=$1" 2>/dev/null || falhar descarga_falhou
    [ "$(sha256_de "$TRAB/pacote.tar.gz")" = "$_m_sha" ] || falhar hash_invalido
}

# Extrai o pacote para $TRAB/pacote e carrega a imagem do manifesto.
carregar_pacote_premium() {
    # Só os ficheiros previstos, sem caminhos para fora nem ligações simbólicas. O tar
    # corre dentro da pasta, com nomes relativos: um caminho com `:` seria lido como
    # o de outra máquina.
    ( cd "$TRAB" && tar -tzf pacote.tar.gz ) > "$TRAB/lista.txt" 2>/dev/null || falhar pacote_invalido
    while IFS= read -r _n || [ -n "$_n" ]; do
        _n=${_n#./}
        case "$_n" in ""|premium-db/|mtls/) continue ;; esac
        case " $FICHEIROS_PACOTE " in *" $_n "*) ;; *) falhar pacote_invalido ;; esac
    done < "$TRAB/lista.txt"
    mkdir -p "$TRAB/pacote" || falhar interno
    # Sem os donos gravados no pacote: os ficheiros ficam do root, seja qual for o tar
    # com que o pacote foi montado, e nunca de um uid que o pacote escolha.
    ( cd "$TRAB" && tar --no-same-owner -xzf pacote.tar.gz -C pacote ) 2>/dev/null || falhar pacote_invalido
    [ -z "$(find "$TRAB/pacote" -type l 2>/dev/null)" ] || falhar pacote_invalido
    for _f in imagem.tar $FICHEIROS_INSTALADOS; do
        [ -f "$TRAB/pacote/$_f" ] || falhar pacote_invalido
    done
    grep -Fqx "    image: $_m_imagem" "$TRAB/pacote/docker-compose.premium.yml" || falhar pacote_invalido
    docker load -i "$TRAB/pacote/imagem.tar" > /dev/null 2>&1 || falhar load_falhou
    [ "$(docker image inspect --format '{{.Id}}' "$_m_imagem" 2>/dev/null)" = "$_m_imagem_id" ] || falhar imagem_invalida
}

# Os ficheiros do pacote extraído entram na instalação.
instalar_ficheiros_premium() {
    # Do root, só de leitura para os outros.
    mkdir -p "$INSTALL_DIR/premium-db" "$INSTALL_DIR/mtls" || falhar interno
    for _f in $FICHEIROS_INSTALADOS; do
        cp "$TRAB/pacote/$_f" "$INSTALL_DIR/$_f.novo" && chmod 644 "$INSTALL_DIR/$_f.novo" \
            && mv -f "$INSTALL_DIR/$_f.novo" "$INSTALL_DIR/$_f" || falhar interno
    done
    # Os segredos do premium (as passwords da base dele, a chave dos conetores): uma
    # instalação só com o núcleo não os tem. O gerador do pacote (conferido com o resto)
    # cria só os que faltam e nunca muda os que existem — como o instalador manual.
    ( cd "$INSTALL_DIR" && sh gen-secrets.sh ) > "$TRAB/segredos.log" 2>&1 || falhar interno
    # Valores públicos do fornecedor (a chave do envelope de IA, a CA): só estas chaves,
    # só com valores de base64.
    if [ -f "$TRAB/pacote/premium.env" ]; then
        for _k in $CHAVES_ENV_PREMIUM; do
            _v=$(campo "$_k" < "$TRAB/pacote/premium.env")
            [ -n "$_v" ] || continue
            so_caracteres "$_v" 16384 'A-Za-z0-9+/=_-' || falhar pacote_invalido
            definir_env "$_k" "$_v" || falhar interno
        done
    fi
}

processar_instalar_premium() {
    _p_pedido=$(printf '%s\n' "$_bruto" | campo premium_pedido)
    _p_segredo=$(printf '%s\n' "$_bruto" | campo segredo)
    _p_nif=$(printf '%s\n' "$_bruto" | campo nif)
    _p_empresa=$(printf '%s\n' "$_bruto" | campo empresa_id)
    _uuid='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    printf '%s' "$_p_pedido" | grep -Eq "$_uuid" || falhar pedido_invalido
    printf '%s' "$_p_empresa" | grep -Eq "$_uuid" || falhar pedido_invalido
    printf '%s' "$_p_segredo" | grep -Eq '^[A-Za-z0-9_-]{43}$' || falhar pedido_invalido
    printf '%s' "$_p_nif" | grep -Eq '^[0-9]{9}$' || falhar pedido_invalido
    registar_estado pedido_recebido
    [ ! -f "$STATE_DIR/premium_ativo" ] || falhar pedido_invalido

    conferir_recursos
    _nucleo=$(ler_piso)
    versao_valida "$_nucleo" || { log "sem piso de versões (agente nunca inicializado)"; falhar interno; }

    registar_estado a_verificar
    TRAB="$STATE_DIR/trabalho/$PEDIDO_ID"
    rm -rf "$TRAB"; mkdir -p "$TRAB/pacote" || falhar interno
    # O manifesto e o token vêm do serviço; o endereço do pacote é o deste agente.
    _corpo=$(printf '{"segredo":"%s","versao_nucleo":"%s","formato":"texto"}' "$_p_segredo" "$_nucleo")
    _http=$(servico_post "/pedidos/$_p_pedido/pacote" "$_corpo" "$TRAB/resposta.txt")
    case "$_http" in
        200) ;;
        # O serviço diz porquê: um núcleo antigo de mais não é um pedido recusado.
        409) grep -Fq '"nucleo_antigo"' "$TRAB/resposta.txt" 2>/dev/null && falhar nucleo_antigo
             falhar pedido_recusado ;;
        *) falhar descarga_falhou ;;
    esac
    conferir_manifesto_premium "$(campo manifesto_b64 < "$TRAB/resposta.txt")" \
        "$(campo assinatura < "$TRAB/resposta.txt")" "$(campo ficheiro < "$TRAB/resposta.txt")" \
        "$(campo token < "$TRAB/resposta.txt")" "$_nucleo"

    registar_estado a_descarregar
    descarregar_pacote_premium "$(campo token < "$TRAB/resposta.txt")"

    registar_estado a_carregar
    carregar_pacote_premium
    instalar_ficheiros_premium
    # O pedido para o sidecar: ele regista a identidade e vai buscar a licença.
    _pdir="$INSTALL_DIR/premium-pedido"
    mkdir -p "$_pdir" && chown "$UID_SIDECAR:$UID_SIDECAR" "$_pdir" 2>/dev/null; chmod 700 "$_pdir" 2>/dev/null
    printf '{"pedido_id":"%s","segredo":"%s","nif":"%s","empresa_id":"%s"}\n' \
        "$_p_pedido" "$_p_segredo" "$_p_nif" "$_p_empresa" > "$_pdir/pedido.json.tmp" || falhar interno
    chown "$UID_SIDECAR:$UID_SIDECAR" "$_pdir/pedido.json.tmp" 2>/dev/null; chmod 400 "$_pdir/pedido.json.tmp"
    mv -f "$_pdir/pedido.json.tmp" "$_pdir/pedido.json" || falhar interno

    # Só o sidecar: o backend não depende dele e não é tocado.
    registar_estado a_arrancar
    _lista=$(com_overlays_premium)
    if ! compose_com "$_lista" up -d premium < /dev/null > "$TRAB/up.log" 2>&1; then
        if grep -q "address pool" "$TRAB/up.log"; then falhar sem_redes; fi
        falhar arranque_falhou
    fi
    esperar_premium_pronto "$_lista" || falhar arranque_falhou
    printf '%s\n' "$VERSAO_ALVO" | escrever_ficheiro "$STATE_DIR/ultima_versao_premium"
    registar_estado instalado
    rm -rf "$TRAB"
    registar_presenca
    log "premium $VERSAO_ALVO instalado (sidecar a correr; o núcleo continua sem ele até ser ativado)"
}

# «Verificar agora» com a licença já emitida: o sidecar, à espera dela, procura-a já.
# É um toque, não um trabalho: não escreve no estado.txt, para não apagar o progresso
# de outro pedido, e o que falhar fica só no registo do agente.
processar_acordar_premium() {
    if [ -f "$STATE_DIR/premium_ativo" ]; then log "acordar_premium: o premium já está ativo"; return 0; fi
    for _o in $OVERLAYS_PREMIUM; do
        [ -f "$INSTALL_DIR/$_o" ] || { log "acordar_premium: o premium não está instalado"; return 0; }
    done
    if compose_com "$(com_overlays_premium)" exec -T premium premium-sidecar procurar-licenca \
            < /dev/null > /dev/null 2>&1; then
        log "pedida ao sidecar a procura da licença"
    else
        log "acordar_premium: o sidecar não aceitou o pedido"
    fi
}

processar_ativar_premium() {
    registar_estado pedido_recebido
    [ ! -f "$STATE_DIR/premium_ativo" ] || falhar pedido_invalido
    VERSAO_ALVO=$(ler_piso_premium); export VERSAO_ALVO
    versao_valida "$VERSAO_ALVO" || { VERSAO_ALVO=""; falhar pedido_invalido; }
    _com=$(com_overlays_premium)
    _sem="$COMPOSE_FILES"
    for _o in $OVERLAYS_PREMIUM; do [ -f "$INSTALL_DIR/$_o" ] || falhar pedido_invalido; done

    # A licença tem de valer (validada pelo serviço) antes de o núcleo depender do sidecar.
    registar_estado a_confirmar_licenca
    compose_com "$_com" exec -T premium premium-sidecar licenca < /dev/null > /dev/null 2>&1 || falhar licenca_invalida

    registar_estado a_ativar
    _env_antes=$(grep -m1 '^COMPOSE_FILE=' "$INSTALL_DIR/.env" 2>/dev/null || true)
    : > "$STATE_DIR/premium_ativo"
    definir_env COMPOSE_FILE "$_com" || true
    if compose_com "$_com" up -d < /dev/null > /dev/null 2>&1 && esperar_backend_saudavel "$_com"; then
        rm -rf "$INSTALL_DIR/premium-pedido"
        registar_estado ativado
        registar_presenca
        log "premium $VERSAO_ALVO ativado"
        return 0
    fi
    # O núcleo nunca fica em baixo por causa do premium: volta à configuração de antes.
    log "o núcleo não ficou saudável com o premium: a reverter"
    rm -f "$STATE_DIR/premium_ativo"
    if [ -n "$_env_antes" ]; then definir_env COMPOSE_FILE "${_env_antes#COMPOSE_FILE=}" || true
    else
        _tmp="$STATE_DIR/env.$$"; grep -v '^COMPOSE_FILE=' "$INSTALL_DIR/.env" > "$_tmp" || true
        cat "$_tmp" > "$INSTALL_DIR/.env"; rm -f "$_tmp"
    fi
    compose_com "$_sem" up -d < /dev/null > /dev/null 2>&1
    if esperar_backend_saudavel "$_sem"; then
        registar_estado revertido arranque_falhou
        registar_presenca
        exit 1
    fi
    falhar interno
}

# O backup antes de mexer: o do núcleo (cifrado, com os dados premium quando o premium
# está ativo). Sem backups configurados, só com o «aceito» do administrador, e então
# tira-se ao menos um dump da base premium, que é a que as migrações vão alterar.
criar_backup_premium() {
    _lista_bk="$1"; _aceito="$2"
    _saida=$(compose_com "$(compose_efetivo)" exec -T backend python -m app.backup.criar < /dev/null 2>/dev/null)
    _bk=$?
    if [ "$_bk" -eq 0 ]; then
        BACKUP_REF=$(printf '%s' "$_saida" | tail -n1 | tr -d '\r'); BACKUP_REF="${BACKUP_REF##*/}"; export BACKUP_REF
        return 0
    fi
    if [ "$_aceito" != "1" ]; then
        [ "$_bk" -eq 2 ] && falhar sem_backup
        falhar backup_falhou
    fi
    _pasta="$INSTALL_DIR/backups-pre-atualizacao"
    ( umask 077; mkdir -p "$_pasta" ) || return 0
    _f="$_pasta/premium-$(date +%Y%m%d-%H%M%S).dump"
    if ( umask 077; compose_com "$_lista_bk" exec -T premium-data-db pg_dump -U premium_data \
            -d nis2pme_premium_data -Fc < /dev/null > "$_f" ) 2>/dev/null && [ -s "$_f" ]; then
        BACKUP_REF="${_f##*/}"; export BACKUP_REF
    else
        rm -f "$_f"
        log "aviso: nem o dump da base premium foi possível"
    fi
}

# Volta aos ficheiros guardados em $2 e ao sidecar anterior (a imagem dele continua
# carregada). Com a base premium já migrada não se volta: a versão anterior sobre um
# esquema novo não é segura, e o caminho é o backup.
reverter_premium() {
    _lista_rv="$1"; _guardado="$2"; _mig="$3"; _ativo_rv="$4"
    log "o premium $VERSAO_ALVO não arrancou bem: a reverter"
    _mig_depois=$(migracoes_premium "$_lista_rv")
    if [ -z "$_mig" ] || [ "$_mig_depois" != "$_mig" ]; then
        falhar migracao_aplicada
    fi
    for _f in $FICHEIROS_INSTALADOS; do
        [ -f "$_guardado/$_f" ] || continue
        cp -p "$_guardado/$_f" "$INSTALL_DIR/$_f.volta" && mv -f "$INSTALL_DIR/$_f.volta" "$INSTALL_DIR/$_f" \
            || falhar interno
    done
    for _k in $CHAVES_ENV_PREMIUM; do
        _v=$(campo "$_k" < "$_guardado/env.txt")
        [ -z "$_v" ] || definir_env "$_k" "$_v" || falhar interno
    done
    if [ "$_ativo_rv" = 1 ]; then
        compose_com "$_lista_rv" up -d < /dev/null > /dev/null 2>&1
    else
        compose_com "$_lista_rv" up -d premium < /dev/null > /dev/null 2>&1
    fi
    if esperar_premium_pronto "$_lista_rv" && { [ "$_ativo_rv" = 0 ] || esperar_backend_saudavel "$_lista_rv"; }; then
        registar_estado revertido arranque_falhou
        rm -rf "$TRAB"
        registar_presenca
        log "revertido para o premium $VERSAO_ORIGEM"
        exit 1
    fi
    falhar interno
}

# Atualiza os módulos premium já instalados para a versão do manifesto que o núcleo
# recebeu do sidecar (assinado pela raiz; o token de download é desta licença).
processar_atualizar_premium() {
    _sem_backup=$(printf '%s\n' "$_bruto" | campo sem_backup)
    VERSAO_ORIGEM=$(ler_piso_premium); export VERSAO_ORIGEM
    registar_estado pedido_recebido
    case "$_sem_backup" in 0|1) ;; *) falhar pedido_invalido ;; esac
    versao_valida "$VERSAO_ORIGEM" || { VERSAO_ORIGEM=""; falhar sem_premium; }
    for _o in $OVERLAYS_PREMIUM; do [ -f "$INSTALL_DIR/$_o" ] || falhar sem_premium; done
    conferir_recursos
    _nucleo=$(ler_piso)
    versao_valida "$_nucleo" || { log "sem piso de versões (agente nunca inicializado)"; falhar interno; }

    registar_estado a_verificar
    TRAB="$STATE_DIR/trabalho/$PEDIDO_ID"
    rm -rf "$TRAB"; mkdir -p "$TRAB" || falhar interno
    _token=$(printf '%s\n' "$_bruto" | campo token)
    conferir_manifesto_premium "$(printf '%s\n' "$_bruto" | campo manifesto_b64)" \
        "$(printf '%s\n' "$_bruto" | campo assinatura)" "$(printf '%s\n' "$_bruto" | campo ficheiro)" \
        "$_token" "$_nucleo"

    registar_estado a_descarregar
    descarregar_pacote_premium "$_token"

    _lista=$(com_overlays_premium)
    _ativo=0
    [ -f "$STATE_DIR/premium_ativo" ] && _ativo=1
    registar_estado a_criar_backup
    criar_backup_premium "$_lista" "$_sem_backup"

    registar_estado a_carregar
    carregar_pacote_premium
    # O que está em uso fica guardado: é para aí que uma reversão volta.
    _guardado="$STATE_DIR/premium-antes"
    rm -rf "$_guardado"; mkdir -p "$_guardado/premium-db" "$_guardado/mtls" || falhar interno
    for _f in $FICHEIROS_INSTALADOS; do
        [ ! -f "$INSTALL_DIR/$_f" ] || cp -p "$INSTALL_DIR/$_f" "$_guardado/$_f" || falhar interno
    done
    for _k in $CHAVES_ENV_PREMIUM; do
        grep -m1 "^$_k=" "$INSTALL_DIR/.env" 2>/dev/null || true
    done > "$_guardado/env.txt"
    _mig_antes=$(migracoes_premium "$_lista")
    instalar_ficheiros_premium

    # Com o premium ativo, o compose recria o que mudou (o backend também, se o overlay
    # dele mudou); sem ele, só o sidecar.
    registar_estado a_arrancar
    if [ "$_ativo" = 1 ]; then
        compose_com "$_lista" up -d < /dev/null > "$TRAB/up.log" 2>&1
    else
        compose_com "$_lista" up -d premium < /dev/null > "$TRAB/up.log" 2>&1
    fi
    _rc=$?
    registar_estado a_confirmar
    if [ "$_rc" -eq 0 ] && esperar_premium_pronto "$_lista" \
            && { [ "$_ativo" = 0 ] || esperar_backend_saudavel "$_lista"; }; then
        printf '%s\n' "$VERSAO_ALVO" | escrever_ficheiro "$STATE_DIR/ultima_versao_premium"
        registar_estado concluido
        rm -rf "$TRAB"
        registar_presenca
        log "premium atualizado de $VERSAO_ORIGEM para $VERSAO_ALVO"
        return 0
    fi
    reverter_premium "$_lista" "$_guardado" "$_mig_antes" "$_ativo"
}

desinstalar() {
    systemctl disable --now nis2pme-agente.path > /dev/null 2>&1 || true
    rm -f /etc/systemd/system/nis2pme-agente.path /etc/systemd/system/nis2pme-agente.service
    systemctl daemon-reload > /dev/null 2>&1 || true
    rm -f "$CONF"
    log "agente desinstalado (o estado em $STATE_DIR e os dados da instalação ficam)"
}

# ---------------------------------------------------------------- arranque
case "${1:-}" in
    --estado)
        carregar_conf || exit 1
        registar_estado "${2:?fase}" "${3:-}"
        exit 0 ;;
    --registar)
        carregar_conf || exit 1
        registar_presenca; exit 0 ;;
    --definir-versao)
        carregar_conf || exit 1
        versao_valida "${2:-}" || exit 1
        printf '%s\n' "$2" | escrever_ficheiro "$STATE_DIR/ultima_versao"; exit $? ;;
    --fixar-versao)
        carregar_conf || exit 1
        fixar_piso "${2:-}"; exit $? ;;
    --desinstalar)
        carregar_conf || exit 1
        desinstalar; exit 0 ;;
    "") ;;
    *) log "argumento desconhecido: $1"; exit 2 ;;
esac

carregar_conf || exit 1

# Uma atualização de cada vez.
mkdir -p "$STATE_DIR"
if command -v flock > /dev/null 2>&1; then
    exec 9> "$STATE_DIR/agente.lock"
    flock -n 9 || { log "já há uma atualização em curso"; exit 0; }
else
    mkdir "$STATE_DIR/agente.lock.d" 2>/dev/null || { log "já há uma atualização em curso"; exit 0; }
    trap 'rmdir "$STATE_DIR/agente.lock.d" 2>/dev/null' EXIT
fi

processar_pedido
