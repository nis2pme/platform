"""
Backups da instalação (on-prem) — criação, cifra e gestão.

Formato do ficheiro `.nbk` (ver `cifra.py`):
    linha 1: cabeçalho JSON em claro (formato, modo, data, recipiente, chave embrulhada)
    resto:   age( tar.gz( manifest.json, core.dump, uploads/?, data/, env ) ), por partes

Segurança: o backup contém TODOS os segredos da instalação (auto-secrets.env,
.env com passwords) — por isso a cifra autenticada é obrigatória e não existe
backup sem passphrase definida. O servidor guarda só a parte pública da chave
(para os backups agendados) e a identidade embrulhada pela passphrase, que
viaja também no cabeçalho de cada ficheiro — um restauro em máquina virgem só
precisa do ficheiro + passphrase.

Regras de compatibilidade: o campo `formato` só incrementa
quando o layout muda, e o leitor terá de aceitar sempre formatos anteriores.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlmodel import Session, select

from app.backup import cifra

logger = logging.getLogger(__name__)

# Diretórios/ficheiros da instalação (fixos na imagem — ver Dockerfile).
_UPLOADS_DIR = Path("/app/uploads")
_DATA_DIR = Path("/app/data")
_ENV_FILE = Path("/app/.env")
BACKUPS_DIR = _DATA_DIR / "backups"
# Parte pública + identidade embrulhada pela passphrase (mode 600). Vai DENTRO do
# backup (data/) — inócuo: sem a passphrase não abre nada, e assim os agendados
# retomam automaticamente após um restauro.
_CHAVE_FILE = _DATA_DIR / "backup-chave.json"

_PASSPHRASE_MIN = 12
_FORMATO = cifra.FORMATO

# Um backup em curso deixa este testemunho e apaga-o ao acabar, bem ou mal. Se o
# processo for morto a meio (falta de memória), o testemunho fica: no arranque
# seguinte limpa-se o que ficou e o agendado não volta a tentar nesse dia —
# senão o arranque tentava de novo, morria de novo, e cada volta deixava uma
# cópia inteira do arquivo no disco (medido: 122 MB por volta, 19 voltas em 10 min).
_EM_CURSO_FILE = _DATA_DIR / "backup-em-curso.json"
_INTERROMPIDO_FILE = _DATA_DIR / "backup-interrompido.json"

# Estado de uma execução a decorrer (backup, restauro, manutenção): descreve esta
# máquina neste instante, não os dados. Não entra no backup — restaurado, o
# testemunho do próprio backup fazia o arranque seguinte julgá-lo interrompido,
# avisar os administradores e saltar o agendado desse dia.
_ESTADO_DE_EXECUCAO = frozenset({
    "backup-em-curso.json", "backup-interrompido.json",
    "manutencao.flag", "restauro-pendente.json",
})

_MODOS = ("completo", "so_db")


# ---------------------------------------------------------------------------
# Passphrase / chave
# ---------------------------------------------------------------------------

def _gravar_chave(dados: dict) -> None:
    _CHAVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporario = _CHAVE_FILE.with_suffix(".tmp")
    temporario.write_text(json.dumps(dados))
    temporario.chmod(0o600)
    temporario.replace(_CHAVE_FILE)


def _ler_chave() -> dict | None:
    """O ficheiro de chave, ou None se não houver um utilizável.

    Os builds de pré-lançamento guardavam aqui a chave derivada da passphrase
    (que abria todos os backups). Esse ficheiro não serve: conta como passphrase
    por definir — o aviso aos administradores pede-a de novo, e a nova escreve
    o formato atual por cima."""
    try:
        dados = json.loads(_CHAVE_FILE.read_text())
    except (OSError, ValueError):
        return None
    if dados.get("versao") != 2 or "recipiente" not in dados or "embrulho" not in dados:
        return None
    # Uma chave com um custo que o restauro recusaria faria backups que não abrem.
    try:
        if not cifra.N_LOG2_MIN <= int(dados["embrulho"]["n_log2"]) <= cifra.N_LOG2_MAX:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return dados


def passphrase_definida() -> bool:
    return _ler_chave() is not None


def definir_passphrase(passphrase: str) -> None:
    """Define (ou substitui) a passphrase dos backups. Backups antigos continuam
    a abrir com a passphrase antiga — cada um leva no cabeçalho a sua identidade
    embrulhada."""
    if len(passphrase) < _PASSPHRASE_MIN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"codigo": "passphrase_curta", "minimo": _PASSPHRASE_MIN},
        )
    dados = cifra.nova_chave(passphrase)
    dados["definida_em"] = datetime.now(timezone.utc).isoformat()
    _gravar_chave(dados)


def _chave_publica() -> tuple[str, dict]:
    """Devolve (recipiente, identidade embrulhada). 400 se a passphrase não está definida."""
    dados = _ler_chave()
    if dados is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"codigo": "passphrase_nao_definida"},
        )
    return dados["recipiente"], dados["embrulho"]


# ---------------------------------------------------------------------------
# Estimativa e verificação de espaço
# ---------------------------------------------------------------------------

def _tamanho_dir(caminho: Path, excluir: Path | None = None) -> int:
    total = 0
    if not caminho.is_dir():
        return 0
    for raiz, dirs, ficheiros in os.walk(caminho):
        if excluir is not None and Path(raiz) == excluir:
            dirs[:] = []  # não descer ao diretório excluído
            continue
        for f in ficheiros:
            try:
                total += (Path(raiz) / f).stat().st_size
            except OSError:
                continue
    return total


def estimar_tamanho(db: Session, modo: str) -> int:
    """Estimativa conservadora do backup por cifrar (bytes)."""
    tamanho_db = db.execute(text("SELECT pg_database_size(current_database())")).scalar() or 0
    total = int(tamanho_db)
    total += _tamanho_dir(_DATA_DIR, excluir=BACKUPS_DIR)
    if modo == "completo":
        total += _tamanho_dir(_UPLOADS_DIR)
    if _ENV_FILE.exists():
        total += _ENV_FILE.stat().st_size
    return total


def _verificar_espaco(estimativa: int) -> None:
    """Exige ≥2× a estimativa livre (o processo usa staging + ficheiro final)."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    livre = shutil.disk_usage(BACKUPS_DIR).free
    necessario = max(estimativa * 2, 100 * 1024 * 1024)
    if livre < necessario:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail={
                "codigo": "espaco_insuficiente",
                "livre_mb": round(livre / 1_048_576),
                "necessario_mb": round(necessario / 1_048_576),
            },
        )


