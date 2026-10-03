"""
A password da base dentro do `DATABASE_URL`.

O compose monta o `DATABASE_URL` colando a password do `.env` tal como está. As
geradas pelo instalador são hexadecimais e passam, mas o `.env` edita-se à mão: uma
password com `@`, `/`, `:` ou espaço dava uma URL que ninguém consegue ler, e a
aplicação ficava à espera de uma base que estava pronta.

Numa URL a password tem de ir codificada (`%40` para `@`). Aqui troca-se a password
crua pela codificada — e só quando ela aparece crua entre `:` e `@`. Uma URL que já
vem codificada, ou que não traz esta password, fica como está.
"""
from __future__ import annotations

from urllib.parse import quote


def codificar_password(url: str, password: str | None) -> str:
    """Devolve a URL com a password codificada, se ela lá estiver crua."""
    if not url or not password:
        return url
    codificada = quote(password, safe="")
    if codificada == password:
        return url
    crua = f":{password}@"
    if crua not in url:
        return url
    return url.replace(crua, f":{codificada}@", 1)
