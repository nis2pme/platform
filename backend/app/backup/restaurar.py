"""
Restauro de backups `.nbk` — linha de comandos + finalização no arranque.

Uso (dentro do container backend, como o utilizador da app — ver o wrapper
`restaurar_backup.sh` na pasta do docker compose):

    python -m app.backup.restaurar <ficheiro> [--confirmo] [--sem-premium]
        [--maquina-nova] [--sem-backup-seguranca]

Sem `--confirmo` o script apenas decifra e MOSTRA o conteúdo do backup e sai
sem tocar em nada — serve para inspecionar um backup antes de decidir.

Proteções, por ordem de execução:
 1. cifra autenticada: passphrase errada ou ficheiro adulterado = recusa limpa;
 2. manifest validado ANTES de aplicar: formato conhecido, checksums dos dumps,
    revisão de migrações conhecida do código instalado (backup de versão mais
    recente que a app → recusa: "atualize a app primeiro");
 3. backup de segurança automático do estado atual (a rede de segurança);
 4. modo manutenção: a API responde 503 e os ticks param até ao fim;
 5. core: DROP SCHEMA + pg_restore — nunca `--clean` sobre o schema vivo, que
    deixaria tabelas de versões mais recentes órfãs;
 6. premium entregue ao sidecar (ImportarDados): o dono dos dados restaura a
    própria base e valida as migrações dela;
 7. migrações pós-restauro pelo caminho normal: o backend é reiniciado (o
    wrapper fá-lo; à mão é `docker restart nis2pme_backend`) e o entrypoint
    corre `alembic upgrade head` — um único caminho de migração, o dos updates;
 8. no arranque seguinte, `finalizar_restauro_no_arranque` reconcilia as
    evidências da BD com os ficheiros, regista tudo na auditoria e escreve o
    relatório final em data/restauro-relatorio.json.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.backup.service import (
    _DATA_DIR,
    _FORMATO,
    _UPLOADS_DIR,
    BACKUPS_DIR,
    criar_backup,
    passphrase_definida,
    separar_credencial,
)
from app.shared import manutencao

logger = logging.getLogger(__name__)

_ENV_RESTAURADO = _DATA_DIR / ".env.restaurado"
_PENDENTE_FILE = _DATA_DIR / "restauro-pendente.json"
_RELATORIO_FILE = _DATA_DIR / "restauro-relatorio.json"


class RestauroErro(Exception):
    """Erro de restauro com mensagem para o operador (sem stack trace) e um
    `codigo` estável para a UI traduzir (a mensagem é sempre em português)."""

    def __init__(self, mensagem: str, codigo: str = "restauro_falhou"):
        super().__init__(mensagem)
        self.codigo = codigo


# ---------------------------------------------------------------------------
# Leitura e validação do ficheiro .nbk
# ---------------------------------------------------------------------------

def _resolver_ficheiro(nome: str) -> Path:
    """Aceita um nome guardado em BACKUPS_DIR ou um caminho dentro do container."""
    caminho = Path(nome) if ("/" in nome or "\\" in nome) else BACKUPS_DIR / nome
    if not caminho.is_file():
        raise RestauroErro(f"Ficheiro de backup não encontrado: {caminho}",
                           codigo="backup_nao_encontrado")
    return caminho


def _decifrar_backup(caminho: Path, passphrase: str, destino: Path) -> dict:
    """Decifra o payload para `destino` (o tar.gz, no disco) e devolve o cabeçalho.
    A cifra é autenticada: passphrase errada ou um único byte adulterado = recusa
    aqui, antes de tocar em rigorosamente nada. Decifra por partes: memória
    constante, qualquer que seja o tamanho."""
    from app.backup import cifra

    with caminho.open("rb") as f:
        try:
            cabecalho = json.loads(f.readline().decode("utf-8"))
            formato = int(cabecalho.get("formato", 0))
        except (ValueError, UnicodeDecodeError, AttributeError, TypeError) as exc:
            raise RestauroErro("O ficheiro não é um backup .nbk válido.",
                               codigo="ficheiro_invalido") from exc

        # Compatibilidade para a frente: um formato desconhecido vem de uma versão
        # mais recente da app — recusar com instrução, nunca tentar adivinhar.
        if formato > _FORMATO:
            raise RestauroErro(
                f"Backup em formato {formato} — esta versão só lê até "
                f"{_FORMATO}. Atualize a app antes de restaurar.",
                codigo="formato_desconhecido",
            )
        # O formato 1 só existiu em builds de pré-lançamento (cifrava tudo em
        # memória); nenhuma versão publicada o escreveu.
        if formato != _FORMATO:
            raise RestauroErro(
                f"Backup em formato {formato}, de uma versão de pré-lançamento — não é suportado.",
                codigo="formato_desconhecido",
            )
        try:
            # A identidade abre-se com os parâmetros DO CABEÇALHO (não os atuais):
            # é isto que permite restaurar em máquina virgem só com ficheiro+passphrase.
            identidade = cifra.abrir_identidade(cabecalho.get("chave") or {}, passphrase)
            cifra.decifrar(f, destino, identidade)
        except cifra.ChaveErrada as exc:
            destino.unlink(missing_ok=True)
            raise RestauroErro(
                "Não foi possível decifrar: passphrase errada ou ficheiro corrompido/adulterado.",
                codigo="passphrase_errada",
            ) from exc
        except (cifra.Adulterado, KeyError, ValueError) as exc:
            destino.unlink(missing_ok=True)
            raise RestauroErro(
                "O backup não decifra: ficheiro corrompido, truncado ou adulterado. Nada foi alterado.",
                codigo="corrompido",
            ) from exc
    return cabecalho


def _extrair_seguro(arquivo: Path, destino: Path) -> None:
    """Extrai o tar.gz validando cada entrada (anti path-traversal, CWE-22):
    só ficheiros e diretórios, sempre dentro do destino. Lê do disco, por partes.

    Antes de extrair, soma o que as entradas declaram e confere-o com o espaço
    livre: um tar.gz de zeros descomprime ~1000× (medido: um `.nbk` de 255 KiB
    punha 256 MiB no disco), e a verificação pelo tamanho do `.nbk` não o via.
    O tamanho declarado é o que o `tarfile` escreve, por isso a conta é exata."""
    base = destino.resolve()
    with tarfile.open(arquivo, mode="r:gz") as tar:
        total = 0
        for membro in tar.getmembers():
            if not (membro.isfile() or membro.isdir()):
                raise RestauroErro(f"Entrada não suportada no arquivo: {membro.name}",
                                   codigo="ficheiro_invalido")
            alvo = (destino / membro.name).resolve()
            if not alvo.is_relative_to(base):
                raise RestauroErro(f"Caminho fora do destino no arquivo: {membro.name}",
                                   codigo="ficheiro_invalido")
            if membro.isfile():
                total += membro.size
        _verificar_espaco_extracao(total, destino)
        tar.extractall(destino)


# Margem que fica livre depois de extrair: a base de dados vive no mesmo disco.
_FOLGA_EXTRACAO = 100 * 1024 * 1024


def _verificar_espaco_extracao(total: int, destino: Path) -> None:
    livre = shutil.disk_usage(destino).free
    necessario = total + _FOLGA_EXTRACAO
    if livre < necessario:
        raise RestauroErro(
            f"Espaço em disco insuficiente para extrair o backup: "
            f"{livre // 1_048_576} MB livres, o conteúdo ocupa "
            f"~{total // 1_048_576} MB (mais {_FOLGA_EXTRACAO // 1_048_576} MB de margem).",
            codigo="espaco_insuficiente",
        )


def _abrir_para(caminho: Path, passphrase: str, workdir: Path) -> None:
    """Decifra e extrai o backup para `workdir`. O tar.gz decifrado fica ao lado
    (nunca dentro do destino da extração) e sai logo a seguir."""
    _verificar_espaco(caminho.stat().st_size)
    arquivo = workdir.with_name(workdir.name + ".tar.gz")
    try:
        _decifrar_backup(caminho, passphrase, arquivo)
        _extrair_seguro(arquivo, workdir)
    finally:
        arquivo.unlink(missing_ok=True)


def _verificar_espaco(payload_bytes: int) -> None:
    """Extração + backup de segurança precisam de folga — exigir 4× o payload."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    livre = shutil.disk_usage(BACKUPS_DIR).free
    necessario = max(payload_bytes * 4, 200 * 1024 * 1024)
    if livre < necessario:
        raise RestauroErro(
            f"Espaço em disco insuficiente para restaurar em segurança: "
            f"{livre // 1_048_576} MB livres, necessários ~{necessario // 1_048_576} MB.",
            codigo="espaco_insuficiente",
        )


