#!/bin/sh
# ============================================================
# NIS2PME — aplicador de atualizações
#
# Corre a partir do agente, DEPOIS de o agente ter verificado a assinatura do
# manifesto e o SHA-256 deste ficheiro. Vem com a versão que se instala, por isso
# a lógica de atualização evolui com ela.
#
# Entrada (ambiente, posto pelo agente): INSTALL_DIR PROJECT COMPOSE_FILES VERSAO
# VERSAO_ORIGEM PEDIDO_ID SEM_BACKUP NOVO_COMPOSE AGENTE STATE_DIR IMAGEM_BACKEND
# IMAGEM_FRONTEND MIN_LIVRE_KB (opcionais: ESPERA_MAX).
# Saída: 0 se concluiu; o progresso vai para o estado através do agente.
# ============================================================
set -u
umask 022

: "${INSTALL_DIR:?}" "${PROJECT:?}" "${VERSAO:?}" "${VERSAO_ORIGEM:?}" "${AGENTE:?}"
: "${NOVO_COMPOSE:?}" "${STATE_DIR:?}"
COMPOSE_FILES="${COMPOSE_FILES:-docker-compose.yml}"
SEM_BACKUP="${SEM_BACKUP:-0}"
MIN_LIVRE_KB="${MIN_LIVRE_KB:-3145728}"
ESPERA_MAX="${ESPERA_MAX:-120}"
ESPERA_PASSO="${ESPERA_PASSO:-5}"
IMAGEM_BACKEND="${IMAGEM_BACKEND:-}"
IMAGEM_FRONTEND="${IMAGEM_FRONTEND:-}"
BACKUP_REF=""
export PEDIDO_ID VERSAO_ORIGEM BACKUP_REF
VERSAO_ALVO="$VERSAO"; export VERSAO_ALVO

log() { printf '[aplicar] %s\n' "$*" >&2; }
est() { "$AGENTE" --estado "$1" "${2:-}"; }
falha() { est falhou "$1"; log "falhou: $1"; exit 1; }

cd "$INSTALL_DIR" || falha interno

ARGS=""
_ant=$IFS; IFS=:
for _f in $COMPOSE_FILES; do ARGS="$ARGS -f $INSTALL_DIR/$_f"; done
IFS=$_ant
PRINCIPAL="${COMPOSE_FILES%%:*}"
compose() { docker compose -p "$PROJECT" $ARGS "$@"; }

# Variáveis do .env só servem de argumento ao psql, e só se tiverem a forma certa.
ler_env() {
    grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- | tr -d '\r' \
        | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' || true
}
DB_USER=$(ler_env DB_USER); printf '%s' "$DB_USER" | grep -Eq '^[A-Za-z0-9_]+$' || DB_USER=nis2pme
DB_NAME=$(ler_env DB_NAME); printf '%s' "$DB_NAME" | grep -Eq '^[A-Za-z0-9_]+$' || DB_NAME=nis2pme

# O compose é lido com a versão pedida: a etiqueta vem do processo, nunca do .env.
NIS2PME_TAG="$VERSAO"; export NIS2PME_TAG

revisao_base() {
    compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" -tAc \
        'select version_num from alembic_version' < /dev/null 2>/dev/null | tr -d '\r\n '
}

esperar_saudavel() {
    _i=0
    while [ "$_i" -lt "$ESPERA_MAX" ]; do
        _cid=$(compose ps -q backend < /dev/null 2>/dev/null | head -n1)
        if [ -n "$_cid" ] && [ "$(docker inspect --format '{{.State.Health.Status}}' "$_cid" 2>/dev/null)" = "healthy" ]; then
            return 0
        fi
        _i=$((_i + 1))
        sleep "$ESPERA_PASSO"
    done
    return 1
}

imagem_de() {
    compose config --images < /dev/null 2>/dev/null | grep "/$1:" | head -n1
}

# ---------------------------------------------------------------- 1. preparar
_raiz=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)
[ -d "$_raiz" ] || _raiz="."
_livres=$(df -P "$_raiz" 2>/dev/null | awk 'NR==2 {print $4}')
case "$_livres" in ''|*[!0-9]*) _livres="" ;; esac
if [ -n "$_livres" ] && [ "$_livres" -lt "$MIN_LIVRE_KB" ]; then
    falha sem_espaco
fi

REV_ANTES=$(revisao_base)

# Pontos de retorno: as imagens em uso ficam com uma etiqueta própria. Sem isto,
# uma reversão não teria para onde voltar quando a etiqueta `latest` avançar.
ANTES=""
for _svc in backend frontend; do
    _cid=$(compose ps -q "$_svc" < /dev/null 2>/dev/null | head -n1)
    [ -n "$_cid" ] || continue
    _id=$(docker inspect --format '{{.Image}}' "$_cid" 2>/dev/null)
    _cfg=$(docker inspect --format '{{.Config.Image}}' "$_cid" 2>/dev/null)
    [ -n "$_id" ] && [ -n "$_cfg" ] || continue
    _repo="${_cfg%:*}"
    if docker tag "$_id" "$_repo:antes-$VERSAO_ORIGEM" 2>/dev/null; then
        ANTES="$ANTES $_svc=$_repo"
    fi
