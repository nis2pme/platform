#!/bin/sh
# NIS2PME — Nginx container entrypoint
# Inicializa a configuração a partir do volume partilhado com o backend
# e monitoriza o ficheiro .reload para recarregar o nginx dinamicamente.
set -e

CONFIG_DIR="/run/nginx_config"
NGINX_CONF_DIR="/etc/nginx/conf.d"
CERTS_DIR="/etc/nginx/certs"
RELOAD_FILE="$CONFIG_DIR/.reload"

# Garantir que os diretórios necessários existem
mkdir -p "$CONFIG_DIR" "$CERTS_DIR"

# --- Inicialização ---
# Se o backend já escreveu uma config no volume, usa-a; caso contrário
# escreve a config padrão (HTTP) no volume para o backend encontrar.
if [ -f "$CONFIG_DIR/nginx.conf" ]; then
    echo "[nginx-entrypoint] Config encontrada no volume — a aplicar..."
    cp "$CONFIG_DIR/nginx.conf" "$NGINX_CONF_DIR/default.conf"
else
    echo "[nginx-entrypoint] Sem config no volume — a usar default (HTTP)."
    cp "$NGINX_CONF_DIR/default.conf" "$CONFIG_DIR/nginx.conf"
fi

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
