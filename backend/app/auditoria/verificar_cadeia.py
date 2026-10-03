"""Verificação da cadeia de hashes do registo de auditoria.

A cadeia é escrita a cada commit e sai no dossiê e no backup, mas nada a
conferia: uma adulteração só seria descoberta por quem tivesse a ferramenta e
a lembrança. Este módulo dá-lhe dois chamadores:

  - um tick diário (ver `main.py`) que confere todas as cadeias vivas e, se uma
    estiver partida, o diz no log e no próprio registo de auditoria;
  - uma linha de comandos para quem investiga:

        python -m app.auditoria.verificar_cadeia [--empresa UUID]

    Código de saída 0 se tudo estiver íntegro, 1 se alguma cadeia partir, 2 se
    nenhuma partir mas houver linhas sem elo depois do início da cadeia.

Só se conferem as linhas VIVAS. As arquivadas têm os dados mascarados de
propósito e verificam-se com `app.auditoria.ler_arquivo`.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from typing import Any

from sqlmodel import Session, select

from app.shared import audit_cadeia
from app.shared.audit import AuditLog

logger = logging.getLogger(__name__)


def _chaves(db: Session) -> list[uuid.UUID | None]:
    """As cadeias existentes: uma por empresa, mais a da plataforma (sem empresa)."""
    empresas = db.exec(
        select(AuditLog.empresa_id).where(AuditLog.empresa_id.is_not(None)).distinct()
    ).all()
    chaves: list[uuid.UUID | None] = sorted(set(empresas), key=str)
    if db.exec(select(AuditLog.id).where(AuditLog.empresa_id.is_(None)).limit(1)).first():
        chaves.append(None)
    return chaves


def _registos(db: Session, empresa_id: uuid.UUID | None) -> list[AuditLog]:
    condicao = AuditLog.empresa_id == empresa_id if empresa_id is not None else AuditLog.empresa_id.is_(None)
    return list(
        db.exec(select(AuditLog).where(condicao).order_by(AuditLog.created_at, AuditLog.id))
    )


def verificar_uma(db: Session, empresa_id: uuid.UUID | None) -> dict[str, Any]:
    registos = audit_cadeia.pela_ordem_da_cadeia(_registos(db, empresa_id))
    # A cadeia viva pode começar a meio: os meses arquivados já saíram da tabela
    # e o primeiro elo vivo aponta para o último arquivado. Parte-se daí — o que
    # se confere é a continuidade do que existe.
    desde = audit_cadeia.GENESE
    for registo in registos:
        if registo.hash_registo is not None:
            desde = registo.hash_anterior or audit_cadeia.GENESE
            break
    resultado = audit_cadeia.verificar(registos, desde=desde)
    resultado["empresa_id"] = str(empresa_id) if empresa_id else None
    _conferir_inicio(resultado, registos, desde)
    return resultado


def _conferir_inicio(resultado: dict[str, Any], registos: list[AuditLog], desde: str) -> None:
    """Um início que não é a génese tem de ser um corte registado pelo arquivo.

    Conferir só a continuidade do que existe deixava passar quem apagasse as
    linhas mais antigas: o que sobra continua a encaixar. Cada arquivo regista
    o elo de onde a parte viva passa a começar; um início que não seja nenhum
    desses denuncia um corte feito por outra via."""
    from app.shared.audit import Acao

    if desde == audit_cadeia.GENESE:
        resultado["inicio"] = "genese"
        return
    cortes: set[str] = set()
    purgas_sem_corte = False
    for registo in registos:
        if registo.acao != Acao.AUDIT_PURGADO:
            continue
        try:
            dados = json.loads(registo.dados_novos or "{}")
        except (TypeError, ValueError):
            dados = {}
        if "corte" in dados:
            if dados["corte"]:
                cortes.add(dados["corte"])
        else:
            purgas_sem_corte = True
    if desde in cortes:
        resultado["inicio"] = "arquivado"
    elif purgas_sem_corte and not cortes:
        # Purgas de uma versão que ainda não registava o corte: não há com que
        # comparar. Não é uma prova de corte, mas também não se confirma.
        resultado["inicio"] = "nao_confirmavel"
    elif resultado.get("integra"):
        resultado.update(
            integra=False,
            motivo="inicio_cortado",
            esperado=sorted(cortes) or None,
            encontrado=desde,
            inicio="cortado",
        )


def verificar_todas(db: Session) -> dict[str, Any]:
    """Confere todas as cadeias. Devolve {integra, cadeias, partidas, com_sem_elo}.

    `integra` diz só se alguma cadeia parte; `com_sem_elo` lista as cadeias com
    linhas escritas depois do início da cadeia e fora dela — não partem nada,
    mas também não se podem dar por verdadeiras."""
    cadeias = [verificar_uma(db, chave) for chave in _chaves(db)]
    partidas = [c for c in cadeias if not c.get("integra", False)]
    com_sem_elo = [c for c in cadeias if c.get("sem_elo")]
    return {"integra": not partidas, "cadeias": cadeias, "partidas": partidas, "com_sem_elo": com_sem_elo}


def _sem_elo_ja_registado(db: Session, cadeia: dict[str, Any]) -> bool:
    """O mesmo achado já está na trilha (mesmo total, mesmas primeiras linhas):
    o tick diário não repete todos os dias a mesma entrada."""
    from app.shared.audit import Acao

    empresa_id = uuid.UUID(cadeia["empresa_id"]) if cadeia.get("empresa_id") else None
    condicao = AuditLog.empresa_id == empresa_id if empresa_id is not None else AuditLog.empresa_id.is_(None)
    ultimo = db.exec(
        select(AuditLog.dados_novos)
        .where(condicao, AuditLog.acao == Acao.AUDIT_CADEIA_SEM_ELO)
        .order_by(AuditLog.created_at.desc())  # type: ignore[union-attr]
        .limit(1)
    ).first()
    if not ultimo:
        return False
    try:
        anterior = json.loads(ultimo)
    except (TypeError, ValueError):
        return False
    return (anterior.get("sem_elo"), anterior.get("sem_elo_ids")) == (cadeia.get("sem_elo"), cadeia.get("sem_elo_ids"))


def verificar_e_registar(db: Session) -> dict[str, Any]:
    """O que o tick diário faz: confere e, se algo estiver partido ou houver
    linhas sem elo, deixa-o no log e no registo de auditoria da empresa afetada
    (ou da plataforma)."""
    from app.shared.audit import Acao, ResultadoAcao, registar_acao

    resultado = verificar_todas(db)
    for cadeia in resultado["com_sem_elo"]:
        logger.warning(
            "Cadeia de auditoria com %d linha(s) SEM ELO (empresa=%s): %s",
            cadeia["sem_elo"], cadeia.get("empresa_id") or "plataforma",
            json.dumps(cadeia.get("sem_elo_ids"), default=str),
        )
        if _sem_elo_ja_registado(db, cadeia):
            continue
        registar_acao(
            db,
            acao=Acao.AUDIT_CADEIA_SEM_ELO,
            resultado=ResultadoAcao.FALHA,
            empresa_id=uuid.UUID(cadeia["empresa_id"]) if cadeia.get("empresa_id") else None,
            dados_novos={"sem_elo": cadeia["sem_elo"], "sem_elo_ids": cadeia.get("sem_elo_ids")},
        )
    for cadeia in resultado["partidas"]:
        logger.error(
            "Cadeia de auditoria PARTIDA (empresa=%s): %s",
            cadeia.get("empresa_id") or "plataforma", json.dumps(cadeia, default=str),
        )
        registar_acao(
            db,
            acao=Acao.AUDIT_CADEIA_PARTIDA,
            resultado=ResultadoAcao.FALHA,
            empresa_id=uuid.UUID(cadeia["empresa_id"]) if cadeia.get("empresa_id") else None,
            dados_novos={
                chave: cadeia.get(chave)
                for chave in ("motivo", "registo_id", "esperado", "encontrado", "verificados", "ignorados", "sem_elo")
                if chave in cadeia
            },
        )
    db.commit()
    if resultado["integra"]:
        logger.info("Cadeia de auditoria íntegra (%d cadeias).", len(resultado["cadeias"]))
    return resultado


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Confere a cadeia de hashes do registo de auditoria.")
    parser.add_argument("--empresa", help="UUID da empresa (omitido = todas + plataforma)")
    args = parser.parse_args(argv)

    from app.shared.segredos_cli import carregar_segredos_da_instalacao

    carregar_segredos_da_instalacao()
    from app.database import engine

    with Session(engine) as db:
        if args.empresa:
            resultado = verificar_uma(db, uuid.UUID(args.empresa))
            integra = bool(resultado.get("integra"))
            sem_elo = bool(resultado.get("sem_elo"))
        else:
            resultado = verificar_todas(db)
            integra = resultado["integra"]
            sem_elo = bool(resultado["com_sem_elo"])
    print(json.dumps(resultado, indent=1, default=str))
    if not integra:
        return 1
    return 2 if sem_elo else 0


if __name__ == "__main__":
    sys.exit(main())
