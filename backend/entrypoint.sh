#!/bin/sh
# ============================================================
# NIS2PME Backend — Entrypoint (on-prem)
# Espera pela base de dados, corre migrações e inicia uvicorn.
# ============================================================
set -e

# ============================================================
# Fase root: reparar posse dos volumes e baixar privilégios
# Os volumes persistentes (ou o .env montado do host) podem
# trazer ficheiros com dono errado — de uma versão antiga da
# imagem que corria como root, de um restauro de backup ou de
# uma intervenção manual. Sem esta reparação, a app entra em
# crash-loop com "Permission denied" depois de um upgrade.
# O chown corre em cada arranque (auto-reparador) e o script
# re-executa-se de imediato como appuser — o uvicorn nunca
# corre como root.
# ============================================================
if [ "$(id -u)" = "0" ]; then
    chown -R appuser:appuser /app/data /app/uploads /app/nginx_config 2>/dev/null || true
    # Pasta onde a app deixa o pedido de atualização para o agente do anfitrião
    # (bind-mount): a app escreve-lhe como appuser. A de estado é só de leitura.
    if [ -d /app/atualizacao/pedido ]; then
        chown appuser:appuser /app/atualizacao/pedido 2>/dev/null || true
    fi
    # .env do host (bind-mount): o wizard precisa de o ler e escrever como appuser.
    if [ -f /app/.env ]; then
        chown appuser:appuser /app/.env 2>/dev/null || true
    fi
    # Sonda primeiro: um compose antigo (sem cap SETUID/SETGID) não permite baixar
    # privilégios — nesse caso avisa e segue como root neste arranque (como as
    # versões anteriores), em vez de deixar a instalação em crash-loop.
    if setpriv --reuid appuser --regid appuser --clear-groups true 2>/dev/null; then
        exec setpriv --reuid appuser --regid appuser --clear-groups "$0" "$@"
    fi
    echo "[entrypoint] AVISO: sem capacidade para baixar privilégios (docker-compose.yml antigo?)."
    echo "[entrypoint] A correr como root neste arranque. Atualize o docker-compose.yml para repor o arranque não-root."
fi

# ============================================================
# Auto-geração de secrets
# Guardados no volume persistente /app/data/auto-secrets.env.
# Cada secret é gerado UMA ÚNICA VEZ e nunca muda — os tokens
# JWT e os dados já cifrados continuam válidos para sempre.
#
# O ficheiro é verificado chave a chave, e não como um todo: um
# ficheiro criado por uma versão anterior pode não ter todas as
# chaves que a versão atual precisa. Nesse caso as que faltam
# são ACRESCENTADAS e as existentes ficam intactas. Antes, com
# a verificação a ser só "o ficheiro existe?", uma chave em
# falta passava despercebida e a app arrancava sem ela.
# ============================================================
SECRETS_FILE="/app/data/auto-secrets.env"
mkdir -p /app/data

python - "$SECRETS_FILE" <<'PYEOF'
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet

caminho = Path(sys.argv[1])

# Cada secret com a forma que lhe corresponde. Acrescentar aqui uma chave nova
# faz com que ela seja gerada tanto em instalações novas como nas já existentes.
# A JWT_REFRESH_SECRET_KEY já não se gera: não assina nada (os refresh tokens são
# opacos). Onde já existe, fica no ficheiro e é ignorada.
GERADORES = {
    "JWT_SECRET_KEY": lambda: secrets.token_hex(32),
    "TOTP_ENCRYPTION_KEY": lambda: Fernet.generate_key().decode(),
    "EVIDENCE_ENCRYPTION_KEY": lambda: Fernet.generate_key().decode(),
    "PII_ENCRYPTION_KEY": lambda: Fernet.generate_key().decode(),
}

CABECALHO = [
    "# NIS2PME — Secrets auto-gerados.",
    "# NÃO apagar nem regenerar este ficheiro — invalida todos os tokens e cifras existentes.",
]

primeira_vez = not caminho.exists()
linhas = caminho.read_text(encoding="utf-8").splitlines() if not primeira_vez else list(CABECALHO)

existentes = set()
preenchidas = []
for i, linha in enumerate(linhas):
    limpa = linha.strip()
    if not limpa or limpa.startswith("#") or "=" not in limpa:
        continue
    nome, _, valor = limpa.partition("=")
    nome = nome.strip()
    if nome not in GERADORES:
        continue
    if valor.strip():
        existentes.add(nome)
    else:
        # Chave presente mas SEM valor: preenche-se a própria linha em vez de
        # acrescentar outra, senão o ficheiro ficaria com a chave duas vezes.
        linhas[i] = f"{nome}={GERADORES[nome]()}"
        existentes.add(nome)
        preenchidas.append(nome)

em_falta = [nome for nome in GERADORES if nome not in existentes]
if em_falta or preenchidas:
    novas = [f"{nome}={GERADORES[nome]()}" for nome in em_falta]
    caminho.write_text("\n".join(linhas + novas) + "\n", encoding="utf-8")
    if primeira_vez:
        print("[entrypoint] Primeira execução — secrets de segurança gerados.")
    else:
        print(f"[entrypoint] Secrets em falta gerados: {', '.join(em_falta + preenchidas)}")
PYEOF
chmod 600 "$SECRETS_FILE"

# Carregar secrets para variáveis de ambiente (apenas os que não estão já definidos)
# — permite override manual via .env se necessário. O ficheiro está num volume
# onde a aplicação escreve: lê-se como dados, só com as chaves conhecidas.
. /app/segredos.sh
carregar_segredos "$SECRETS_FILE" "entrypoint"

echo "[entrypoint] A aguardar pela base de dados..."

# Aguardar que o PostgreSQL aceite ligações (máx. 60 tentativas × 2s = 120s)
RETRIES=60
# O tipo do erro vai para o registo (nunca a mensagem, que pode trazer a URL):
# "OperationalError" com a base de pé é uma password ou um endereço errado, não
# uma base que ainda está a arrancar.
until MOTIVO=$(python -c "
import os, sys
sys.path.insert(0, '/app')
try:
    import psycopg2
    from app.shared.url_base import codificar_password
    conn = psycopg2.connect(codificar_password(os.environ['DATABASE_URL'], os.environ.get('DB_PASSWORD')))
    conn.close()
except Exception as e:
    print(type(e).__name__)
    sys.exit(1)
" 2>/dev/null); do
    RETRIES=$((RETRIES - 1))
    if [ "$RETRIES" -le 0 ]; then
        echo "[entrypoint] ERRO: Base de dados não ficou disponível a tempo (último erro: ${MOTIVO:-desconhecido}). A sair."
        exit 1
    fi
    echo "[entrypoint] Base de dados ainda não está pronta (${MOTIVO:-sem resposta}). A aguardar... ($RETRIES tentativas restantes)"
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
#
# --workers 1 é REQUISITO DE SEGURANÇA, não uma escolha de desempenho. Os
# contadores de rate limit (slowapi) vivem na memória do processo: com N workers
# passam a existir N contadores independentes e o limite efetivo fica N vezes mais
# alto, sem erro nem aviso. Subir este número exige mover primeiro o rate limiting
# para armazenamento partilhado. A defesa que NÃO depende disto é o bloqueio de
# conta/IP no login, que vive na base de dados e vale em qualquer configuração.
echo "[entrypoint] A iniciar uvicorn..."
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers 1 \
    --no-access-log