# ---------------------------------------------------------------------------
# Retenção e listagem
# ---------------------------------------------------------------------------

def _registar_modelos() -> None:
    """O mapper da `Empresa` refere o `Utilizador` pelo nome. Dentro da app o
    import já aconteceu; nos comandos de linha (backup antes de atualizar,
    backup de segurança do restauro) ninguém o fazia, e a primeira consulta à
    `Empresa` rebentava — o restauro com backup de segurança falhava sempre."""
    import app.auth.models  # noqa: F401


def retencao_em_vigor(db: Session) -> int:
    """Quantas cópias se guardam: a política da empresa, ou a da instalação.

    O backup é da instalação, e on-prem tem uma empresa só — é a política dela que
    manda. O número gravado nas Definições («Cópias de segurança a guardar») não
    era lido por ninguém: valia sempre o `BACKUP_RETENCAO` do `.env`.
    """
    _registar_modelos()
    from app.empresas.models import Empresa
    from app.shared.politica_seguranca import politica

    empresa_id = db.exec(select(Empresa.id).order_by(Empresa.created_at)).first()
    return max(1, politica(db, empresa_id).backup_retencao_dias)


def _aplicar_retencao(db: Session) -> int:
    """Apaga os backups mais antigos até sobrarem `retenção` cópias.

    Corre DEPOIS de a cópia nova estar gravada: correr antes deixava a instalação
    sem nenhuma cópia quando a geração falhava. O disco não enche por isso — o
    espaço para a cópia nova já foi confirmado antes de gerar. A mais recente
    nunca é apagada."""
    retencao = max(1, retencao_em_vigor(db))
    ficheiros = sorted(
        BACKUPS_DIR.glob("nis2pme-backup-*.nbk"),
        key=lambda f: f.stat().st_mtime,
    )
    apagados = 0
    while len(ficheiros) > retencao:
        antigo = ficheiros.pop(0)
        antigo.unlink(missing_ok=True)
        apagados += 1
        logger.info("Retenção de backups: apagado %s.", antigo.name)
    return apagados