def _sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def _rev_alembic_conhecida(rev: str | None) -> bool:
    """A revisão do backup tem de existir no código instalado — um backup de
    versão mais nova (ou adulterado) é recusado ANTES de tocar na base."""
    if not rev:
        return False
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(Config("alembic.ini"))
        return script.get_revision(rev) is not None
    except Exception:  # noqa: BLE001 — revisão desconhecida ou alembic.ini ausente
        return False


def _versao_mais_recente_que_app(versao_backup: str | None) -> bool:
    """True se o backup vier de uma app mais recente (recusar). Se as versões
    não forem comparáveis, fica a valer a verificação da revisão alembic."""
    from app.config import get_settings

    # Fora do try: uma falha das settings não pode passar por "versões
    # incomparáveis" (o ValidationError do pydantic é subclasse de ValueError).
    instalada = get_settings().APP_VERSION
    try:
        def tupla(v: str) -> tuple[int, ...]:
            return tuple(int(p) for p in v.strip().split(".")[:3])
        return tupla(versao_backup or "") > tupla(instalada)
    except ValueError:
        return False


def _sidecar_disponivel() -> bool:
    from app.config import get_settings
    settings = get_settings()
    if not settings.PREMIUM_ENABLED or not settings.PREMIUM_SIDECAR_ADDR:
        return False
    try:
        import grpc
        from app.premium.client import criar_canal_sidecar

        canal = criar_canal_sidecar(grpc, settings.PREMIUM_SIDECAR_ADDR)
        try:
            grpc.channel_ready_future(canal).result(timeout=3)
            return True
        finally:
            canal.close()
    except Exception:  # noqa: BLE001
        return False


