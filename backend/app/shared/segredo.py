"""Segredos lidos de ficheiro (os `secrets:` do compose).

O compose monta cada segredo em /run/secrets, só no serviço que o usa, e passa o
caminho em `<NOME>_FILE`. Assim o valor fica fora do ambiente do processo — que
qualquer `docker inspect` mostra e os processos filhos herdam. O valor direto em
`<NOME>` continua a ser aceite (testes, desenvolvimento, e uma instalação a meio da
atualização); com os dois, manda o ficheiro.

Uma URL de base de dados chega sem password, e a password num ficheiro à parte
(`<URL>_PASSWORD_FILE`): `url_com_password` junta-as.

Este ficheiro é igual, byte a byte, no núcleo, no gateway, no relay, no License
Service e na borda de registo (cada um confere a sua cópia num teste). Só usa a
biblioteca padrão.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


class SegredoIndisponivel(RuntimeError):
    """O ficheiro de um segredo foi indicado mas não se lê.

    Com o ficheiro em falta no anfitrião, o Docker monta uma PASTA no lugar dele.
    Um segredo em falta não pode passar por «não configurado»: o serviço tem de
    parar e dizer porquê."""


def ler_segredo(nome: str, omissao: str = "", ambiente: Mapping[str, str] | None = None) -> str:
    """O valor do segredo `nome`: o ficheiro de `<nome>_FILE`, ou a variável
    `<nome>`. Vazio conta como ausente (devolve `omissao`). `ambiente` troca o
    ambiente do processo por outro (quem lê a configuração de um dicionário)."""
    amb = os.environ if ambiente is None else ambiente
    caminho = (amb.get(f"{nome}_FILE") or "").strip()
    if not caminho:
        return amb.get(nome) or omissao
    ficheiro = Path(caminho)
    if ficheiro.is_dir():
        raise SegredoIndisponivel(
            f"{nome}_FILE aponta para uma pasta ({caminho}): o ficheiro do segredo não "
            "existe no anfitrião (correr docker/gen-secrets.sh)"
        )
    try:
        valor = ficheiro.read_text(encoding="utf-8").strip()
    except OSError as erro:
        raise SegredoIndisponivel(f"{nome}_FILE: não foi possível ler {caminho} ({erro.strerror})") from None
    return valor or omissao


def url_com_password(url: str, password: str) -> str:
    """A URL com a password posta (codificada, como a URL pede). Sem password, a
    URL fica como vem — pode já trazer uma, como antes."""
    if not url or not password:
        return url
    partes = urlsplit(url)
    if not partes.hostname:
        raise ValueError("URL sem anfitrião: não se lhe pode juntar a password")
    utilizador = ""
    anfitriao = partes.netloc
    if "@" in anfitriao:
        credenciais, anfitriao = anfitriao.rsplit("@", 1)
        utilizador = credenciais.split(":", 1)[0]
    netloc = f"{utilizador}:{quote(password, safe='')}@{anfitriao}"
    return urlunsplit(partes._replace(netloc=netloc))


def url_da_base(nome_url: str, omissao: str = "") -> str:
    """A URL em `nome_url` com a password do ficheiro `<nome_url>_PASSWORD_FILE`
    (ou da variável `<nome_url>_PASSWORD`), se houver."""
    return url_com_password(os.getenv(nome_url, omissao), ler_segredo(f"{nome_url}_PASSWORD"))
