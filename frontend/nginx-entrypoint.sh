#!/bin/sh
# NIS2PME — Nginx container entrypoint
# Inicializa a configuração a partir do volume partilhado com o backend
# e monitoriza o ficheiro .reload para recarregar o nginx dinamicamente.
set -e

CONFIG_DIR="/run/nginx_config"
NGINX_CONF_DIR="/etc/nginx/conf.d"
CERTS_DIR="/etc/nginx/certs"
RELOAD_FILE="$CONFIG_DIR/.reload"
ACTIVE_CONF="$NGINX_CONF_DIR/default.conf"

# TRUST_CLOUDFLARE_HEADERS: a MESMA flag que o backend usa. Decide se este nginx confia no
# CF-Connecting-IP (real_ip + auditoria). Default false: em on-prem direto, confiar no header
# deixava um cliente forjar o IP que o backend consome (CWE-348). Este container não decide nada —
# limita-se a aplicar a flag recebida do ambiente.
# REAL_IP_FROM: fonte de confiança do real_ip quando a flag está ligada; gateway exato da bridge.
: "${TRUST_CLOUDFLARE_HEADERS:=false}"
: "${REAL_IP_FROM:=172.16.0.0/12}"

is_true() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

# O bloco real_ip/auditoria vem na config delimitado pelos marcadores __CF_REALIP_BEGIN/END__:
#  - Confiar no CF: substitui ${REAL_IP_FROM} pelo valor e remove só as linhas-marcador.
#  - Não confiar: apaga tudo entre os marcadores (sem real_ip; access_log volta ao default).
# Em configs sem marcadores (ex. geradas já condicionadas pelo backend) ambos os ramos são no-op.
apply_cf_realip() {
    [ -f "$ACTIVE_CONF" ] || return 0
    if is_true "$TRUST_CLOUDFLARE_HEADERS"; then
        sed -e "s|\${REAL_IP_FROM}|${REAL_IP_FROM}|g" \
            -e '/__CF_REALIP_BEGIN__/d' -e '/__CF_REALIP_END__/d' \
            "$ACTIVE_CONF" > "$ACTIVE_CONF.tmp" && mv "$ACTIVE_CONF.tmp" "$ACTIVE_CONF"
    else
        sed '/__CF_REALIP_BEGIN__/,/__CF_REALIP_END__/d' \
            "$ACTIVE_CONF" > "$ACTIVE_CONF.tmp" && mv "$ACTIVE_CONF.tmp" "$ACTIVE_CONF"
    fi
}

# Garantir que os diretórios necessários existem
mkdir -p "$CONFIG_DIR" "$CERTS_DIR"

# --- Inicialização ---
# Duas configs diferentes podem estar no volume, e não se tratam da mesma forma:
#
#  - A GERADA pelo backend (wizard de TLS) pertence à INSTALAÇÃO: tem os
#    certificados e o modo que aquele cliente escolheu. Nunca se toca — quem a
#    atualiza é o backend, que sabe preservar o modo em vigor.
#
#  - A ESTÁTICA pertence à IMAGEM. A cópia no volume é só um espelho, posto lá
#    no primeiro arranque para o backend a encontrar. Se ficar a mandar, a
#    imagem deixa de conseguir mudar seja o que for: uma correção de segurança
#    no nginx nunca chega a quem já arrancou uma vez, e não há sinal nenhum
#    disso — o container reinicia, diz que está tudo bem, e serve a config
#    antiga. Por isso refresca-se a partir da imagem.
if [ -f "$CONFIG_DIR/nginx.conf" ]; then
    if grep -q "Gerado automaticamente" "$CONFIG_DIR/nginx.conf" 2>/dev/null; then
        echo "[nginx-entrypoint] Config gerada pelo backend — a aplicar..."
        cp "$CONFIG_DIR/nginx.conf" "$NGINX_CONF_DIR/default.conf"
    else
        echo "[nginx-entrypoint] Config estática no volume — a refrescar a partir da imagem."
        cp "$NGINX_CONF_DIR/default.conf" "$CONFIG_DIR/nginx.conf"
    fi
else
    echo "[nginx-entrypoint] Sem config no volume — a usar default (HTTP)."
    cp "$NGINX_CONF_DIR/default.conf" "$CONFIG_DIR/nginx.conf"
fi
apply_cf_realip

# Copiar certificados se existirem no volume
if [ -d "$CONFIG_DIR/certs" ] && [ "$(ls -A "$CONFIG_DIR/certs" 2>/dev/null)" ]; then
    echo "[nginx-entrypoint] Certificados encontrados — a copiar..."
    cp -r "$CONFIG_DIR/certs/." "$CERTS_DIR/"
fi

# Remover sinalizador de reload antigo (se existir de execução anterior)
rm -f "$RELOAD_FILE"

# Iniciar nginx em background
nginx -g "daemon off;" &
NGINX_PID=$!
echo "[nginx-entrypoint] Nginx iniciado (PID=$NGINX_PID)"

# --- Loop de monitorização de reloads ---
while kill -0 "$NGINX_PID" 2>/dev/null; do
    if [ -f "$RELOAD_FILE" ]; then
        rm -f "$RELOAD_FILE"
        echo "[nginx-entrypoint] Sinal de reload recebido — a recarregar..."

        # Aplicar nova config
        if [ -f "$CONFIG_DIR/nginx.conf" ]; then
            cp "$CONFIG_DIR/nginx.conf" "$NGINX_CONF_DIR/default.conf"
            apply_cf_realip
        fi

        # Aplicar novos certificados
        if [ -d "$CONFIG_DIR/certs" ] && [ "$(ls -A "$CONFIG_DIR/certs" 2>/dev/null)" ]; then
            cp -r "$CONFIG_DIR/certs/." "$CERTS_DIR/"
        fi

        # Testar config antes de recarregar
        if nginx -t 2>/dev/null; then
            nginx -s reload
            echo "[nginx-entrypoint] Reload concluído com sucesso."
        else
            echo "[nginx-entrypoint] AVISO: Config nginx inválida — reload cancelado. A manter config anterior."
        fi
    fi
    sleep 3
done

wait "$NGINX_PID"