def _validar_manifest(manifest: dict, workdir: Path, sem_premium: bool) -> None:
    componentes = manifest.get("componentes", {})
    checksums = manifest.get("checksums", {})

    if not componentes.get("core") or not (workdir / "core.dump").is_file():
        raise RestauroErro("Backup sem core.dump — arquivo incompleto.", codigo="corrompido")
    if _sha256(workdir / "core.dump") != checksums.get("core.dump"):
        raise RestauroErro("Checksum do core.dump não corresponde — arquivo corrompido.",
                           codigo="corrompido")
    if componentes.get("premium"):
        if not (workdir / "premium.dump").is_file():
            raise RestauroErro("Manifest anuncia premium.dump mas o arquivo não o contém.",
                               codigo="corrompido")
        if _sha256(workdir / "premium.dump") != checksums.get("premium.dump"):
            raise RestauroErro("Checksum do premium.dump não corresponde — arquivo corrompido.",
                               codigo="corrompido")

    if _versao_mais_recente_que_app(manifest.get("app_version")):
        raise RestauroErro(
            f"O backup foi criado numa versão mais recente da app "
            f"({manifest.get('app_version')}). Atualize a app primeiro.",
            codigo="versao_futura",
        )
    if not _rev_alembic_conhecida(manifest.get("alembic_rev")):
        raise RestauroErro(
            f"Revisão de migrações desconhecida no backup ({manifest.get('alembic_rev')}). "
            "Ou o backup vem de uma versão mais recente/modificada da app, ou está "
            "adulterado. Nada foi alterado.",
            codigo="revisao_desconhecida",
        )

    if componentes.get("premium") and not sem_premium and not _sidecar_disponivel():
        raise RestauroErro(
            "O backup contém dados premium mas o sidecar não está acessível. "
            "Arranque o container premium, ou repita com --sem-premium para "
            "restaurar só o core (os dados premium do backup serão ignorados).",
            codigo="premium_indisponivel",
        )


def _imprimir_resumo(manifest: dict, caminho: Path) -> None:
    componentes = manifest.get("componentes", {})
    print("\n=== Conteúdo do backup ===")
    print(f"  ficheiro:   {caminho.name}")
    print(f"  criado em:  {manifest.get('criado_em')}")
    print(f"  versão app: {manifest.get('app_version')}  (migrações: {manifest.get('alembic_rev')})")
    print(f"  modo:       {manifest.get('modo')}")
    print(f"  componentes: core={componentes.get('core')} premium={componentes.get('premium')} "
          f"uploads={componentes.get('uploads')} data={componentes.get('data')} env={componentes.get('env')}")


# ---------------------------------------------------------------------------
# Passos destrutivos (só com --confirmo)
# ---------------------------------------------------------------------------

