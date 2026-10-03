"""
Verificador de referência do formato .nis2pme.

Ferramenta de linha de comandos que valida um dossiê SEM precisar da
plataforma do auditor — serve de suporte ("este ficheiro está íntegro?"),
de implementação de referência do formato e de harness de conformidade.

Uso (dentro do container do backend, ou em qualquer Python com as
dependências `cryptography` e `pyrage`):

    python -m app.dossie.verificar FICHEIRO.nis2pme
    python -m app.dossie.verificar FICHEIRO.nis2pme --passphrase "..."
    python -m app.dossie.verificar --auto-teste

Sem passphrase valida o que é público: estrutura, assinatura do envelope e
integridade do corpo (SHA-256). Com passphrase decifra e valida também o
manifest e o hash de TODAS as entradas. A verificação é por camadas e
fail-closed: pára no primeiro problema.

Nota de confiança: a assinatura prova que o cabeçalho e o corpo não foram
alterados desde a geração e que quem assinou detém a chave indicada —
confiar NESSA CHAVE (fingerprint confirmado por outro canal, ou atestação)
é sempre decisão de quem verifica.

Códigos de saída: 0 = válido; 1 = inválido/adulterado; 2 = erro de uso/IO.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path

from app.dossie import crypto, keyslot

# Versões conhecidas por este verificador (aceita sempre versões anteriores).
FORMATO_VERSAO_MAX = 1
SCHEMA_VERSAO_MAX = 1

# Limites estruturais do leitor (fail-closed contra ficheiros hostis). Os mesmos
# da plataforma do auditor.
_MAX_ENTRADAS = 10_000
_MAX_ENTRADAS_INTERNAS = 1_000            # o núcleo escreve umas dezenas
_MAX_DADOS_CLARO = 512 * 1024 * 1024      # zip interno descomprimido (bytes)
_MAX_ENVELOPE = 64 * 1024                 # linha do envelope (bytes)
_MAX_CHAVE_AGE = 64 * 1024                # o keyslot tem umas centenas de bytes
# O zip interno cifrado: o claro máximo mais o que o age lhe junta.
_MAX_DADOS_AGE = _MAX_DADOS_CLARO + _MAX_DADOS_CLARO // 512 + 4096

# Nomes de entrada admissíveis — tudo o resto é rejeitado (inclui traversal).
_RE_ENTRADA_CORPO = re.compile(
    r"^(chave\.age|dados\.age|ev/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.age)$"
)
_RE_ENTRADA_INTERNA = re.compile(r"^(manifest\.json|dados/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*\.json)$")


class FalhaVerificacao(Exception):
    """Uma camada de verificação falhou — o ficheiro não é de confiança."""

    def __init__(self, camada: str, detalhe: str) -> None:
        self.camada = camada
        super().__init__(detalhe)


def _sha256(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


def ler_entrada(z: zipfile.ZipFile, nome: str, maximo: int) -> bytes | None:
    """Uma entrada de um ZIP que veio de fora, com teto: nunca mais do que o
    tamanho que ela declara nem do que `maximo`. None se não existir, passar do
    teto ou não ler (CRC, cabeçalho, método).

    Não se usa o `ZipFile.read`: sem tamanho, o `zipfile` descomprime a entrada
    de uma vez e só depois a corta ao declarado — um tamanho declarado falso e
    um fluxo DEFLATE de 1 MiB davam 2 GiB em memória antes do corte."""
    try:
        info = z.getinfo(nome)
        if info.file_size > maximo:
            return None
        with z.open(info) as f:
            dados = f.read(info.file_size + 1)
    except (KeyError, zipfile.BadZipFile, NotImplementedError, RuntimeError, ValueError, OSError, EOFError):
        return None
    return dados if len(dados) <= info.file_size else None


def _sha256_entrada(z: zipfile.ZipFile, nome: str) -> tuple[int, str]:
    """(bytes, sha256) de uma entrada STORED do transporte, em blocos."""
    h = hashlib.sha256()
    total = 0
    with z.open(nome) as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
            total += len(bloco)
    return total, h.hexdigest()


# ---------------------------------------------------------------------------
# Camadas de verificação
# ---------------------------------------------------------------------------

def _ler_container(caminho: Path) -> tuple[dict, dict, bytes]:
    """Camadas 1–2: magic + envelope. Devolve (payload, envelope, corpo)."""
    with caminho.open("rb") as f:
        magic = f.readline(len(crypto.MAGIC) + 1)
        if magic != crypto.MAGIC:
            raise FalhaVerificacao("formato", "não é um ficheiro .nis2pme (magic inválido)")
        linha = f.readline(_MAX_ENVELOPE + 1)
        if len(linha) > _MAX_ENVELOPE:
            raise FalhaVerificacao("envelope", "envelope demasiado grande")
        corpo = f.read()

    try:
        env = json.loads(linha.decode("utf-8"))
        if not isinstance(env, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        raise FalhaVerificacao("envelope", "envelope não é JSON válido")

    payload = crypto.verificar_envelope(linha)
    if payload is None:
        raise FalhaVerificacao("assinatura", "assinatura Ed25519 inválida — cabeçalho adulterado ou chave errada")
    return payload, env, corpo


def _validar_payload(payload: dict) -> None:
    """Camada 3: estrutura e versões do cabeçalho assinado."""
    if payload.get("formato") != "nis2pme":
        raise FalhaVerificacao("cabecalho", "campo `formato` inesperado")
    versao = payload.get("versao")
    if not isinstance(versao, int) or not 1 <= versao <= FORMATO_VERSAO_MAX:
        raise FalhaVerificacao("cabecalho", f"versão do formato não suportada: {versao!r}")
    if payload.get("tipo") not in ("dossie", "parecer"):
        raise FalhaVerificacao("cabecalho", "campo `tipo` inesperado")
    cifra = payload.get("cifra") or {}
    if cifra.get("alg") != "age-v1" or cifra.get("modo") not in ("passphrase", "convite"):
        raise FalhaVerificacao("cabecalho", "algoritmo/modo de cifra não suportado")
    corpo = payload.get("corpo") or {}
    if not isinstance(corpo.get("sha256"), str) or not isinstance(corpo.get("bytes"), int):
        raise FalhaVerificacao("cabecalho", "descrição do corpo em falta")
    for campo in ("dossie_id", "criado_em"):
        if not isinstance(payload.get(campo), str):
            raise FalhaVerificacao("cabecalho", f"campo `{campo}` em falta")
    if payload["tipo"] == "parecer" and not isinstance(payload.get("dossie_ref"), str):
        raise FalhaVerificacao("cabecalho", "parecer sem `dossie_ref` (dossiê a que responde)")


def _validar_atestacao(atestacao: dict | None, pub_instancia: str, tipo: str) -> str:
    """Camada 4: atestação da chave (opcional). Devolve o texto do estado.
    A verificação em si vive em `crypto.verificar_atestacao` (partilhada com
    o exportador e o importador do parecer — uma só implementação). Um dossiê
    é assinado pela chave de dossiê da empresa; um parecer, pela identidade do
    auditor: a atestação tem de ser desse tipo de chave."""
    subject = crypto.SUBJECT_AUDITOR if tipo == "parecer" else crypto.SUBJECT_DOSSIE
    try:
        return crypto.verificar_atestacao(atestacao, pub_instancia, subject)
    except ValueError as erro:
        raise FalhaVerificacao("atestacao", str(erro)) from erro


def _validar_corpo(payload: dict, corpo: bytes) -> None:
    """Camada 5: integridade do corpo — ANTES de qualquer decifra."""
    esperado = payload["corpo"]
    if len(corpo) != esperado["bytes"]:
        raise FalhaVerificacao("corpo", f"tamanho do corpo difere ({len(corpo)} ≠ {esperado['bytes']})")
    if _sha256(corpo) != esperado["sha256"]:
        raise FalhaVerificacao("corpo", "SHA-256 do corpo não corresponde — ficheiro adulterado")


def _validar_conteudo(payload: dict, corpo: bytes, passphrase: str) -> dict:
    """Camadas 6–7: decifra o keyslot e o zip de dados, valida o manifest e o
    hash de TODAS as entradas. Devolve o manifest."""
    import pyrage

    try:
        zext = zipfile.ZipFile(io.BytesIO(corpo))
    except zipfile.BadZipFile:
        raise FalhaVerificacao("corpo", "corpo não é um ZIP de transporte válido")

    nomes = zext.namelist()
    if len(nomes) > _MAX_ENTRADAS:
        raise FalhaVerificacao("corpo", "demasiadas entradas no corpo")
    if len(set(nomes)) != len(nomes):
        raise FalhaVerificacao("corpo", "entradas repetidas no corpo")
    for nome in nomes:
        if not _RE_ENTRADA_CORPO.match(nome):
            raise FalhaVerificacao("corpo", f"entrada inesperada no corpo: {nome!r}")
    if "chave.age" not in nomes or "dados.age" not in nomes:
        raise FalhaVerificacao("corpo", "faltam as entradas chave.age/dados.age")
    # O transporte é STORED por norma: o conteúdo já é ciphertext (não comprime)
    # e assim o tamanho declarado é o real — uma entrada comprimida seria a
    # porta de uma bomba de descompressão.
    for info in zext.infolist():
        if info.compress_type != zipfile.ZIP_STORED or info.file_size != info.compress_size:
            raise FalhaVerificacao("corpo", f"entrada {info.filename!r} não é STORED")

    if payload["cifra"]["modo"] != "passphrase":
        raise FalhaVerificacao("cifra", "este verificador só decifra o modo passphrase")
    chave_age = ler_entrada(zext, "chave.age", _MAX_CHAVE_AGE)
    if chave_age is None:
        raise FalhaVerificacao("corpo", "chave.age ilegível ou demasiado grande")
    # O custo do scrypt vem no cabeçalho do keyslot, escolhido por quem gerou o
    # ficheiro, e paga-se antes de se saber se a passphrase está certa.
    custo = keyslot.custo_scrypt(chave_age)
    if custo is None:
        raise FalhaVerificacao("cifra", "o keyslot não é um age por passphrase")
    if custo > keyslot.N_LOG2_MAX:
        raise FalhaVerificacao(
            "cifra", f"custo do scrypt do keyslot acima do aceite (2^{custo}; máximo 2^{keyslot.N_LOG2_MAX})"
        )
    try:
        identidade = pyrage.x25519.Identity.from_str(
            pyrage.passphrase.decrypt(chave_age, passphrase).decode("ascii")
        )
    except Exception:
        raise FalhaVerificacao("cifra", "passphrase errada ou keyslot corrompido")

    dados_age = ler_entrada(zext, "dados.age", _MAX_DADOS_AGE)
    if dados_age is None:
        raise FalhaVerificacao("corpo", "dados.age ilegível ou demasiado grande")
    try:
        dados_claro = pyrage.decrypt(dados_age, [identidade])
    except Exception:
        raise FalhaVerificacao("cifra", "dados.age não decifra com a identidade do dossiê")
    del dados_age

    try:
        zint = zipfile.ZipFile(io.BytesIO(dados_claro))
    except zipfile.BadZipFile:
        raise FalhaVerificacao("dados", "conteúdo decifrado não é um zip válido")
    infos_int = zint.infolist()
    if len(infos_int) > _MAX_ENTRADAS_INTERNAS:
        raise FalhaVerificacao("dados", "demasiadas entradas no zip interno")
    if len({i.filename for i in infos_int}) != len(infos_int):
        raise FalhaVerificacao("dados", "entradas repetidas no zip interno")
    # Tamanhos declarados: a soma confere-se aqui e cada leitura para no seu.
    if sum(i.file_size for i in infos_int) > _MAX_DADOS_CLARO:
        raise FalhaVerificacao("dados", "zip interno excede o limite de descompressão")
    for nome in zint.namelist():
        if not _RE_ENTRADA_INTERNA.match(nome):
            raise FalhaVerificacao("dados", f"entrada inesperada no zip interno: {nome!r}")

    bruto = ler_entrada(zint, "manifest.json", _MAX_DADOS_CLARO)
    try:
        if bruto is None:
            raise ValueError
        manifest = json.loads(bruto.decode("utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        raise FalhaVerificacao("manifest", "manifest.json em falta ou ilegível")

    schema = manifest.get("schema_versao")
    if not isinstance(schema, int) or not 1 <= schema <= SCHEMA_VERSAO_MAX:
        raise FalhaVerificacao("manifest", f"schema não suportado: {schema!r}")
    if manifest.get("dossie_id") != payload["dossie_id"]:
        raise FalhaVerificacao("manifest", "dossie_id do manifest difere do cabeçalho assinado")

    entradas = manifest.get("entradas")
    if not isinstance(entradas, dict):
        raise FalhaVerificacao("manifest", "manifest sem mapa de entradas")

    # Cada entrada declarada tem de existir e bater certo no hash — e não pode
    # existir NADA no ficheiro que o manifest não declare.
    internos = {n for n in zint.namelist() if n != "manifest.json"}
    externos = {n for n in nomes if n.startswith("ev/")}
    declarados_int = {n for n in entradas if n.startswith("dados/")}
    declarados_ext = {n for n in entradas if n.startswith("ev/")}
    if internos != declarados_int:
        raise FalhaVerificacao("manifest", "entradas dados/* não coincidem com o manifest")
    if externos != declarados_ext:
        raise FalhaVerificacao("manifest", "entradas ev/* não coincidem com o manifest")

    for nome, meta in entradas.items():
        if not isinstance(meta, dict):
            raise FalhaVerificacao("manifest", f"metadados inválidos na entrada {nome!r}")
        if nome.startswith("dados/"):
            dados = ler_entrada(zint, nome, _MAX_DADOS_CLARO)
            if dados is None:
                raise FalhaVerificacao("dados", f"entrada {nome!r} ilegível")
            tamanho, resumo = len(dados), _sha256(dados)
        else:
            try:
                tamanho, resumo = _sha256_entrada(zext, nome)
            except KeyError:
                raise FalhaVerificacao("manifest", f"entrada declarada em falta: {nome!r}")
            except (zipfile.BadZipFile, ValueError, OSError, EOFError):
                raise FalhaVerificacao("corpo", f"entrada {nome!r} ilegível")
        if tamanho != meta.get("bytes") or resumo != meta.get("sha256"):
            raise FalhaVerificacao("manifest", f"hash/tamanho difere na entrada {nome!r}")

    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def verificar_ficheiro(caminho: Path, passphrase: str | None) -> dict:
    """Corre as camadas todas e devolve o relatório (lança FalhaVerificacao)."""
    payload, envelope, corpo = _ler_container(caminho)
    _validar_payload(payload)
    estado_atestacao = _validar_atestacao(envelope.get("atestacao"), envelope.get("pub", ""), payload["tipo"])
    _validar_corpo(payload, corpo)

    relatorio = {
        "valido": True,
        "tipo": payload["tipo"],
        "dossie_id": payload["dossie_id"],
        "criado_em": payload["criado_em"],
        "app_version": payload.get("app_version"),
        "cifra": payload["cifra"],
        "corpo_bytes": payload["corpo"]["bytes"],
        "fingerprint_instancia": crypto.fingerprint(envelope.get("pub", "")),
        "atestacao": estado_atestacao,
        "conteudo_verificado": False,
    }
    if passphrase is not None:
        manifest = _validar_conteudo(payload, corpo, passphrase)
        relatorio.update({
            "conteudo_verificado": True,
            "empresa": manifest.get("empresa", {}).get("nome"),
            "gerado_por": manifest.get("gerado_por", {}).get("nome"),
            "contagens": manifest.get("contagens"),
            "ambito": manifest.get("ambito"),
        })
    return relatorio


def _imprimir(relatorio: dict, como_json: bool) -> None:
    if como_json:
        print(json.dumps(relatorio, ensure_ascii=False, indent=1))
        return
    print("[OK] Ficheiro .nis2pme válido")
    print(f"  tipo: {relatorio['tipo']}   dossiê: {relatorio['dossie_id']}")
    print(f"  criado em: {relatorio['criado_em']}   app: {relatorio['app_version']}")
    print(f"  cifra: {relatorio['cifra']['alg']} (modo {relatorio['cifra']['modo']})")
    print(f"  corpo: {relatorio['corpo_bytes']} bytes, SHA-256 confirmado")
    print(f"  chave da instância: {relatorio['fingerprint_instancia']}")
    print(f"  atestação: {relatorio['atestacao']}")
    if relatorio["conteudo_verificado"]:
        print(f"  empresa: {relatorio['empresa']}   gerado por: {relatorio['gerado_por']}")
        print(f"  contagens: {relatorio['contagens']}")
        print("  conteúdo: manifest e hashes de todas as entradas confirmados")
    else:
        print("  conteúdo: não verificado (sem passphrase)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.dossie.verificar",
        description="Verifica a integridade e a origem de um ficheiro .nis2pme.",
    )
    parser.add_argument("ficheiro", nargs="?", help="caminho do .nis2pme a verificar")
    parser.add_argument("--passphrase", help="decifra e valida também o conteúdo")
    parser.add_argument("--json", action="store_true", help="relatório em JSON")
    parser.add_argument(
        "--auto-teste", action="store_true",
        help="corre o harness de conformidade do formato (não precisa de ficheiro)",
    )
    args = parser.parse_args(argv)

    if args.auto_teste:
        from app.dossie import verificar_teste
        return verificar_teste.correr()

    if not args.ficheiro:
        parser.error("indique o ficheiro a verificar (ou --auto-teste)")
    caminho = Path(args.ficheiro)
    if not caminho.is_file():
        print(f"[X] ficheiro não encontrado: {caminho}", file=sys.stderr)
        return 2

    try:
        relatorio = verificar_ficheiro(caminho, args.passphrase)
    except FalhaVerificacao as falha:
        print(f"[X] INVÁLIDO (camada: {falha.camada}) — {falha}", file=sys.stderr)
        return 1
    except OSError as erro:
        print(f"[X] erro de leitura: {erro}", file=sys.stderr)
        return 2
    _imprimir(relatorio, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
