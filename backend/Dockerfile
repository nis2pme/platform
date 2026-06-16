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

# Incorporar as definições de frameworks (QNRCS 2026) na imagem
COPY frameworks /app/frameworks

# Tornar entrypoint executável
RUN chmod +x /app/entrypoint.sh

# Diretório de uploads (sobreposto pelo volume do compose)
RUN mkdir -p /app/uploads

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
