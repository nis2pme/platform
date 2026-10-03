"""Backup completo a partir da linha de comandos — para o instalador correr
ANTES de puxar imagens novas numa atualização.

    docker compose exec -T backend python -m app.backup.criar

Códigos de saída: 0 backup criado; 2 sem passphrase definida (o operador
ainda não ativou os backups na aba Sistema — o instalador avisa e pergunta se
continua); 1 outro erro.
"""
from __future__ import annotations

import sys

from sqlmodel import Session


def main() -> int:
    from app.shared.segredos_cli import carregar_segredos_da_instalacao

    carregar_segredos_da_instalacao()
    from app.backup.service import criar_backup, passphrase_definida
    from app.database import engine

    if not passphrase_definida():
        print("Sem passphrase de backups definida — não é possível criar o backup.", file=sys.stderr)
        return 2
    try:
        with Session(engine) as db:
            resultado = criar_backup(db, "completo")
    except Exception as erro:  # noqa: BLE001 — é um CLI: o motivo vai para o stderr
        print(f"Backup falhou: {erro}", file=sys.stderr)
        return 1
    print(resultado.get("ficheiro", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
