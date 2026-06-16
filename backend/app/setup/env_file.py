"""Escrita segura de variáveis no ficheiro .env do host.

Partilhado pelos wizards de SMTP (app.setup.email_service) e de HTTPS
(actualização do APP_URL após configurar TLS). Centraliza a proteção contra
injeção de linhas (CWE-93): nenhum valor pode conter quebras de linha ou
carateres de controlo, que criariam variáveis de ambiente arbitrárias na
próxima leitura do env_file pelo Docker Compose.
"""
import os
import re
from pathlib import Path

from fastapi import HTTPException, status

from app.config import get_settings

ENV_PATH = Path(".env")


def formatar_valor(valor: str) -> str:
    """Envolve em aspas duplas se o valor contiver espaços, '#' ou aspas."""
    if re.search(r'[\s#"]', valor):
        escapado = valor.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escapado}"'
    return valor


def atualizar_env(updates: dict[str, str], comentario: str | None = None) -> None:
    """
    Atualiza (ou adiciona) as chaves indicadas no .env, aplica-as ao ambiente do
    processo atual e invalida a cache de get_settings(). `comentario`, se dado, é
    escrito antes das chaves NOVAS (não das já existentes).
    """
    # Defesa contra injeção de linhas no .env (CWE-93): nenhum valor pode conter
    # quebras de linha ou carateres de controlo.
    for _chave, _valor in updates.items():
        if any(ord(c) < 32 or ord(c) == 127 for c in str(_valor)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Configuração inválida: carateres de controlo não permitidos.",
            )

    pendentes = dict(updates)
    linhas: list[str] = []
    if ENV_PATH.exists():
        linhas = ENV_PATH.read_text(encoding="utf-8").splitlines()

    novas_linhas: list[str] = []
    for linha in linhas:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", linha)
        if m and m.group(1) in pendentes:
            chave = m.group(1)
            novas_linhas.append(f"{chave}={formatar_valor(str(pendentes.pop(chave)))}")
        else:
            novas_linhas.append(linha)

    if pendentes:
        if comentario:
            novas_linhas.append("")
            novas_linhas.append(comentario)
        for chave, valor in pendentes.items():
            novas_linhas.append(f"{chave}={formatar_valor(str(valor))}")

    ENV_PATH.write_text("\n".join(novas_linhas) + "\n", encoding="utf-8")

    for chave, valor in updates.items():
        os.environ[chave] = str(valor)

    get_settings.cache_clear()
