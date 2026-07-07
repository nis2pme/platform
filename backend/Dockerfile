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
# mTLS do mesh (montado :ro) fica legível por um único uid em todos os consumidores
# (no VPS: chown -R 10001:10001 mtls/certs). Os diretórios escritos em runtime
# (uploads/data/nginx_config) ficam a pertencer ao uid → os volumes nomeados herdam
# essa posse no 1.º arranque. uvicorn liga em :8000 (porta alta, sem privilégios).
RUN useradd -r -u 10001 -d /app appuser \
 && mkdir -p /app/uploads /app/data /app/nginx_config \
 && chown -R 10001:10001 /app
USER 10001

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
