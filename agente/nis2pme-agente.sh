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

AGENTE_VERSAO="1"
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
        esac
    done < "$CONF"
    [ -n "$INSTALL_DIR" ] && [ -d "$INSTALL_DIR" ] || { log "INSTALL_DIR inválido"; return 1; }
    # Os caminhos entram em argumentos sem aspas: nada de espaços nem metacarateres.
    case "$INSTALL_DIR$COMPOSE_FILES$STATE_DIR" in
        *[!A-Za-z0-9_./:-]*) log "caminhos com carateres não suportados"; return 1 ;;
    esac
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
    printf 'agente_versao=%s\nregistado=%s\n' "$AGENTE_VERSAO" "$(agora)" \
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
    VERSAO_ALVO=$(printf '%s\n' "$_bruto" | campo versao)
    VERSAO_ORIGEM=""
    BACKUP_REF=""
    export PEDIDO_ID VERSAO_ALVO VERSAO_ORIGEM BACKUP_REF
    printf '%s' "$PEDIDO_ID" | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' \
        || { PEDIDO_ID=""; VERSAO_ALVO=""; falhar pedido_invalido; }
    versao_valida "$VERSAO_ALVO" || { VERSAO_ALVO=""; falhar pedido_invalido; }
    registar_estado pedido_recebido

    _sem_backup=$(printf '%s\n' "$_bruto" | campo sem_backup)
    _assinatura=$(printf '%s\n' "$_bruto" | campo assinatura)
    _manifesto_b64=$(printf '%s\n' "$_bruto" | campo manifesto_b64)
    case "$_sem_backup" in 0|1) ;; *) falhar pedido_invalido ;; esac
    printf '%s' "$_assinatura" | grep -Eq '^[A-Za-z0-9_-]{86}$' || falhar pedido_invalido
    printf '%s' "$_manifesto_b64" | grep -Eq '^[A-Za-z0-9+/=]{1,8192}$' || falhar pedido_invalido

    registar_estado a_verificar
    TRAB="$STATE_DIR/trabalho/$PEDIDO_ID"
    rm -rf "$TRAB"; mkdir -p "$TRAB" || falhar interno
    printf '%s' "$_manifesto_b64" | base64 -d > "$TRAB/manifesto.txt" 2>/dev/null || falhar pedido_invalido
    verificar_assinatura "$TRAB/manifesto.txt" "$_assinatura" || falhar assinatura_invalida

    # O manifesto já é de confiança; mesmo assim lê-se com o mesmo rigor.
    [ "$(head -n1 "$TRAB/manifesto.txt" | tr -d '\r')" = "nis2pme-manifesto 1" ] || falhar pedido_invalido
    _m_versao=$(campo versao < "$TRAB/manifesto.txt")
    _m_canal=$(campo canal < "$TRAB/manifesto.txt")
    _m_min=$(campo min_origem < "$TRAB/manifesto.txt")
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

    registar_estado a_descarregar
    descarregar docker-compose.yml "$TRAB/docker-compose.yml" "$_m_compose"
    _r=$?; [ $_r -eq 0 ] || { [ $_r -eq 3 ] && falhar hash_invalido; falhar descarga_falhou; }
    descarregar agente/aplicar-atualizacao.sh "$TRAB/aplicar-atualizacao.sh" "$_m_script"
    _r=$?; [ $_r -eq 0 ] || { [ $_r -eq 3 ] && falhar hash_invalido; falhar descarga_falhou; }
    descarregar agente/nis2pme-agente.sh "$TRAB/nis2pme-agente.sh" "$_m_agente"
    _r=$?; [ $_r -eq 0 ] || { [ $_r -eq 3 ] && falhar hash_invalido; falhar descarga_falhou; }

    registar_estado a_preparar
    env -i PATH="$PATH" HOME="${HOME:-/root}" LANG=C \
        INSTALL_DIR="$INSTALL_DIR" PROJECT="$PROJECT" COMPOSE_FILES="$COMPOSE_FILES" \
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
