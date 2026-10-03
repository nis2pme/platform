"""
Rotação das chaves Fernet do núcleo, passo a passo.

    python -m app.shared.rodar_chaves estado
    python -m app.shared.rodar_chaves preparar PII_ENCRYPTION_KEY
    (reiniciar o backend)
    python -m app.shared.rodar_chaves recifrar
    python -m app.shared.rodar_chaves concluir PII_ENCRYPTION_KEY
    (reiniciar o backend e fazer logo um backup)

`preparar` põe a chave atual em `<NOME>_PREV` e gera uma nova, no ficheiro de
segredos da instalação. Depois do reinício a aplicação cifra com a nova e ainda
lê com a anterior. `recifrar` volta a cifrar com a chave atual tudo o que ainda
está na anterior: as colunas da base, os ficheiros de evidência e os arquivos da
trilha. Pode correr com a aplicação a funcionar e pode repetir-se. `concluir` só
retira a anterior depois de confirmar que já nada depende dela.

O que fica de fora, de propósito: os dados das linhas da trilha
(`dados_anteriores`/`dados_novos`) entram na cadeia de hashes, e voltar a
cifrá-los partia-a. O `estado` diz quantos valores cifrados estão lá dentro.

Rodar a PII_ENCRYPTION_KEY muda também as impressões dos IPs (derivam dela): as
anteriores deixam de se comparar com as novas.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.shared.chaves import NOMES

_PREFIXO = "gAAAAA"  # todos os tokens Fernet começam assim (versão 0x80 + timestamp)
_LOTE = 500

# Colunas que entram na cadeia de hashes da trilha: nunca se reescrevem.
_INTOCAVEIS = {("audit_logs", "dados_anteriores"), ("audit_logs", "dados_novos")}


@dataclass
class Contagem:
    rodados: int = 0
    ja_na_atual: int = 0
    ilegiveis: int = 0
    na_trilha: int = 0
    onde: dict[str, int] = field(default_factory=dict)

    def anotar(self, sitio: str) -> None:
        self.onde[sitio] = self.onde.get(sitio, 0) + 1


class Rotacao:
    """As chaves em rotação: para cada uma, a atual e a anterior."""

    def __init__(self, nomes: tuple[str, ...] = NOMES) -> None:
        from app.config import get_settings

        settings = get_settings()
        self.pares: list[tuple[str, Fernet, Fernet]] = []
        for nome in nomes:
            atual = getattr(settings, nome)
            anterior = getattr(settings, f"{nome}_PREV", "")
            if atual and anterior and anterior != atual:
                self.pares.append((nome, Fernet(atual.encode()), Fernet(anterior.encode())))
        self._atuais = [Fernet(getattr(settings, n).encode()) for n in NOMES if getattr(settings, n)]

    def rodar(self, token: str, contagem: Contagem, sitio: str, escrever: bool) -> str | None:
        """O token cifrado de novo com a chave atual, ou None se não é preciso."""
        dados = token.encode()
        for _nome, atual, anterior in self.pares:
            try:
                claro = anterior.decrypt(dados)
            except InvalidToken:
                continue
            contagem.rodados += 1
            contagem.anotar(sitio)
            return atual.encrypt(claro).decode() if escrever else None
        for atual in self._atuais:
            try:
                atual.decrypt(dados)
                contagem.ja_na_atual += 1
                return None
            except InvalidToken:
                continue
        contagem.ilegiveis += 1
        return None

    def rodar_bytes(self, dados: bytes, contagem: Contagem, sitio: str, escrever: bool) -> bytes | None:
        novo = self.rodar(dados.decode("ascii"), contagem, sitio, escrever)
        return novo.encode("ascii") if novo is not None else None


# ---------------------------------------------------------------------------
# Valores: um token solto, ou JSON com tokens lá dentro
# ---------------------------------------------------------------------------

def _rodar_json(valor: Any, rotacao: Rotacao, contagem: Contagem, sitio: str, escrever: bool):
    """(novo_valor, mudou) para uma estrutura JSON já carregada."""
    if isinstance(valor, str):
        if valor.startswith(_PREFIXO):
            novo = rotacao.rodar(valor, contagem, sitio, escrever)
            if novo is not None:
                return novo, True
        return valor, False
    if isinstance(valor, list):
        mudou = False
        saida = []
        for item in valor:
            novo, m = _rodar_json(item, rotacao, contagem, sitio, escrever)
            saida.append(novo)
            mudou = mudou or m
        return saida, mudou
    if isinstance(valor, dict):
        mudou = False
        saida = {}
        for chave, item in valor.items():
            novo, m = _rodar_json(item, rotacao, contagem, sitio, escrever)
            saida[chave] = novo
            mudou = mudou or m
        return saida, mudou
    return valor, False


def _rodar_valor(valor: Any, tipo: str, rotacao: Rotacao, contagem: Contagem, sitio: str, escrever: bool):
    """O valor novo de uma célula, ou None se fica como está."""
    if valor is None:
        return None
    if tipo in ("json", "jsonb"):
        novo, mudou = _rodar_json(valor, rotacao, contagem, sitio, escrever)
        return novo if mudou else None
    texto = str(valor)
    if texto.startswith(_PREFIXO):
        return rotacao.rodar(texto, contagem, sitio, escrever)
    # Texto com JSON guardado (por exemplo, parâmetros de notificações).
    try:
        carregado = json.loads(texto)
    except ValueError:
        return None
    novo, mudou = _rodar_json(carregado, rotacao, contagem, sitio, escrever)
    return json.dumps(novo, ensure_ascii=False) if mudou else None


# ---------------------------------------------------------------------------
# Base de dados
# ---------------------------------------------------------------------------

def _colunas(conn) -> dict[str, list[tuple[str, str]]]:
    from sqlalchemy import text

    linhas = conn.execute(text(
        "SELECT c.table_name, c.column_name, c.data_type "
        "FROM information_schema.columns c "
        "JOIN information_schema.tables t "
        "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        "WHERE c.table_schema = current_schema() AND t.table_type = 'BASE TABLE' "
        "  AND c.data_type IN ('text', 'character varying', 'json', 'jsonb') "
        "ORDER BY c.table_name, c.ordinal_position"
    )).all()
    por_tabela: dict[str, list[tuple[str, str]]] = {}
    for tabela, coluna, tipo in linhas:
        por_tabela.setdefault(tabela, []).append((coluna, tipo))
    return por_tabela


def _q(nome: str) -> str:
    return '"' + nome.replace('"', '""') + '"'


def recifrar_base(engine, rotacao: Rotacao, contagem: Contagem, escrever: bool) -> None:
    """Percorre as colunas de texto e JSON de todas as tabelas, em lotes."""
    from sqlalchemy import text

    with engine.connect() as conn:
        por_tabela = _colunas(conn)

    for tabela, colunas in por_tabela.items():
        tocaveis = [(c, t) for c, t in colunas if (tabela, c) not in _INTOCAVEIS]
        for c, _t in colunas:
            if (tabela, c) in _INTOCAVEIS:
                _contar_na_trilha(engine, tabela, c, contagem)
        if not tocaveis:
            continue
        filtro = " OR ".join(f"{_q(c)}::text LIKE '%{_PREFIXO}%'" for c, _t in tocaveis)
        selecao = ", ".join(_q(c) for c, _t in tocaveis)
        ultimo = "(0,0)"
        while True:
            with engine.begin() as conn:
                # O texto do ctid leva outro nome: com o mesmo, o ORDER BY ctid
                # ordenava pelo texto ("(0,10)" antes de "(0,9)"), o fim do lote
                # não era o maior ctid, e o lote seguinte saltava linhas.
                linhas = conn.execute(text(
                    f"SELECT ctid::text AS posicao, {selecao} FROM {_q(tabela)} "
                    f"WHERE ctid > CAST(:ultimo AS tid) AND ({filtro}) "
                    f"ORDER BY ctid LIMIT {_LOTE}"
                ), {"ultimo": ultimo}).all()
                if not linhas:
                    break
                for linha in linhas:
                    ctid = linha[0]
                    novos: dict[str, Any] = {}
                    for (coluna, tipo), valor in zip(tocaveis, linha[1:]):
                        novo = _rodar_valor(valor, tipo, rotacao, contagem, f"{tabela}.{coluna}", escrever)
                        if novo is not None:
                            novos[coluna] = (novo, tipo)
                    if novos and escrever:
                        atribuicoes = []
                        valores: dict[str, Any] = {"ctid": ctid}
                        for i, (coluna, (novo, tipo)) in enumerate(novos.items()):
                            if tipo in ("json", "jsonb"):
                                atribuicoes.append(f"{_q(coluna)} = CAST(:v{i} AS {tipo})")
                                valores[f"v{i}"] = json.dumps(novo, ensure_ascii=False)
                            else:
                                atribuicoes.append(f"{_q(coluna)} = :v{i}")
                                valores[f"v{i}"] = novo
                        # Pelo ctid: se a aplicação mudou a linha entretanto, o ctid
                        # já é outro e esta escrita não apanha nada — a próxima
                        # passagem trata dela.
                        conn.execute(
                            text(
                                f"UPDATE {_q(tabela)} SET {', '.join(atribuicoes)} "
                                "WHERE ctid = CAST(:ctid AS tid)"
                            ),
                            valores,
                        )
                ultimo = linhas[-1][0]


def _contar_na_trilha(engine, tabela: str, coluna: str, contagem: Contagem) -> None:
    from sqlalchemy import text

    with engine.connect() as conn:
        n = conn.execute(text(
            f"SELECT count(*) FROM {_q(tabela)} WHERE {_q(coluna)}::text LIKE '%{_PREFIXO}%'"
        )).scalar() or 0
    contagem.na_trilha += int(n)


# ---------------------------------------------------------------------------
# Ficheiros
# ---------------------------------------------------------------------------

def _substituir(caminho: Path, dados: bytes) -> None:
    """Escreve por cima de forma atómica, mantendo o dono e as permissões."""
    info = caminho.stat()
    temporario = caminho.with_name(caminho.name + ".rodar.tmp")
    with open(temporario, "wb") as f:
        f.write(dados)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(temporario, info.st_mode & 0o7777)
    if hasattr(os, "chown"):
        try:
            os.chown(temporario, info.st_uid, info.st_gid)
        except PermissionError:
            pass
    os.replace(temporario, caminho)


def recifrar_evidencias(raiz: Path, rotacao: Rotacao, contagem: Contagem, escrever: bool) -> None:
    """Cada ficheiro guardado cifrado (começa por um token Fernet) sob `raiz`."""
    if not raiz.is_dir():
        return
    for caminho in raiz.rglob("*"):
        if not caminho.is_file() or caminho.name.endswith(".tmp"):
            continue
        with open(caminho, "rb") as f:
            if f.read(len(_PREFIXO)) != _PREFIXO.encode():
                continue
        dados = caminho.read_bytes().strip()
        novo = rotacao.rodar_bytes(dados, contagem, "ficheiros de evidência", escrever)
        if novo is not None and escrever:
            _substituir(caminho, novo)


def recifrar_arquivos_trilha(raiz: Path, rotacao: Rotacao, contagem: Contagem, escrever: bool) -> None:
    """IP e user-agent dos arquivos da trilha. Os hashes da cadeia não os cobrem."""
    if not raiz.is_dir():
        return
    for caminho in sorted(raiz.glob("*.jsonl.gz")):
        mudou = False
        saida: list[str] = []
        with gzip.open(caminho, "rt", encoding="utf-8") as f:
            for linha in f:
                registo = json.loads(linha)
                for campo in ("ip_address", "user_agent"):
                    valor = registo.get(campo)
                    if isinstance(valor, str) and valor.startswith(_PREFIXO):
                        novo = rotacao.rodar(valor, contagem, "arquivos da trilha", escrever)
                        if novo is not None:
                            registo[campo] = novo
                            mudou = True
                saida.append(json.dumps(registo, ensure_ascii=False))
        if mudou and escrever:
            conteudo = gzip.compress(("\n".join(saida) + "\n").encode("utf-8"), mtime=0)
            _substituir(caminho, conteudo)


# ---------------------------------------------------------------------------
# Segredos da instalação
# ---------------------------------------------------------------------------

def _ficheiro_segredos() -> Path:
    from app.shared import segredos_cli

    return segredos_cli._SEGREDOS


def _ler_segredos(caminho: Path) -> list[str]:
    return caminho.read_text(encoding="utf-8").splitlines()


def _valor(linhas: list[str], nome: str) -> str:
    for linha in linhas:
        chave, sep, valor = linha.strip().partition("=")
        if sep and chave.strip() == nome:
            return valor.strip()
    return ""


def _definir(linhas: list[str], nome: str, valor: str | None) -> list[str]:
    """Troca (ou acrescenta) `nome=valor`; com None, retira a linha."""
    saida, feito = [], False
    for linha in linhas:
        chave, sep, _ = linha.strip().partition("=")
        if sep and chave.strip() == nome:
            if valor is not None and not feito:
                saida.append(f"{nome}={valor}")
            feito = True
            continue
        saida.append(linha)
    if not feito and valor is not None:
        saida.append(f"{nome}={valor}")
    return saida


def _gravar_segredos(caminho: Path, linhas: list[str]) -> None:
    _substituir(caminho, ("\n".join(linhas) + "\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# Passos
# ---------------------------------------------------------------------------

def _percorrer(escrever: bool) -> Contagem:
    from app.config import get_settings
    from app.database import engine

    rotacao = Rotacao()
    contagem = Contagem()
    recifrar_base(engine, rotacao, contagem, escrever)
    try:
        from app.superadmin.db import superadmin_engine  # só existe na imagem do superadmin
    except ImportError:
        superadmin_engine = None
    if superadmin_engine is not None:
        recifrar_base(superadmin_engine, rotacao, contagem, escrever)
    recifrar_evidencias(Path(get_settings().UPLOADS_DIR), rotacao, contagem, escrever)
    from app.auditoria.arquivo import ARQUIVO_DIR

    recifrar_arquivos_trilha(ARQUIVO_DIR, rotacao, contagem, escrever)
    return contagem


def _relatorio(contagem: Contagem, escrever: bool) -> dict:
    relatorio = {
        ("recifrados" if escrever else "ainda_na_anterior"): contagem.rodados,
        "ilegiveis": contagem.ilegiveis,
        "cifrados_dentro_da_trilha": contagem.na_trilha,
        "onde": contagem.onde,
    }
    # Ao escrever, uma linha reescrita pode voltar a aparecer mais à frente no
    # varrimento (a versão nova fica noutro ctid): a contagem das que já estavam
    # na chave atual só é fiel quando nada se escreve.
    if not escrever:
        relatorio["ja_na_atual"] = contagem.ja_na_atual
    return relatorio


def estado() -> int:
    from app.config import get_settings

    settings = get_settings()
    chaves = {n: {"anterior_definida": bool(getattr(settings, f"{n}_PREV", ""))} for n in NOMES}
    print(json.dumps({"chaves": chaves, **_relatorio(_percorrer(False), False)}, indent=1))
    return 0


def preparar(nome: str, env_antes: dict[str, str]) -> int:
    caminho = _ficheiro_segredos()
    if nome in env_antes:
        print(
            f"{nome} vem das variáveis do contentor (.env), não do ficheiro de segredos.\n"
            f"Faça a rotação à mão no .env: {nome}_PREV=<valor atual>, {nome}=<chave nova>,\n"
            "recrie o backend e corra `recifrar`."
        )
        return 1
    if not caminho.exists():
        print(f"Não existe {caminho}: este passo corre dentro do contentor do backend.")
        return 1
    linhas = _ler_segredos(caminho)
    atual = _valor(linhas, nome)
    if not atual:
        print(f"{nome} não está no ficheiro de segredos.")
        return 1
    if _valor(linhas, f"{nome}_PREV"):
        print(f"Já há uma rotação de {nome} em curso: corra `recifrar` e `concluir {nome}`.")
        return 1
    linhas = _definir(linhas, f"{nome}_PREV", atual)
    linhas = _definir(linhas, nome, Fernet.generate_key().decode())
    _gravar_segredos(caminho, linhas)
    print(
        f"{nome}: a chave atual passou a anterior e foi gerada uma nova.\n"
        "Próximo passo: reiniciar o backend (docker compose restart backend) e correr\n"
        "  python -m app.shared.rodar_chaves recifrar"
    )
    return 0


def recifrar() -> int:
    rotacao = Rotacao()
    if not rotacao.pares:
        print("Nenhuma chave em rotação: nada a fazer.")
        return 0
    contagem = _percorrer(True)
    print(json.dumps(_relatorio(contagem, True), indent=1))
    return 0


def concluir(nome: str, env_antes: dict[str, str]) -> int:
    from app.config import get_settings

    settings = get_settings()
    if not getattr(settings, f"{nome}_PREV", ""):
        print(f"{nome} não tem chave anterior: nada a concluir.")
        return 0
    rotacao = Rotacao((nome,))
    contagem = Contagem()
    from app.database import engine

    recifrar_base(engine, rotacao, contagem, False)
    recifrar_evidencias(Path(settings.UPLOADS_DIR), rotacao, contagem, False)
    from app.auditoria.arquivo import ARQUIVO_DIR

    recifrar_arquivos_trilha(ARQUIVO_DIR, rotacao, contagem, False)
    if contagem.rodados:
        print(
            f"Ainda há {contagem.rodados} valores na chave anterior de {nome} "
            f"({contagem.onde}). Corra `recifrar` primeiro."
        )
        return 1
    if f"{nome}_PREV" in env_antes:
        print(f"{nome}_PREV vem do .env: retire-a de lá e recrie o backend.")
        return 0
    caminho = _ficheiro_segredos()
    _gravar_segredos(caminho, _definir(_ler_segredos(caminho), f"{nome}_PREV", None))
    print(
        f"{nome}: chave anterior retirada.\n"
        "Reinicie o backend e faça já um backup — os anteriores levam a chave antiga,\n"
        "e é com ela que restauram."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rotação das chaves Fernet do núcleo.")
    sub = parser.add_subparsers(dest="passo", required=True)
    sub.add_parser("estado")
    for passo in ("preparar", "concluir"):
        p = sub.add_parser(passo)
        p.add_argument("chave", choices=NOMES)
    sub.add_parser("recifrar")
    args = parser.parse_args(argv)

    # O que já vinha do contentor, antes de se ler o ficheiro de segredos: é
    # assim que se sabe se uma chave está no .env (e aí a rotação é à mão).
    env_antes = {n: os.environ[n] for n in os.environ if n in NOMES or n.endswith("_PREV")}
    from app.shared.segredos_cli import carregar_segredos_da_instalacao

    carregar_segredos_da_instalacao()
    if args.passo == "estado":
        return estado()
    if args.passo == "preparar":
        return preparar(args.chave, env_antes)
    if args.passo == "recifrar":
        return recifrar()
    return concluir(args.chave, env_antes)


if __name__ == "__main__":
    sys.exit(main())
