#!/bin/sh
# ============================================================
# NIS2PME — Restauro de backups .nbk (wrapper do host)
#
# Uso:
#   sh restaurar_backup.sh <ficheiro.nbk | nome-guardado> [flags]
#
# Sem flags: só INSPECIONA o backup (não altera nada).
# Flags (passadas ao restaurador):
#   --confirmo              executa mesmo o restauro
#   --sem-premium           ignora os dados premium do backup
#   --maquina-nova          primeira reposição num servidor novo
#                           (aplica também o .env do backup, preservando
#                           as passwords de BD geradas nesta máquina)
#   --sem-backup-seguranca  dispensa o backup de segurança (NÃO recomendado)
#
# O restauro em si corre DENTRO do container backend, com todas as
# proteções (cifra autenticada, verificação de versões, modo manutenção).
# Ver a secção "Restaurar um backup" no MANUAL.
# ============================================================
set -e

# Sobreponível só para ensaios num stack paralelo (outro nome de contentor).
CONTAINER=${NIS2PME_BACKEND_CONTAINER:-nis2pme_backend}

FICHEIRO="$1"
if [ -z "$FICHEIRO" ]; then
    echo "Uso: sh restaurar_backup.sh <ficheiro.nbk | nome-guardado> [--confirmo] [--sem-premium] [--maquina-nova]"
    exit 1
fi
shift

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "ERRO: container $CONTAINER não existe — arranque a stack primeiro (docker compose up -d)."
    exit 1
fi

# Ficheiro local (ex.: .nbk descarregado da UI)? Copiar para o container.
NOME=$(basename "$FICHEIRO")
if [ -f "$FICHEIRO" ]; then
    echo "A copiar $NOME para o container..."
    # Numa instalação nova a pasta ainda não existe (só nasce com o primeiro
    # backup), e é precisamente aí que se restaura para recuperar de um desastre.
    docker exec -u 10001 "$CONTAINER" mkdir -p /app/data/backups
    docker cp "$FICHEIRO" "$CONTAINER:/app/data/backups/$NOME"
    docker exec -u 0 "$CONTAINER" chown appuser:appuser "/app/data/backups/$NOME" 2>/dev/null || true
fi

CONFIRMO=0
MAQUINA_NOVA=0
for ARG in "$@"; do
    [ "$ARG" = "--confirmo" ] && CONFIRMO=1
    [ "$ARG" = "--maquina-nova" ] && MAQUINA_NOVA=1
done

# -t: terminal para o pedido de passphrase; -u 10001: o utilizador da app
# (ficheiros restaurados ficam com o dono certo). Os secrets auto-gerados são
# carregados dentro do container (o docker exec não herda o ambiente do
# entrypoint). BACKUP_PASSPHRASE no ambiente do host = uso não-interativo: a
# passphrase segue pela entrada do restaurador, e não com `-e`, que a deixava na
# linha de comandos do docker (legível no `ps` de qualquer utilizador desta
# máquina) e no ambiente do processo do restauro. O `printf` é da própria shell.
# Com --maquina-nova o restauro reescreve o .env (montado em /app/.env): guardar
# a versão desta máquina antes de ele lhe tocar.
if [ "$CONFIRMO" = "1" ] && [ "$MAQUINA_NOVA" = "1" ] && [ -f ./.env ]; then
    cp ./.env ./.env.antes-restauro
fi

# O ficheiro de secrets vive num volume onde a aplicação escreve: lê-se como dados
# (só as chaves conhecidas, sem ser interpretado), pelo carregador da imagem.
CMD='[ -f /app/segredos.sh ] || { echo "ERRO: imagem do backend sem /app/segredos.sh — atualizar a imagem." >&2; exit 1; }; . /app/segredos.sh; carregar_segredos /app/data/auto-secrets.env restauro; cd /app && exec python -m app.backup.restaurar "$@"'
if [ -n "$BACKUP_PASSPHRASE" ]; then
    printf '%s\n' "$BACKUP_PASSPHRASE" | docker exec -i -u 10001 \
        "$CONTAINER" sh -c "$CMD" restaurador "$NOME" --passphrase-stdin "$@"
else
    docker exec -it -u 10001 \
        "$CONTAINER" sh -c "$CMD" restaurador "$NOME" "$@"
fi

[ "$CONFIRMO" = "1" ] || exit 0

# ------------------------------------------------------------
# Reiniciar o backend: o entrypoint corre as migrações e o arranque
# finaliza o restauro (reconciliação + relatório + fim da manutenção).
# ------------------------------------------------------------
echo ""
echo "A reiniciar o backend para aplicar as migrações..."
docker restart "$CONTAINER" >/dev/null

echo "A aguardar que o backend fique saudável (pode demorar 1-2 min)..."
ESTADO="?"
TENTATIVAS=60
while [ "$TENTATIVAS" -gt 0 ]; do
    ESTADO=$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo "?")
    [ "$ESTADO" = "healthy" ] && break
    sleep 5
    TENTATIVAS=$((TENTATIVAS - 1))
done
echo "Estado do backend: $ESTADO"
if [ "$ESTADO" != "healthy" ]; then
    echo "AVISO: o backend ainda não está saudável — verifique: docker logs $CONTAINER"
fi

# ------------------------------------------------------------
# Máquina nova: aplicar o .env do backup, preservando as credenciais
# de base de dados desta máquina (o initdb dos volumes já as usou —
# trocá-las deixaria a app sem acesso às bases).
# ------------------------------------------------------------
if [ "$MAQUINA_NOVA" = "1" ]; then
    # O .env do backup já foi aplicado DENTRO do container, sobre este mesmo
    # ficheiro (montado em /app/.env): as variáveis do backup entram, as das
    # bases de dados desta máquina e as que só existem aqui ficam. Voltar a
    # aplicá-lo daqui substituía o ficheiro inteiro e perdia as que só existem
    # nesta máquina (portas, endereços de escuta).
    echo ""
    echo "O .env do backup foi aplicado (credenciais de BD desta máquina preservadas;"
    echo "o anterior ficou em .env.antes-restauro)."
    echo "Para ativar o novo .env:  docker compose up -d --force-recreate"
else
    echo ""
    echo "Nota: o .env do backup ficou em data/.env.restaurado (no container) para revisão manual."
fi

echo ""
echo "=== Relatório do restauro ==="
docker exec "$CONTAINER" cat /app/data/restauro-relatorio.json 2>/dev/null \
    || echo "(relatório ainda não disponível — verifique: docker logs $CONTAINER)"
echo ""
echo "Restauro terminado. Confirme o acesso pela UI."
