# ============================================================
# NIS2PME Backend — Dockerfile (on-prem, produção)
# ============================================================
FROM python:3.11-slim

LABEL description="NIS2PME Backend FastAPI"

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# pg_dump/pg_restore 16 para o módulo de backups. O repo PGDG é necessário porque
# o Debian bookworm só traz o client 15, que recusa servidores PostgreSQL 16.
RUN install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# NOTA: o contexto de build é a pasta docker/ (ver docker-compose*.yml),
# para que a pasta frameworks/ (definições dos frameworks) seja incorporada na imagem
# e esta fique auto-suficiente — sem depender de um bind-mount do host.

# Instalar dependências de produção (apenas o necessário)
COPY backend/requirements.prod.txt .
RUN pip install --no-cache-dir -r requirements.prod.txt

# Copiar código da aplicação
COPY backend/ .

# Gerar os stubs Python do contrato premium.v1 (seam premium do open-core). grpcio-tools
# é só build-time (gera premium_pb2/premium_pb2_grpc); grpcio (runtime) vem em requirements.
RUN pip install --no-cache-dir grpcio-tools==1.68.1 \
 && sh scripts/gen_premium_stubs.sh \
 && pip uninstall -y grpcio-tools

# Incorporar as definições de frameworks (QNRCS 2026) na imagem
COPY frameworks /app/frameworks

# Tornar entrypoint executável
RUN chmod +x /app/entrypoint.sh

# Utilizador não-root. Mesmo uid 10001 do sidecar premium e do gateway → o material
# mTLS do mesh (montado :ro) fica legível por um único uid em todos os consumidores; a
# posse é atribuída por quem gera a PKI, sem passo manual. Os diretórios escritos em runtime
# (uploads/data/nginx_config) ficam a pertencer ao uid → os volumes nomeados herdam
# essa posse no 1.º arranque. uvicorn liga em :8000 (porta alta, sem privilégios).
#
# SEM "USER 10001" aqui: o entrypoint arranca como root APENAS para reparar a posse
# dos volumes persistentes (ficheiros dono root herdados de versões antigas, restauro
# de backups, intervenções manuais deixariam a app em crash-loop após upgrade) e baixa
# imediatamente para appuser via setpriv — o uvicorn nunca corre como root.
RUN useradd -r -u 10001 -d /app appuser \
 && mkdir -p /app/uploads /app/data /app/nginx_config \
 && chown -R 10001:10001 /app

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
