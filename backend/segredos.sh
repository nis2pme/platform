#!/bin/sh
# Carrega as chaves da instalação a partir de /app/data/auto-secrets.env.
#
# Usado pelo arranque do backend, pelo do superadmin (cuja imagem é feita a partir
# desta) e pelo restauro de backups. O ficheiro vive num volume onde o backend
# escreve, por isso é tratado como dados e nunca como código:
#   - só se aceitam as chaves da lista fechada abaixo (as que o backend gera e as
#     anteriores de uma rotação); qualquer outra linha é ignorada e o nome fica no
#     registo, sem o valor;
#   - o valor é exportado tal e qual, sem ser interpretado pela shell.
# Uma variável já definida no ambiente ganha ao ficheiro (override pelo .env).
#
# A mesma lista existe em Python (app/shared/segredos_cli.py); um teste confirma
# que as duas coincidem.

SEGREDOS_DA_INSTALACAO="JWT_SECRET_KEY JWT_REFRESH_SECRET_KEY TOTP_ENCRYPTION_KEY TOTP_ENCRYPTION_KEY_PREV EVIDENCE_ENCRYPTION_KEY EVIDENCE_ENCRYPTION_KEY_PREV PII_ENCRYPTION_KEY PII_ENCRYPTION_KEY_PREV"

# carregar_segredos <ficheiro> [etiqueta-do-registo]
carregar_segredos() {
    _ficheiro="$1"
    _etiqueta="${2:-segredos}"
    [ -f "$_ficheiro" ] || return 0
    while IFS= read -r _linha || [ -n "$_linha" ]; do
        _linha=$(printf '%s' "$_linha" | tr -d '\r')
        case "$_linha" in '#'*|'') continue ;; esac
        _chave="${_linha%%=*}"
        _valor="${_linha#*=}"
        _aceite=0
        for _nome in $SEGREDOS_DA_INSTALACAO; do
            if [ "$_chave" = "$_nome" ]; then _aceite=1; break; fi
        done
        if [ "$_aceite" != "1" ]; then
            # Só o nome, e só se for um nome de variável: nunca o valor.
            case "$_chave" in
                *[!A-Za-z0-9_]*|'') echo "[$_etiqueta] AVISO: linha ignorada em $_ficheiro (não é uma chave conhecida)." >&2 ;;
                *) echo "[$_etiqueta] AVISO: chave ignorada em $_ficheiro: $_chave" >&2 ;;
            esac
            continue
        fi
        # Nome já validado contra a lista: a leitura indireta é segura aqui.
        _atual=$(printenv "$_chave" || true)
        if [ -z "$_atual" ]; then
            export "$_chave=$_valor"
        fi
    done < "$_ficheiro"
}
