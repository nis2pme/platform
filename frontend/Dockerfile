# ============================================================
# NIS2PME Frontend — Dockerfile (on-prem, produção)
# O dist/ já está pré-compilado — apenas serve via Nginx.
# ============================================================
FROM nginx:stable-alpine

# Remover configuração default do nginx
RUN rm /etc/nginx/conf.d/default.conf

# Configuração base (HTTP — substituída em runtime via volume após wizard HTTPS)
COPY nginx.onprem.conf /etc/nginx/conf.d/default.conf

# Assets pré-compilados do Vue.js
COPY dist/ /usr/share/nginx/html/

# Entrypoint: monitoriza volume partilhado e recarrega nginx ao configurar HTTPS
COPY nginx-entrypoint.sh /nginx-entrypoint.sh
RUN chmod +x /nginx-entrypoint.sh \
    && mkdir -p /etc/nginx/certs \
    && chown -R nginx:nginx /usr/share/nginx/html \
    && chmod -R 755 /usr/share/nginx/html

EXPOSE 80 443

ENTRYPOINT ["/nginx-entrypoint.sh"]