def _restaurar_core(dump: Path) -> None:
    """DROP SCHEMA + pg_restore. O DROP (e não `pg_restore --clean`) garante que
    tabelas criadas por versões posteriores ao backup não sobrevivem órfãs — a
    base fica EXATAMENTE no estado do backup, e as migrações pós-restauro no
    arranque seguinte trazem-na para a versão instalada."""
    import psycopg2
    from app.config import get_settings

    url = get_settings().DATABASE_URL
    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute("SET lock_timeout = '5s'")
        for tentativa in range(3):
            # Terminar as outras ligações (uvicorn/ticks); a nossa sobrevive.
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
            try:
                cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
                break
            except psycopg2.errors.LockNotAvailable:
                if tentativa == 2:
                    raise RestauroErro(
                        "Não foi possível obter o lock da base (ligações ativas persistentes).",
                        codigo="base_ocupada",
                    )
        cur.execute("CREATE SCHEMA public")
    finally:
        conn.close()

    # pg_dump 16 (PG >= 15) não inclui o schema public no dump — o CREATE acima
    # repõe-no e o --exit-on-error pode ficar estrito sem falsos positivos.
    # A credencial vai no ambiente do subprocesso: como argumento ficaria em
    # /proc/<pid>/cmdline durante todo o restauro, que é a operação mais longa.
    url_sem_password, credencial = separar_credencial(url)
    resultado = subprocess.run(
        ["pg_restore", "--exit-on-error", "--no-owner",
         f"--dbname={url_sem_password}", str(dump)],
        capture_output=True, text=True, timeout=3600,
        env={**os.environ, **credencial},
    )
    if resultado.returncode != 0:
        raise RestauroErro(
            "pg_restore falhou a meio — a base pode ter ficado inconsistente. "
            "Restaure o backup de segurança criado no início deste restauro "
            f"(em {BACKUPS_DIR}). Detalhe: {resultado.stderr.strip()[-500:]}"
        )


def _restaurar_ficheiros(workdir: Path, manifest: dict) -> None:
    """data/ e uploads/ por sobreposição (nunca apaga o que lá está — ficheiros
    a mais são reportados como órfãos na reconciliação, nunca destruídos);
    o .env do backup fica em data/.env.restaurado para revisão do operador."""
    origem_data = workdir / "data"
    if origem_data.is_dir():
        for item in origem_data.iterdir():
            destino = _DATA_DIR / item.name
            if item.is_dir():
                shutil.copytree(item, destino, dirs_exist_ok=True)
            else:
                shutil.copy2(item, destino)

    origem_uploads = workdir / "uploads"
    if manifest.get("componentes", {}).get("uploads") and origem_uploads.is_dir():
        shutil.copytree(origem_uploads, _UPLOADS_DIR, dirs_exist_ok=True)

    origem_env = workdir / "env"
    if origem_env.is_file():
        shutil.copy2(origem_env, _ENV_RESTAURADO)
        _ENV_RESTAURADO.chmod(0o600)


# Variáveis que descrevem ESTA máquina e não a instalação: as credenciais e o
# endereço das bases de dados. Vêm do compose/gen-secrets de quem instalou aqui;
# as do backup apontavam para as bases da máquina antiga.
_ENV_CHAVES_LOCAIS = ("DATABASE_URL", "POSTGRES_", "_DB_PASSWORD", "_DB_USER", "_DB_NAME", "_DB_HOST", "_DB_PORT")
_ENV_DB_LOCAIS = ("DB_PASSWORD", "DB_USER", "DB_NAME", "DB_HOST", "DB_PORT")

# Segredos dos outros serviços, que vivem em ficheiros (docker/segredos/, montados só no
# serviço que usa cada um) desde que deixaram o .env. Um backup de antes ainda os traz no
# .env; aplicá-los punha-os de volta no ficheiro que o backend lê. Na máquina nova já
# existem, gerados para ela. A lista é a do docker/gen-secrets.sh (um teste confere).
# A CONNECTOR_SECRETS_KEY não está aqui: no on-prem continua no .env e vai no backup.
_ENV_EM_FICHEIRO = frozenset({
    "PREMIUM_DB_PASSWORD", "PREMIUM_DATA_DB_PASSWORD", "PREMIUM_DATA_SIDECAR_DB_PASSWORD",
    "LICENSE_DB_PASSWORD", "ENTITLEMENTS_GATEWAY_DB_PASSWORD", "ENTITLEMENTS_SIDECAR_DB_PASSWORD",
    "ENTITLEMENTS_SUPERADMIN_DB_PASSWORD", "GW_DB_PASSWORD", "VALKEY_PASSWORD",
    "GATEWAY_AUTH_TOKEN", "GATEWAY_PURGE_TOKEN", "GATEWAY_PROVISION_TOKEN",
    "GATEWAY_PROVISION_TOKEN_TRIAL", "LICENSE_ADMIN_TOKEN", "SAAS_TRIAL_INTERNAL_TOKEN",
    "SAAS_TRIAL_DB_PASSWORD", "FORM_SESSION_SECRET", "TRIAL_LEDGER_PEPPER",
    "CORE_SUSPEND_TOKEN", "SUPERADMIN_DB_PASSWORD", "SUPERADMIN_JWT_SECRET_KEY",
    "RELAY_ADMIN_TOKEN", "LICENSE_DESAFIO_KEY", "RESEND_API_KEY",
    "SAAS_TRIAL_TURNSTILE_SECRET_KEY", "GOOGLE_API_KEY", "LLM_API_KEY", "IA_LOCAL_API_KEY",
    "ANALISE_IA_ENVELOPE_PRIVKEY", "ANALISE_IA_ENVELOPE_PRIVKEY_PREV",
})


