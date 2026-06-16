#!/bin/sh
# ============================================================
# NIS2PME Backend — Entrypoint (on-prem)
# Espera pela base de dados, corre migrações e inicia uvicorn.
# ============================================================
set -e

# ============================================================
# Auto-geração de secrets (primeira execução)
# Os secrets são gerados UMA ÚNICA VEZ e guardados no volume
# persistente /app/data/auto-secrets.env.
# Em actualizações subsequentes, o ficheiro já existe e é
# apenas carregado — os secrets NUNCA mudam, portanto os
# tokens JWT e chaves Fernet existentes continuam válidos.
# ============================================================
SECRETS_FILE="/app/data/auto-secrets.env"
mkdir -p /app/data

if [ ! -f "$SECRETS_FILE" ]; then
    echo "[entrypoint] Primeira execução — a gerar secrets de segurança..."
    python - <<'PYEOF'
import secrets, os
from cryptography.fernet import Fernet
from pathlib import Path

lines = [
    "# NIS2PME — Secrets auto-gerados na primeira execução.",
    "# NÃO apagar nem regenerar este ficheiro — invalida todos os tokens e cifras existentes.",
    f"JWT_SECRET_KEY={secrets.token_hex(32)}",
    f"JWT_REFRESH_SECRET_KEY={secrets.token_hex(32)}",
    f"TOTP_ENCRYPTION_KEY={Fernet.generate_key().decode()}",
    f"EVIDENCE_ENCRYPTION_KEY={Fernet.generate_key().decode()}",
    f"PII_ENCRYPTION_KEY={Fernet.generate_key().decode()}",
]
Path("/app/data/auto-secrets.env").write_text("\n".join(lines) + "\n")
print("[entrypoint] Secrets gerados e guardados em /app/data/auto-secrets.env")
PYEOF
    chmod 600 "$SECRETS_FILE"
fi

# Carregar secrets para variáveis de ambiente (apenas os que não estão já definidos)
# — permite override manual via .env se necessário
while read -r line; do
    # Remover quebras de linha Windows se existirem
    line=$(echo "$line" | tr -d '\r\n')
    
    # Ignorar comentários e linhas vazias
    case "$line" in
        '#'*|'') continue ;;
    esac
    
    # Separar a chave e o valor pelo primeiro sinal de igual
    key="${line%%=*}"
    value="${line#*=}"
    
    # Só exportar se a variável ainda não estiver definida no ambiente
    eval "current=\${${key}:-}"
    if [ -z "$current" ]; then
        export "${key}=${value}"
    fi
done < "$SECRETS_FILE"

echo "[entrypoint] A aguardar pela base de dados..."

# Aguardar que o PostgreSQL aceite ligações (máx. 60 tentativas × 2s = 120s)
RETRIES=60
until python -c "
import os, sys
try:
    import psycopg2
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    conn.close()
    sys.exit(0)
except Exception as e:
    sys.exit(1)
" 2>/dev/null; do
    RETRIES=$((RETRIES - 1))
    if [ "$RETRIES" -le 0 ]; then
        echo "[entrypoint] ERRO: Base de dados não ficou disponível a tempo. A sair."
        exit 1
    fi
    echo "[entrypoint] Base de dados ainda não está pronta. A aguardar... ($RETRIES tentativas restantes)"
    sleep 2
done

echo "[entrypoint] Base de dados pronta."

# Correr migrações Alembic
echo "[entrypoint] A executar migrações..."
python -m alembic upgrade head
echo "[entrypoint] Migrações concluídas."

# Configurar TLS do nginx no 1.º arranque a partir de TLS_MODE (apenas on-prem;
# idempotente via marcador; falha-suave — a app arranca à mesma).
echo "[entrypoint] A aplicar configuração TLS inicial..."
python -c "from app.setup.https_service import aplicar_tls_inicial; aplicar_tls_inicial()" \
    || echo "[entrypoint] AVISO: configuração TLS inicial falhou — configure no wizard."

# Iniciar a aplicação
echo "[entrypoint] A iniciar uvicorn..."
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1 \
    --no-access-log