done

# ---------------------------------------------------------------- 2. backup
est a_criar_backup
dump_base() {
    _pasta="$INSTALL_DIR/backups-pre-atualizacao"
    ( umask 077; mkdir -p "$_pasta" ) || return 1
    _f="$_pasta/$DB_NAME-$(date +%Y%m%d-%H%M%S).dump"
    if ( umask 077; compose exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc < /dev/null > "$_f" ) 2>/dev/null \
        && [ -s "$_f" ]; then
        BACKUP_REF="${_f##*/}"; export BACKUP_REF
        return 0
    fi
    rm -f "$_f"
    return 1
}

_bk=1
if compose exec -T backend python -c "import app.backup.criar" < /dev/null > /dev/null 2>&1; then
    _saida=$(compose exec -T backend python -m app.backup.criar < /dev/null 2>/dev/null)
    _bk=$?
    if [ "$_bk" -eq 0 ]; then
        BACKUP_REF=$(printf '%s' "$_saida" | tail -n1 | tr -d '\r'); BACKUP_REF="${BACKUP_REF##*/}"; export BACKUP_REF
    fi
fi
if [ "$_bk" -ne 0 ]; then
    if [ "$SEM_BACKUP" != "1" ]; then
        [ "$_bk" -eq 2 ] && falha sem_backup
        falha backup_falhou
    fi
    # O administrador aceitou atualizar sem o backup cifrado: tira-se ao menos um
    # dump da base, que é o que as migrações da versão nova vão alterar.
    dump_base || log "aviso: nem o dump da base foi possível"
fi

# ----------------------------------------------------- 3. compose e imagens
est a_atualizar_imagens
cp -p "$INSTALL_DIR/$PRINCIPAL" "$INSTALL_DIR/$PRINCIPAL.bak-$VERSAO_ORIGEM" || falha interno
restaurar_compose() {
    cp -p "$INSTALL_DIR/$PRINCIPAL.bak-$VERSAO_ORIGEM" "$INSTALL_DIR/$PRINCIPAL.volta" \
        && mv -f "$INSTALL_DIR/$PRINCIPAL.volta" "$INSTALL_DIR/$PRINCIPAL"
}
cp "$NOVO_COMPOSE" "$INSTALL_DIR/$PRINCIPAL.novo" \
    && chmod 644 "$INSTALL_DIR/$PRINCIPAL.novo" \
    && mv -f "$INSTALL_DIR/$PRINCIPAL.novo" "$INSTALL_DIR/$PRINCIPAL" || falha interno

if ! compose pull < /dev/null; then
    restaurar_compose
    falha pull_falhou
fi

# O que se puxou tem de ser o que o manifesto assinado diz.
for _par in "backend:$IMAGEM_BACKEND" "frontend:$IMAGEM_FRONTEND"; do
    _svc="${_par%%:*}"; _dig="${_par#*:}"
    [ -n "$_dig" ] || continue
    _ref=$(imagem_de "$_svc")
    if [ -z "$_ref" ] || ! docker image inspect --format '{{range .RepoDigests}}{{.}}{{"\n"}}{{end}}' "$_ref" 2>/dev/null \
            | grep -q "@$_dig\$"; then
        restaurar_compose
        falha digest_invalido
    fi
done

# A etiqueta por omissão passa a apontar para o mesmo: um `docker compose up`
# manual daqui em diante não troca a versão por baixo.
for _svc in backend frontend; do
    _ref=$(imagem_de "$_svc")
    [ -n "$_ref" ] && docker tag "$_ref" "${_ref%:*}:latest" 2>/dev/null
done

# ------------------------------------------------------- 4. arrancar e confirmar
reverter() {
    log "a nova versão não ficou saudável"
    _depois=$(revisao_base)
    if [ -z "$REV_ANTES" ] || [ "$_depois" != "$REV_ANTES" ]; then
        # As migrações só andam para a frente: com a base alterada, ou sem poder
        # provar que não foi, repor as imagens antigas deixava-as sobre um esquema novo.
        falha migracao_aplicada
    fi
    restaurar_compose
    for _it in $ANTES; do
        _s="${_it%%=*}"; _r="${_it#*=}"
        docker tag "$_r:antes-$VERSAO_ORIGEM" "$_r:latest" 2>/dev/null
    done
    NIS2PME_TAG="antes-$VERSAO_ORIGEM"; export NIS2PME_TAG
    compose up -d < /dev/null
    if esperar_saudavel; then
        est revertido arranque_falhou
        log "revertido para a versão $VERSAO_ORIGEM"
        exit 1
    fi
    falha interno
}

est a_arrancar
compose up -d < /dev/null || reverter
est a_confirmar
esperar_saudavel || reverter

est concluido
log "atualizado para a versão $VERSAO"
exit 0