def _chave_local(chave: str) -> bool:
    """Identifica uma base de dados DESTA máquina — nunca vem do backup.

    A password de uma base vive também no volume dela: trazer a do servidor antigo
    deixava o `.env` a dizer uma coisa e o Postgres outra, e o backend não voltava
    a ligar-se no reinício seguinte. `DB_PASSWORD` (a do core) não tem prefixo, por
    isso não a apanha uma procura por `_DB_PASSWORD` — e foi assim que escapou.
    """
    return chave in _ENV_DB_LOCAIS or any(marca in chave for marca in _ENV_CHAVES_LOCAIS)


def _ler_env(caminho: Path) -> dict[str, str]:
    valores: dict[str, str] = {}
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        valores[chave.strip()] = valor.strip()
    return valores


def _aplicar_env_do_backup() -> dict:
    """Escreve no .env desta instalação as variáveis do .env do backup, exceto
    as que identificam as bases de dados desta máquina.

    É o que `--maquina-nova` sempre prometeu: num servidor virgem, SMTP, APP_URL,
    TLS, chaves de sessão e preferências vêm do backup; as passwords das bases
    ficam as que o instalador gerou aqui, e os segredos dos outros serviços, que
    já não vivem no .env, não voltam para lá. Sem isto o ficheiro ficava em
    `data/.env.restaurado` à espera de alguém que não sabia que tinha de o ler.
    """
    from app.setup.env_file import atualizar_env

    do_backup = _ler_env(_ENV_RESTAURADO)
    aplicar = {
        chave: valor for chave, valor in do_backup.items()
        if not _chave_local(chave) and chave not in _ENV_EM_FICHEIRO
    }
    if aplicar:
        atualizar_env(aplicar, comentario="Aplicado pelo restauro (--maquina-nova)")
    return {
        "aplicadas": len(aplicar),
        "preservadas": len(do_backup) - len(aplicar),
        "chaves_aplicadas": sorted(aplicar),
    }


def _restaurar_premium(dump: Path) -> str:
    """Envia o premium.dump ao sidecar (ImportarDados) — é ELE que restaura e
    valida a base dele. Devolve 'ok' ou 'falhou' (o core já está restaurado;
    uma falha aqui não pode desfazer o resto — fica no relatório)."""
    from app.config import get_settings

    try:
        import grpc
        from app.premium.client import criar_canal_sidecar
        from app.premium.proto import premium_pb2, premium_pb2_grpc

        def chunks():
            with dump.open("rb") as f:
                while bloco := f.read(1024 * 1024):
                    yield premium_pb2.BackupChunk(dados=bloco)

        canal = criar_canal_sidecar(grpc, get_settings().PREMIUM_SIDECAR_ADDR)
        try:
            premium_pb2_grpc.BackupServiceStub(canal).ImportarDados(chunks(), timeout=1800)
        finally:
            canal.close()
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.error("Restauro premium falhou: %s", exc)
        print(f"AVISO: o restauro do componente premium FALHOU ({exc}).")
        print("       O core foi restaurado; o backup de segurança contém os dados premium.")
        return "falhou"


# ---------------------------------------------------------------------------
# Fluxo partilhado (CLI F4 e UI F5 usam EXATAMENTE os mesmos passos)
# ---------------------------------------------------------------------------

