"""Apagar o conteúdo de uma evidência, e lembrar-se disso depois de um restauro.

Duas peças que todos os caminhos de apagamento partilham:

* `limpar_conteudo` tira da linha tudo o que é conteúdo — ficheiro, texto, nome do
  ficheiro e título — e deixa só o que prova que existiu: o identificador, a
  impressão digital (`conteudo_hash`), o tipo e o tamanho. Apagar metade (o
  ficheiro, mas não o texto da nota nem o nome) deixava na base precisamente o que
  um apagamento existe para tirar.

* O **registo de apagamentos** é um ficheiro `jsonl` fora da base. Um restauro põe
  a base como estava na data do backup, e com ela voltariam as evidências apagadas
  depois dessa data — incluindo as que saíram a pedido do titular dos dados. O
  restauro relê este registo e volta a aplicar os apagamentos. Cada linha leva só
  identificadores, a impressão digital, o tipo e o código da razão: nunca conteúdo
  nem texto livre, que podem conter os dados que se apagaram.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.evidencias import ligacoes
from app.evidencias.models import Evidencia

logger = logging.getLogger(__name__)

# Fica em data/, que vai dentro dos backups. O restauro sobrepõe data/ com a cópia
# do backup, por isso lê este ficheiro ANTES e une as duas versões depois.
REGISTO = Path("/app/data") / "apagamentos.jsonl"

TIPO_DEFINITIVO = "definitivo"
TIPO_LAPIDE = "lapide"


def limpar_conteudo(evidencia: Evidencia, agora: datetime | None = None) -> str | None:
    """Tira o conteúdo da linha. Devolve o caminho do ficheiro a remover.

    O ficheiro só se remove depois do commit (`remover_ficheiros`): se a
    transação falhar, a linha continua viva e o ficheiro tem de lá estar.
    """
    caminho = evidencia.ficheiro_path
    evidencia.deleted_at = evidencia.deleted_at or agora or datetime.now(timezone.utc)
    evidencia.titulo = None
    evidencia.conteudo_texto = None
    evidencia.conteudo_texto_cifrado = False
    evidencia.ficheiro_nome = None
    # O caminho em disco leva o nome original do ficheiro.
    evidencia.ficheiro_path = None
    return caminho


def remover_ficheiros(caminhos: list[str | None]) -> None:
    for caminho in caminhos:
        if not caminho:
            continue
        try:
            os.remove(caminho)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Não foi possível remover o ficheiro de uma evidência apagada.")


def registar(
    evidencias: list[Evidencia],
    *,
    tipo: str,
    codigo: str,
    por_id: uuid.UUID | None,
) -> None:
    """Acrescenta os apagamentos ao registo. Corre ANTES do commit.

    Se o registo não se puder escrever, o apagamento não acontece: um apagamento
    que um restauro desfaz em silêncio é pior do que um erro à vista.
    """
    agora = datetime.now(timezone.utc).isoformat()
    linhas = "".join(
        json.dumps(
            {
                "v": 1,
                "evidencia_id": str(ev.id),
                "empresa_id": str(ev.empresa_id),
                "conteudo_hash": ev.conteudo_hash,
                "tipo": tipo,
                "codigo": codigo,
                "por_id": str(por_id) if por_id else None,
                "em": agora,
            },
            ensure_ascii=False,
        )
        + "\n"
        for ev in evidencias
    )
    try:
        REGISTO.parent.mkdir(parents=True, exist_ok=True)
        with open(REGISTO, "a", encoding="utf-8") as ficheiro:
            ficheiro.write(linhas)
            ficheiro.flush()
            os.fsync(ficheiro.fileno())
    except OSError as exc:
        logger.error("Não foi possível escrever o registo de apagamentos: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Não foi possível registar o apagamento. Nada foi apagado.",
        ) from exc


def ler_linhas() -> list[str]:
    try:
        return [
            linha for linha in REGISTO.read_text(encoding="utf-8").splitlines()
            if linha.strip()
        ]
    except FileNotFoundError:
        return []


def unir(anteriores: list[str]) -> None:
    """Junta ao registo que veio do backup as linhas que existiam antes do restauro.

    Chamado pelo restauro depois de sobrepor data/: sem isto, o registo antigo do
    backup substituía o atual e os apagamentos posteriores ao backup perdiam-se
    exatamente quando fazem falta.
    """
    atuais = ler_linhas()
    vistas = set(atuais)
    juntas = atuais + [linha for linha in anteriores if linha not in vistas]
    if juntas == atuais:
        return
    temporario = REGISTO.with_name(REGISTO.name + ".tmp")
    temporario.write_text("".join(linha + "\n" for linha in juntas), encoding="utf-8")
    os.replace(temporario, REGISTO)


def reaplicar(db: Session) -> dict:
    """Volta a apagar o que o registo diz que foi apagado. Não faz commit.

    Idempotente: o que já está apagado não se toca. Devolve a contagem e os
    ficheiros a remover depois do commit.
    """
    from app.shared.audit import Acao, registar_acao

    entradas: dict[uuid.UUID, dict] = {}
    for linha in ler_linhas():
        try:
            entrada = json.loads(linha)
            entradas[uuid.UUID(entrada["evidencia_id"])] = entrada
        except (ValueError, KeyError, TypeError):
            logger.warning("Linha ilegível no registo de apagamentos ignorada.")
    if not entradas:
        return {"reaplicados": 0, "ficheiros": []}

    ficheiros: list[str | None] = []
    reaplicados = 0
    for evidencia in db.exec(
        select(Evidencia).where(Evidencia.id.in_(list(entradas)))
    ).all():
        entrada = entradas[evidencia.id]
        ja_apagada = (
            evidencia.deleted_at is not None
            and evidencia.conteudo_texto is None
            and evidencia.ficheiro_path is None
            and evidencia.titulo is None
        )
        if ja_apagada:
            continue

        ficheiros.append(limpar_conteudo(evidencia))
        if entrada.get("tipo") == TIPO_LAPIDE:
            evidencia.eliminacao_rgpd = True
        if entrada.get("por_id"):
            try:
                evidencia.eliminacao_por_id = uuid.UUID(entrada["por_id"])
            except ValueError:
                pass
        if entrada.get("codigo"):
            # O texto da razão não viaja no registo: a lápide fica só com o código.
            from app.shared.pii import cifrar_pii

            evidencia.eliminacao_motivo = cifrar_pii(
                json.dumps({"codigo": entrada["codigo"], "texto": None})
            )
        db.add(evidencia)
        ligacoes.desligar_todas(db, evidencia.id)
        registar_acao(
            db,
            acao=Acao.EVIDENCIA_REAPAGADA_POS_RESTAURO,
            empresa_id=evidencia.empresa_id,
            entidade_tipo="Evidencia",
            entidade_id=evidencia.id,
            dados_novos={
                "conteudo_hash": evidencia.conteudo_hash,
                "tipo": entrada.get("tipo"),
                "codigo": entrada.get("codigo"),
                "apagada_em": entrada.get("em"),
            },
        )
        reaplicados += 1

    return {"reaplicados": reaplicados, "ficheiros": ficheiros}
