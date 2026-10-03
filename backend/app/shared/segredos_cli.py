"""Segredos da instalação para quem corre FORA do processo da API.

O `entrypoint.sh` gera as chaves (JWT, TOTP, PII, evidências) para
`/app/data/auto-secrets.env` e exporta-as só para o processo que arranca. Um
`docker exec … python -m …` não as herda, e a configuração recusa arrancar sem
elas. Qualquer comando de linha chama isto antes de importar a configuração.

O ficheiro vive num volume onde a aplicação escreve. Por isso só se aceitam as
chaves da lista fechada: uma linha a mais (`LD_PRELOAD`, `PYTHONPATH`, variáveis
do libpq…) não chega ao ambiente de um comando que o operador está a correr. A
mesma lista existe no carregador da shell (`segredos.sh`); um teste confirma que
as duas coincidem.
"""
from __future__ import annotations

import os
from pathlib import Path

_SEGREDOS = Path("/app/data/auto-secrets.env")

# As chaves que o arranque gera, a antiga do refresh (já não assina nada, mas as
# instalações antigas têm-na no ficheiro) e as anteriores de uma rotação.
SEGREDOS_DA_INSTALACAO: frozenset[str] = frozenset({
    "JWT_SECRET_KEY",
    "JWT_REFRESH_SECRET_KEY",
    "TOTP_ENCRYPTION_KEY",
    "TOTP_ENCRYPTION_KEY_PREV",
    "EVIDENCE_ENCRYPTION_KEY",
    "EVIDENCE_ENCRYPTION_KEY_PREV",
    "PII_ENCRYPTION_KEY",
    "PII_ENCRYPTION_KEY_PREV",
})


def ler_segredos(caminho: Path | str) -> dict[str, str]:
    """As chaves conhecidas do ficheiro, e só essas. Sem ficheiro, vazio."""
    try:
        linhas = Path(caminho).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    segredos: dict[str, str] = {}
    for linha in linhas:
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave = chave.strip()
        if chave in SEGREDOS_DA_INSTALACAO:
            segredos[chave] = valor.strip().strip('"').strip("'")
    return segredos


def carregar_segredos_da_instalacao(caminho: Path | str | None = None) -> int:
    """Põe no ambiente as variáveis do ficheiro de segredos que ainda faltem.
    Devolve quantas carregou. Sem ficheiro (fora do Docker) não faz nada."""
    carregadas = 0
    for chave, valor in ler_segredos(caminho or _SEGREDOS).items():
        if chave not in os.environ:
            os.environ[chave] = valor
            carregadas += 1
    return carregadas