def _aplicar_restauro(
    workdir: Path,
    manifest: dict,
    caminho: Path,
    *,
    sem_premium: bool,
    criar_seguranca: bool,
    maquina_nova: bool = False,
    executado_por: str | None = None,
    informar=lambda msg: None,
) -> dict:
    """Passos destrutivos do restauro. O chamador já validou o manifest.
    O reinício do processo e a finalização (migrações, reconciliação,
    relatório) ficam por conta do chamador e do arranque seguinte."""
    seguranca = None
    if criar_seguranca:
        if not passphrase_definida():
            raise RestauroErro(
                "Sem passphrase de backups definida não é possível criar o "
                "backup de segurança — defina-a primeiro.",
                codigo="passphrase_nao_definida",
            )
        informar("A criar backup de segurança do estado atual…")
        from sqlmodel import Session
        from app.database import engine
        with Session(engine) as db:
            seguranca = criar_backup(db, "completo")
        informar(f"Backup de segurança: {seguranca['ficheiro']}")

    informar("A ativar o modo manutenção (API responde 503 até ao fim)…")
    manutencao.ativar()
    from app.database import engine
    engine.dispose()

    # O registo de apagamentos de evidências vive em data/, e data/ vai ser
    # sobreposto com a cópia do backup — que não conhece os apagamentos feitos
    # depois dele. Lê-se antes e junta-se depois; a reaplicação corre no arranque.
    from app.evidencias import apagamentos
    registo_apagamentos = apagamentos.ler_linhas()

    informar("A restaurar a base de dados core…")
    _restaurar_core(workdir / "core.dump")

    informar("A restaurar ficheiros (segredos, uploads, .env)…")
    _restaurar_ficheiros(workdir, manifest)
    apagamentos.unir(registo_apagamentos)

    env_aplicado: dict | None = None
    if maquina_nova and _ENV_RESTAURADO.is_file():
        informar("Máquina nova: a aplicar o .env do backup (credenciais de BD desta máquina preservadas)…")
        env_aplicado = _aplicar_env_do_backup()
        informar(
            f".env aplicado: {env_aplicado['aplicadas']} variáveis do backup, "
            f"{env_aplicado['preservadas']} desta máquina mantidas."
        )

    estado_premium = "ausente"
    if manifest.get("componentes", {}).get("premium"):
        if sem_premium:
            estado_premium = "ignorado"
            informar("Componente premium ignorado (a pedido).")
        else:
            informar("A restaurar o componente premium (via sidecar)…")
            estado_premium = _restaurar_premium(workdir / "premium.dump")

    # A finalização (migrações, reconciliação, auditoria, relatório) acontece
    # no arranque seguinte — deixar o testemunho.
    _PENDENTE_FILE.write_text(json.dumps({
        "ficheiro": caminho.name,
        "modo": manifest.get("modo"),
        "app_version_backup": manifest.get("app_version"),
        "alembic_rev_backup": manifest.get("alembic_rev"),
        "premium": estado_premium,
        "maquina_nova": maquina_nova,
        "env_aplicado": env_aplicado,
        "executado_por": executado_por,
        "iniciado_em": datetime.now(timezone.utc).isoformat(),
    }))
    return {
        "premium": estado_premium,
        "backup_seguranca": seguranca["ficheiro"] if seguranca else None,
    }


