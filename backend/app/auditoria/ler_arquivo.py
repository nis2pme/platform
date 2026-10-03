"""
Lê um ficheiro de arquivo do audit log e escreve-o legível no stdout.

    python -m app.auditoria.ler_arquivo /app/data/audit-archive/audit-<id>-2025-03.jsonl.gz

O `.jsonl.gz` já é legível com `zcat` — exceto o `ip_address` e o `user_agent`, que
saem cifrados tal como estão na base. Este comando decifra-os, usando a mesma
PII_ENCRYPTION_KEY que a aplicação. Sem ele o arquivo seria, na prática, só de escrita.

Corre dentro do contentor, onde a chave já está no ambiente do processo. Não existe
endpoint HTTP equivalente de propósito: seria um caminho novo a devolver o histórico de
endereços de toda a instalação, e teria de ser auditado ele próprio — muita superfície
para um caso de uso que é frio por natureza.

Um valor que não decifre sai como `null`, com o motivo em stderr, e a leitura continua.
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path


def _decifrar_linha(linha: dict) -> dict:
    from app.shared.pii import decifrar_pii

    for campo in ("ip_address", "user_agent"):
        if linha.get(campo):
            linha[campo] = decifrar_pii(linha[campo])
    return linha


def ler(caminho: Path, saida=sys.stdout) -> int:
    """Escreve o ficheiro decifrado, uma linha JSON por registo. Devolve o total."""
    total = 0
    with gzip.open(caminho, "rt", encoding="utf-8") as gz:
        for numero, bruta in enumerate(gz, start=1):
            bruta = bruta.strip()
            if not bruta:
                continue
            try:
                linha = json.loads(bruta)
            except ValueError:
                print(
                    f"{caminho.name}:{numero}: linha ilegível, ignorada.",
                    file=sys.stderr,
                )
                continue
            print(json.dumps(_decifrar_linha(linha), ensure_ascii=False), file=saida)
            total += 1
    return total


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2

    caminho = Path(argv[0])
    if not caminho.is_file():
        print(f"Ficheiro não encontrado: {caminho}", file=sys.stderr)
        return 1

    total = ler(caminho)
    print(f"{total} registos.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