def listar_backups() -> list[dict]:
    if not BACKUPS_DIR.is_dir():
        return []
    out = []
    for f in sorted(BACKUPS_DIR.glob("nis2pme-backup-*.nbk"), reverse=True):
        st = f.stat()
        cab = _ler_cabecalho(f)
        out.append({
            "ficheiro": f.name,
            "tamanho_mb": round(st.st_size / 1_048_576, 1),
            "criado_em": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "modo": cab.get("modo", "?"),
        })
    return out


def _ler_cabecalho(caminho: Path) -> dict:
    try:
        with caminho.open("rb") as f:
            return json.loads(f.readline().decode("utf-8"))
    except (OSError, ValueError):
        return {}


def caminho_backup(nome: str) -> Path:
    """Resolve o nome para dentro de BACKUPS_DIR (anti path-traversal, CWE-22)."""
    if "/" in nome or "\\" in nome or not nome.startswith("nis2pme-backup-") or not nome.endswith(".nbk"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"codigo": "backup_nao_encontrado"})
    caminho = (BACKUPS_DIR / nome).resolve()
    if caminho.parent != BACKUPS_DIR.resolve() or not caminho.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"codigo": "backup_nao_encontrado"})
    return caminho


_FOLGA_UPLOAD = 100 * 1024 * 1024


def verificar_espaco_para_upload(content_length: str | None) -> None:
    """Recusa (507) um upload que não cabe no disco, antes de o receber.

    O ficheiro passa por dois sítios: o temporário onde o servidor o recebe e a
    cópia em BACKUPS_DIR. Se forem o mesmo disco, precisa do dobro. Sem
    `Content-Length` não há como saber antes: fica o teto de corpo da rota e o 507
    da cópia (`guardar_upload`). Um disco cheio a meio de um upload de 4 GiB
    parava também a base de dados que vive nele.
    """
    try:
        tamanho = int(content_length) if content_length is not None else None
    except ValueError:
        tamanho = None
    if not tamanho or tamanho < 0:
        return
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    necessario_por_disco: dict[int, int] = {}
    livre_por_disco: dict[int, int] = {}
    for pasta in (Path(tempfile.gettempdir()), BACKUPS_DIR):
        disco = pasta.stat().st_dev
        necessario_por_disco[disco] = necessario_por_disco.get(disco, _FOLGA_UPLOAD) + tamanho
        livre_por_disco[disco] = shutil.disk_usage(pasta).free
    for disco, necessario in necessario_por_disco.items():
        if livre_por_disco[disco] < necessario:
            raise HTTPException(
                status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                detail={
                    "codigo": "espaco_insuficiente",
                    "livre_mb": round(livre_por_disco[disco] / 1_048_576),
                    "necessario_mb": round(necessario / 1_048_576),
                },
            )


