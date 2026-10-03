"""Trancas consultivas de transação (Postgres) para serializar uma decisão.

Serve para o padrão «ler o que falta e inserir»: dois pedidos em paralelo leem
os dois que falta a mesma linha e inserem os dois. Com a tranca, o segundo
espera que o primeiro faça commit e, ao voltar a ler, já vê a linha.

Regras de uso:
- **Só a partir de código que corre fora do event loop** (rotas `def`, ticks em
  thread). A tranca só se larga no commit; num `async def` a espera parava o
  processo inteiro.
- Pedir a tranca só quando há trabalho a fazer (depois de uma leitura otimista),
  para o caminho normal não esperar por nada.
- Em SQLite não há trancas consultivas: a função não faz nada. A prova da
  serialização é contra Postgres.
- Tem limite de espera. Esgotado, desiste só da tranca (savepoint) e o pedido
  segue sem serialização — o pior caso volta a ser o duplicado raro, nunca uma
  paragem.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session

logger = logging.getLogger(__name__)

ESPERA_MS = 5000


def _chave(valor: uuid.UUID) -> int:
    return int.from_bytes(valor.bytes[:4], "big", signed=True)


def trancar_par(db: Session, a: uuid.UUID, b: uuid.UUID, *, espera_ms: int = ESPERA_MS) -> bool:
    """Tranca o par `(a, b)` até ao fim da transação. Devolve True se a obteve."""
    try:
        dialeto = db.get_bind().dialect.name
    except Exception:  # noqa: BLE001 — sessão sem bind (testes com duplos)
        return False
    if dialeto != "postgresql":
        return False
    try:
        # Com savepoint, um tempo de espera esgotado desfaz só a tentativa, e
        # não a transação do pedido.
        with db.begin_nested():
            db.execute(text(f"SET LOCAL lock_timeout = '{int(espera_ms)}ms'"))
            db.execute(text("SELECT pg_advisory_xact_lock(:a, :b)"), {"a": _chave(a), "b": _chave(b)})
        # O SET LOCAL valeria até ao fim da transação e mudaria o limite de
        # outras trancas do mesmo pedido (a da cadeia de auditoria): repõe-se.
        db.execute(text("SET LOCAL lock_timeout = DEFAULT"))
        return True
    except OperationalError:
        logger.warning("Tranca (%s, %s) não obtida em %sms; segue sem serialização.", a, b, espera_ms)
        return False