def inspecionar_backup(nome: str, passphrase: str, sem_premium: bool = False) -> dict:
    """Decifra e valida um backup SEM tocar em nada — devolve o manifest.
    Usado pelo passo de verificação do wizard da UI."""
    caminho = _resolver_ficheiro(nome)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(dir=BACKUPS_DIR, prefix=".restauro-"))
    try:
        _abrir_para(caminho, passphrase, workdir)
        manifest = json.loads((workdir / "manifest.json").read_text())
        _validar_manifest(manifest, workdir, sem_premium)
        return manifest
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def executar_restauro_ui(
    nome: str, passphrase: str, sem_premium: bool = False,
    executado_por: str | None = None,
) -> dict:
    """Restauro completo a partir da UI — as mesmas proteções do CLI, com o
    backup de segurança sempre obrigatório. O chamador (router) responde ao
    cliente e só depois reinicia o processo."""
    caminho = _resolver_ficheiro(nome)
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(dir=BACKUPS_DIR, prefix=".restauro-"))
    try:
        _abrir_para(caminho, passphrase, workdir)
        manifest = json.loads((workdir / "manifest.json").read_text())
        _validar_manifest(manifest, workdir, sem_premium)
        return _aplicar_restauro(
            workdir, manifest, caminho, sem_premium=sem_premium,
            criar_seguranca=True, executado_por=executado_por,
            informar=logger.info,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Entrada do script
# ---------------------------------------------------------------------------

def _ler_passphrase(da_entrada: bool) -> str:
    """A passphrase por stdin (o wrapper não-interativo), pelo terminal, ou — por
    compatibilidade com quem já a passava assim — pelo ambiente.

    O wrapper passava-a com `docker exec -e BACKUP_PASSPHRASE=…`: ficava na linha
    de comandos do `docker` no anfitrião (visível a qualquer utilizador da
    máquina, durante todo o restauro) e no ambiente deste processo. Pela entrada
    não fica em nenhum dos dois."""
    if da_entrada:
        linha = sys.stdin.readline()
        passphrase = linha[:-1] if linha.endswith("\n") else linha
        if passphrase.endswith("\r"):
            passphrase = passphrase[:-1]
        if not passphrase:
            raise RestauroErro("Passphrase vazia na entrada.", codigo="passphrase_errada")
        return passphrase
    return os.environ.get("BACKUP_PASSPHRASE") or getpass.getpass("Passphrase do backup: ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.backup.restaurar",
        description="Restaura um backup .nbk desta instalação (ver MANUAL).",
    )
    parser.add_argument("ficheiro", help="nome em data/backups/ ou caminho no container")
    parser.add_argument("--confirmo", action="store_true",
                        help="executa mesmo (sem esta flag só mostra o conteúdo)")
    parser.add_argument("--sem-premium", action="store_true",
                        help="ignora o componente premium do backup")
    parser.add_argument("--maquina-nova", action="store_true",
                        help="instalação virgem: dispensa o backup de segurança")
    parser.add_argument("--sem-backup-seguranca", action="store_true",
                        help="não cria o backup de segurança (NÃO recomendado)")
    parser.add_argument("--passphrase-stdin", action="store_true",
                        help="lê a passphrase da primeira linha da entrada (uso não-interativo)")
    args = parser.parse_args(argv)

    try:
        caminho = _resolver_ficheiro(args.ficheiro)
        passphrase = _ler_passphrase(args.passphrase_stdin)
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(dir=BACKUPS_DIR, prefix=".restauro-"))
        try:
            _abrir_para(caminho, passphrase, workdir)
            manifest = json.loads((workdir / "manifest.json").read_text())
            _validar_manifest(manifest, workdir, args.sem_premium)
            _imprimir_resumo(manifest, caminho)

            if not args.confirmo:
                print("\nInspeção concluída — nada foi alterado.")
                print("Para executar o restauro, repita o comando com --confirmo.")
                return 0

            # Backup de segurança do estado ATUAL — a rede de segurança de tudo
            # o que se segue. Exceções: máquina virgem (nada a proteger) ou
            # dispensa explícita do operador.
            criar_seguranca = not (args.maquina_nova or args.sem_backup_seguranca)
            if not criar_seguranca:
                print("A saltar o backup de segurança (pedido do operador).")
            elif not passphrase_definida():
                raise RestauroErro(
                    "Sem passphrase de backups definida não é possível criar o "
                    "backup de segurança. Numa instalação virgem use "
                    "--maquina-nova; para dispensar conscientemente, "
                    "--sem-backup-seguranca.",
                    codigo="passphrase_nao_definida",
                )

            _aplicar_restauro(
                workdir, manifest, caminho, sem_premium=args.sem_premium,
                criar_seguranca=criar_seguranca, maquina_nova=args.maquina_nova,
                informar=print,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        print("\nDados aplicados. Falta reiniciar o backend para correr as migrações:")
        print("    docker restart nis2pme_backend        (o wrapper fá-lo sozinho)")
        print("Até lá a API responde 503 (modo manutenção).")
        print(f"Relatório final (após o arranque): {_RELATORIO_FILE}")
        if _ENV_RESTAURADO.is_file():
            print(f"O .env do backup ficou em {_ENV_RESTAURADO} — reveja antes de aplicar.")
        return 0

    except RestauroErro as erro:
        print(f"\nERRO: {erro}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Finalização no arranque seguinte (chamada pelo lifespan em main.py, on-prem)
# ---------------------------------------------------------------------------

def _no_disco(caminho: str) -> str:
    """Caminho de uma evidência tal como aparece ao percorrer o disco.

    As evidências guardam o caminho relativo à raiz da aplicação (`uploads/…`) e o
    disco lista-se em absoluto (`/app/uploads/…`). Comparados sem alinhar, todos os
    ficheiros com evidência saíam como órfãos e nenhum apagado era descontado.
    """
    p = Path(caminho)
    return str(p if p.is_absolute() else _UPLOADS_DIR.parent / p)


def _reconciliar_evidencias(db, a_remover: frozenset[str] = frozenset()) -> dict:
    """Evidência na BD sem ficheiro em uploads → soft-delete da referência
    (reversível, preserva os metadados-prova) com uma entrada de auditoria
    por evidência; ficheiros órfãos (sem linha na BD) são
    apenas REPORTADOS — podem ser a única cópia de uma prova. Num restauro
    completo o esperado é zero discrepâncias (verificação de integridade grátis).

    Escritas interrompidas (`.parcial-`) não contam como órfãs: sabe-se o que
    são, e reportá-las diluiria o sinal que esta contagem existe para dar."""
    from sqlalchemy import text
    from sqlmodel import select
    from app.evidencias.models import Evidencia
    from app.shared.audit import Acao, registar_acao

    ativos = db.exec(
        select(Evidencia).where(
            Evidencia.deleted_at.is_(None), Evidencia.ficheiro_path.is_not(None)
        )
    ).all()
    soft_deleted = 0
    for ev in ativos:
        if Path(_no_disco(ev.ficheiro_path)).is_file():
            continue
        ev.deleted_at = datetime.now(timezone.utc)
        db.add(ev)
        # Sem o caminho nem o título: o caminho leva o nome original do ficheiro,
        # e os dois podem ser dados pessoais que a trilha nunca mais largaria.
        registar_acao(
            db, acao=Acao.EVIDENCIA_RECONCILIADA, empresa_id=ev.empresa_id,
            entidade_tipo="Evidencia", entidade_id=ev.id,
            dados_novos={"motivo": "ficheiro_em_falta_apos_restauro",
                         "conteudo_hash": ev.conteudo_hash},
        )
        soft_deleted += 1

    # Órfãos: qualquer linha (mesmo soft-deleted) ainda "reclama" o seu ficheiro.
    referenciados = {
        _no_disco(p) for p in db.execute(
            text("SELECT ficheiro_path FROM evidencias WHERE ficheiro_path IS NOT NULL")
        ).scalars()
    }
    a_remover = frozenset(_no_disco(p) for p in a_remover)
    # Os `.parcial-` ficam de fora: são escritas de evidência interrompidas antes
    # da publicação do ficheiro, e sabe-se o que são. Contá-los como órfãos daria
    # a entender que podem ser a única cópia de uma prova — que é o motivo de os
    # órfãos serem reportados em vez de apagados — e enchia o relatório de ruído.
    # Os ficheiros de evidências que o registo de apagamentos voltou a apagar
    # também ficam de fora: saem do disco logo a seguir ao commit.
    orfaos = [
        str(p) for p in _UPLOADS_DIR.rglob("*")
        if p.is_file()
        and str(p) not in referenciados
        and str(p) not in a_remover
        and not p.name.startswith(".parcial-")
    ]
    return {
        "evidencias_soft_delete": soft_deleted,
        "ficheiros_orfaos": len(orfaos),
        "ficheiros_orfaos_lista": orfaos[:50],
    }


def finalizar_restauro_no_arranque() -> None:
    """Corre no lifespan, DEPOIS de o entrypoint ter migrado a base. Sem restauro
    pendente limita-se a limpar uma flag de manutenção órfã (auto-cura)."""
    if not _PENDENTE_FILE.exists():
        manutencao.desativar()
        return

    from sqlalchemy import text
    from sqlmodel import Session
    from app.database import engine
    from app.shared.audit import Acao, registar_acao

    pendente = json.loads(_PENDENTE_FILE.read_text())
    logger.info("A finalizar restauro de %s…", pendente.get("ficheiro"))

    from app.evidencias import apagamentos

    with Session(engine) as db:
        # Antes da reconciliação: o que se volta a apagar sai com o ficheiro, e
        # não deve ser contado como "ficheiro em falta".
        reaplicacao = apagamentos.reaplicar(db)
        reconciliacao = _reconciliar_evidencias(
            db, frozenset(p for p in reaplicacao["ficheiros"] if p)
        )
        reconciliacao["apagamentos_reaplicados"] = reaplicacao["reaplicados"]
        verificacoes = {
            "alembic_rev": db.execute(text("SELECT version_num FROM alembic_version")).scalar(),
            "empresas": db.execute(text("SELECT count(*) FROM empresas")).scalar(),
            "utilizadores": db.execute(text("SELECT count(*) FROM utilizadores")).scalar(),
        }
        resumo = {**pendente, "reconciliacao": reconciliacao, "verificacoes": verificacoes,
                  "concluido_em": datetime.now(timezone.utc).isoformat()}
        # A trilha de cada empresa diz que a instalação foi restaurada, de onde e
        # quando — e mais nada. O resumo completo é da instalação: a lista de órfãos
        # traz caminhos e nomes originais de ficheiros de TODAS as empresas, e a
        # trilha, encadeada, não os largaria nunca. Fica em restauro-relatorio.json.
        na_trilha = {k: resumo.get(k) for k in (
            "ficheiro", "modo", "app_version_backup", "alembic_rev_backup", "premium",
            "maquina_nova", "executado_por", "iniciado_em", "concluido_em",
        )}
        for empresa_id in db.execute(text("SELECT id FROM empresas")).scalars():
            registar_acao(db, acao=Acao.RESTAURO_EXECUTADO, empresa_id=empresa_id,
                          dados_novos=na_trilha)
        db.commit()
    apagamentos.remover_ficheiros(reaplicacao["ficheiros"])

    _RELATORIO_FILE.write_text(json.dumps(resumo, indent=1))
    _PENDENTE_FILE.unlink(missing_ok=True)
    manutencao.desativar()
    logger.info(
        "Restauro finalizado: %s empresas, %s utilizadores, %s evidências reconciliadas, "
        "%s ficheiros órfãos. Relatório em %s.",
        verificacoes["empresas"], verificacoes["utilizadores"],
        reconciliacao["evidencias_soft_delete"], reconciliacao["ficheiros_orfaos"],
        _RELATORIO_FILE,
    )


if __name__ == "__main__":
    sys.exit(main())