def guardar_upload(origem, nome_original: str | None) -> str:
    """Guarda um .nbk enviado pela UI (ex.: descarregado de outro servidor) em
    BACKUPS_DIR, para poder ser inspecionado/restaurado como os locais. Só
    aceita ficheiros cuja 1.ª linha é um cabeçalho .nbk plausível — a validação
    a sério (cifra autenticada) acontece na inspeção, com a passphrase."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    nome = Path(nome_original or "").name
    if not (nome.startswith("nis2pme-backup-") and nome.endswith(".nbk")):
        nome = f"nis2pme-backup-importado-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.nbk"
    destino = BACKUPS_DIR / nome
    if destino.exists():  # nunca sobrepor um backup existente
        nome = f"{nome[:-4]}-{os.urandom(2).hex()}.nbk"
        destino = BACKUPS_DIR / nome

    temporario = destino.with_suffix(".parcial")
    try:
        with temporario.open("wb") as f:
            shutil.copyfileobj(origem, f, 1024 * 1024)
    except OSError:
        temporario.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail={"codigo": "espaco_insuficiente"},
        )
    cabecalho = _ler_cabecalho(temporario)
    formato = cabecalho.get("formato")
    if formato != _FORMATO or "chave" not in cabecalho:
        temporario.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"codigo": "ficheiro_invalido"},
        )
    temporario.rename(destino)
    logger.info("Backup importado pela UI: %s.", nome)
    return nome


# ---------------------------------------------------------------------------
# Criação
# ---------------------------------------------------------------------------

def _sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def separar_credencial(url: str) -> tuple[str, dict[str, str]]:
    """
    Parte uma URL de ligação em (URL sem password, ambiente com `PGPASSWORD`).

    Passar a URL completa como argumento de `pg_dump`/`pg_restore` deixa a password
    em `/proc/<pid>/cmdline`, visível a qualquer processo do contentor e a um `ps`
    durante o backup. O ambiente do subprocesso não fica nessa lista.

    A password vem **percent-encoded** dentro da URL (`%40` para `@`) e o PGPASSWORD
    quer o valor literal, portanto tem de ser descodificada. Falhar nisto partia o
    backup de quem tem caracteres especiais na password — as geradas pelo instalador
    são hexadecimais, mas o `.env` pode ser editado à mão.

    Uma URL sem password volta inalterada e sem ambiente extra.
    """
    from urllib.parse import unquote, urlsplit, urlunsplit

    partes = urlsplit(url)
    if not partes.password:
        return url, {}

    anfitriao = partes.hostname or ""
    if partes.port:
        anfitriao = f"{anfitriao}:{partes.port}"
    if partes.username:
        anfitriao = f"{partes.username}@{anfitriao}"

    sem_password = urlunsplit(
        (partes.scheme, anfitriao, partes.path, partes.query, partes.fragment)
    )
    # O `urlsplit` NÃO descodifica: `.password` devolve o texto tal como está na URL.
    # O `unquote` é obrigatório — sem ele, uma password com `@` chegava ao Postgres
    # como "%40" e a autenticação falhava.
    # O `username` fica por descodificar de propósito: volta para dentro da URL, onde
    # tem de continuar codificado.
    return sem_password, {"PGPASSWORD": unquote(partes.password)}


def _pg_dump(destino: Path) -> None:
    """pg_dump -Fc da core-db para `destino`. A credencial vai no ambiente, não em argv."""
    from app.config import get_settings
    url, credencial = separar_credencial(get_settings().DATABASE_URL)
    resultado = subprocess.run(
        ["pg_dump", "--format=custom", "--file", str(destino), f"--dbname={url}"],
        capture_output=True, text=True, timeout=1800,
        env={**os.environ, **credencial},
    )
    if resultado.returncode != 0:
        # stderr do pg_dump pode conter o host/porta mas nunca a password — ok para log.
        logger.error("pg_dump falhou: %s", resultado.stderr.strip()[:500])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"codigo": "dump_falhou"},
        )


def _premium_dump(destino: Path) -> bool:
    """
    Pede ao sidecar o dump da premium-data-db (BackupService.ExportarDados) e
    grava-o em `destino`. Best-effort, como a purga: sem sidecar (ou com erro),
    o backup segue SEM o componente premium — o manifest reflete a ausência.
    """
    from app.config import get_settings
    settings = get_settings()
    if not settings.PREMIUM_ENABLED or not settings.PREMIUM_SIDECAR_ADDR:
        return False
    try:
        import grpc
        from app.premium.client import criar_canal_sidecar
        from app.premium.proto import premium_pb2, premium_pb2_grpc

        canal = criar_canal_sidecar(grpc, settings.PREMIUM_SIDECAR_ADDR)
        try:
            stub = premium_pb2_grpc.BackupServiceStub(canal)
            with destino.open("wb") as f:
                for chunk in stub.ExportarDados(premium_pb2.BackupExportReq(), timeout=1800):
                    f.write(chunk.dados)
        finally:
            canal.close()
        return True
    except Exception:  # noqa: BLE001 — premium ausente não pode impedir o backup do core
        logger.warning("Componente premium indisponível — o backup segue sem premium.dump.", exc_info=True)
        destino.unlink(missing_ok=True)
        return False


def criar_backup(db: Session, modo: str) -> dict:
    """
    Cria um backup cifrado em BACKUPS_DIR e devolve os metadados.
    Ordem: passphrase → estimativa → espaço → gerar → cifrar → gravar → retenção.
    """
    if modo not in _MODOS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"codigo": "modo_invalido"})

    recipiente, embrulho = _chave_publica()
    estimativa = estimar_tamanho(db, modo)
    _verificar_espaco(estimativa)

    _EM_CURSO_FILE.write_text(json.dumps({"inicio": datetime.now(timezone.utc).isoformat(), "modo": modo}))
    try:
        resultado = _gerar_backup(db, modo, recipiente, embrulho)
    finally:
        _EM_CURSO_FILE.unlink(missing_ok=True)
    _aplicar_retencao(db)
    _resolver_interrupcao(db)
    return resultado


def _resolver_interrupcao(db: Session) -> None:
    """Um backup acabou bem: o aviso de backup interrompido deixou de ser
    verdade, e o agendado volta ao normal."""
    if not _INTERROMPIDO_FILE.exists():
        return
    _INTERROMPIDO_FILE.unlink(missing_ok=True)
    _registar_modelos()
    from app.empresas.models import Empresa
    from app.notificacoes.catalogo import Codigo
    from app.notificacoes.service import marcar_lidas_por_chave_prefixo

    for empresa_id in db.exec(select(Empresa.id)).all():
        marcar_lidas_por_chave_prefixo(db, prefixo=Codigo.BACKUP_INTERROMPIDO, empresa_id=empresa_id)
    db.commit()


def _gerar_backup(db: Session, modo: str, recipiente: str, embrulho: dict) -> dict:

    agora = datetime.now(timezone.utc)
    alembic_rev = db.execute(text("SELECT version_num FROM alembic_version")).scalar()

    from app.config import get_settings
    with tempfile.TemporaryDirectory(dir=BACKUPS_DIR, prefix=".tmp-") as tmp:
        tmp_dir = Path(tmp)

        dump = tmp_dir / "core.dump"
        _pg_dump(dump)

        # Componente premium (inventário/risco/fornecedores/IA): o SIDECAR faz o
        # dump da base dele e envia-o por gRPC — o core nunca toca na premium-db.
        premium_dump = tmp_dir / "premium.dump"
        tem_premium = _premium_dump(premium_dump)

        manifest = {
            "formato": _FORMATO,
            "modo": modo,
            "app_version": get_settings().APP_VERSION,
            "alembic_rev": alembic_rev,
            "criado_em": agora.isoformat(),
            "componentes": {
                "core": True,
                "premium": tem_premium,
                "uploads": modo == "completo",
                "data": True,
                "env": _ENV_FILE.exists(),
            },
            "checksums": {"core.dump": _sha256(dump)},
        }
        if tem_premium:
            manifest["checksums"]["premium.dump"] = _sha256(premium_dump)
        manifest_path = tmp_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=1))

        tar_path = tmp_dir / "conteudo.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(manifest_path, arcname="manifest.json")
            tar.add(dump, arcname="core.dump")
            if tem_premium:
                tar.add(premium_dump, arcname="premium.dump")
            if _DATA_DIR.is_dir():
                # data/ SEM o diretório de backups (recursão), staging temporário
                # nem o estado de execução.
                for item in _DATA_DIR.iterdir():
                    if item.resolve() == BACKUPS_DIR.resolve() or item.name in _ESTADO_DE_EXECUCAO:
                        continue
                    tar.add(item, arcname=f"data/{item.name}")
            if modo == "completo" and _UPLOADS_DIR.is_dir():
                tar.add(_UPLOADS_DIR, arcname="uploads")
            if _ENV_FILE.exists():
                tar.add(_ENV_FILE, arcname="env")

        # Cifra autenticada por partes (age): memória constante qualquer que seja
        # o tamanho, e um bloco adulterado é recusado na leitura. O cabeçalho é
        # informativo — o que manda no restauro é o manifest, dentro do payload.
        cabecalho = json.dumps({
            "formato": _FORMATO,
            "modo": modo,
            "app_version": get_settings().APP_VERSION,
            "criado_em": agora.isoformat(),
            "recipiente": recipiente,
            "chave": embrulho,
        }).encode("utf-8")

        nome = f"nis2pme-backup-{agora.strftime('%Y%m%d-%H%M%S')}.nbk"
        final_tmp = tmp_dir / nome
        with final_tmp.open("wb") as f:
            f.write(cabecalho + b"\n")
            cifra.cifrar(tar_path, f, recipiente)
        # Escrita atómica: só aparece em BACKUPS_DIR quando está completo.
        destino = BACKUPS_DIR / nome
        shutil.move(str(final_tmp), destino)

    st = destino.stat()
    logger.info("Backup criado: %s (%.1f MB, modo=%s).", nome, st.st_size / 1_048_576, modo)
    return {
        "ficheiro": nome,
        "tamanho_mb": round(st.st_size / 1_048_576, 1),
        "criado_em": agora.isoformat(),
        "modo": modo,
    }


# ---------------------------------------------------------------------------
# Backup agendado (diário, ligado por defeito)
# ---------------------------------------------------------------------------

_AGENDADO_FILE = _DATA_DIR / "backup-agendado.json"
# Sem ficheiro = comportamento de fábrica: ativo, às 03:00. O ficheiro vai
# dentro de data/ nos backups — após um restauro o agendamento retoma sozinho.
_AGENDADO_DEFAULTS = {"ativo": True, "hora": 3}
# O mercado-alvo é português e os containers correm em UTC — a hora agendada
# é interpretada na hora de Portugal continental (com fallback para UTC).
_FUSO_LOCAL = "Europe/Lisbon"


def _agora_local() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(_FUSO_LOCAL))
    except Exception:  # noqa: BLE001 — sem tzdata, UTC (desvio máx. 1h em PT)
        return datetime.now(timezone.utc)


def obter_agendado() -> dict:
    """Configuração do backup agendado (defaults quando o ficheiro não existe
    ou está corrompido — nunca falha, para não derrubar o tick nem o GET)."""
    try:
        dados = json.loads(_AGENDADO_FILE.read_text())
    except (OSError, ValueError):
        return dict(_AGENDADO_DEFAULTS)
    hora = dados.get("hora")
    return {
        "ativo": bool(dados.get("ativo", True)),
        "hora": hora if isinstance(hora, int) and 0 <= hora <= 23 else _AGENDADO_DEFAULTS["hora"],
    }


def definir_agendado(ativo: bool, hora: int) -> dict:
    if not 0 <= hora <= 23:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail={"codigo": "hora_invalida"}
        )
    cfg = {"ativo": bool(ativo), "hora": int(hora)}
    _AGENDADO_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AGENDADO_FILE.write_text(json.dumps(cfg))
    return cfg


def _ha_backup_de_hoje(agora_local: datetime) -> bool:
    """O objetivo do agendado é «pelo menos um backup por dia» — um backup
    manual feito hoje também conta, e nesse dia o tick não duplica."""
    meia_noite = agora_local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    try:
        return any(
            f.stat().st_mtime >= meia_noite
            for f in BACKUPS_DIR.glob("nis2pme-backup-*.nbk")
        )
    except OSError:
        return False


_aviso_passphrase_dia: str | None = None  # anti-spam do log: 1 aviso por dia


def _avisar_administradores_sem_passphrase(db: Session) -> None:
    """Aviso na caixa de quem pode definir a frase-secreta.

    O registo do servidor e um cartão nas Definições não chegam: quem nunca abre
    essa página não sabe que a instalação está sem cópias. A deduplicação do
    catálogo impede repetições enquanto o aviso estiver por ler; sai quando a
    frase-secreta é definida.
    """
    from app.notificacoes.catalogo import Codigo

    _avisar_administradores(db, Codigo.BACKUP_SEM_PASSPHRASE)


def _avisar_administradores(db: Session, codigo: str) -> None:
    """Uma notificação a cada administrador ativo (o catálogo deduplica)."""
    from app.auth.models import RoleUtilizador, Utilizador
    from app.notificacoes.service import criar_notificacao

    administradores = db.exec(
        select(Utilizador).where(
            Utilizador.role == RoleUtilizador.ADMIN,
            Utilizador.ativo.is_(True),  # type: ignore[union-attr]
        )
    ).all()
    for admin in administradores:
        criar_notificacao(
            db, empresa_id=admin.empresa_id, utilizador_id=admin.id,
            codigo=codigo,
        )
    db.commit()


def _limpar_temporarios() -> int:
    """Apaga os diretórios de trabalho que um backup morto a meio deixou."""
    apagados = 0
    for pasta in BACKUPS_DIR.glob(".tmp-*"):
        if pasta.is_dir():
            shutil.rmtree(pasta, ignore_errors=True)
            apagados += 1
    return apagados


def recuperar_backup_interrompido(db: Session) -> bool:
    """No arranque: se um backup ficou a meio (o processo foi morto), limpa o que
    ele deixou, suspende o agendado nesse dia e avisa quem administra.
    Devolve True se havia um backup interrompido."""
    if not _EM_CURSO_FILE.exists():
        return False
    try:
        info = json.loads(_EM_CURSO_FILE.read_text())
    except (OSError, ValueError):
        info = {}
    apagados = _limpar_temporarios()
    _INTERROMPIDO_FILE.write_text(json.dumps({
        "dia": _agora_local().strftime("%Y-%m-%d"),
        "inicio": info.get("inicio"),
    }))
    _EM_CURSO_FILE.unlink(missing_ok=True)
    logger.error(
        "Backup interrompido (iniciado em %s): o processo terminou a meio — falta de memória? "
        "%d diretório(s) de trabalho apagado(s); o agendado não volta a tentar hoje.",
        info.get("inicio"), apagados,
    )
    from app.notificacoes.catalogo import Codigo

    _avisar_administradores(db, Codigo.BACKUP_INTERROMPIDO)
    return True


def _interrompido_hoje(agora_local: datetime) -> bool:
    try:
        return json.loads(_INTERROMPIDO_FILE.read_text()).get("dia") == agora_local.strftime("%Y-%m-%d")
    except (OSError, ValueError):
        return False


def executar_backup_agendado(db: Session) -> bool:
    """
    Corpo do tick horário (main.py): quando a hora local chega à agendada e
    ainda não há backup de hoje, cria um backup completo. Falhas (ex.: disco)
    propagam-se ao tick, que as regista — sem backup criado, o ciclo seguinte
    tenta de novo. Devolve True se criou um backup neste ciclo.
    """
    global _aviso_passphrase_dia
    cfg = obter_agendado()
    if not cfg["ativo"]:
        return False
    agora = _agora_local()
    if agora.hour < cfg["hora"] or _ha_backup_de_hoje(agora) or _interrompido_hoje(agora):
        return False
    if not passphrase_definida():
        dia = agora.strftime("%Y-%m-%d")
        if _aviso_passphrase_dia != dia:
            _aviso_passphrase_dia = dia
            logger.warning(
                "Backup agendado ativo mas sem passphrase definida — "
                "defina-a em Definições → Sistema para os backups arrancarem."
            )
        _avisar_administradores_sem_passphrase(db)
        return False

    resultado = criar_backup(db, "completo")

    # Auditoria por empresa (on-prem = normalmente uma): a UI de auditoria
    # filtra por empresa e uma entrada de sistema órfã ficaria invisível.
    from app.shared.audit import Acao, registar_acao
    for empresa_id in db.execute(text("SELECT id FROM empresas")).scalars():
        registar_acao(
            db, acao=Acao.BACKUP_CRIADO, empresa_id=empresa_id,
            dados_novos={**resultado, "agendado": True},
        )
    db.commit()
    return True
