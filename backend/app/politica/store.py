"""
Leitura dos desvios de política de uma empresa, com cache em memória.

Este módulo trata só de armazenamento: devolve strings, não conhece a matriz
nem os interruptores. É a matriz que sabe o que fazer com os desvios — assim
não há dependência circular entre a política e as capacidades.

Porquê o cache: a autorização corre em todos os pedidos e várias vezes dentro do
mesmo pedido. Sem cache, cada verificação seria uma ida à base de dados. Com ele,
uma alteração feita por um processo demora até `TTL_SEGUNDOS` a chegar aos
restantes — a instalação pode ter mais do que um worker, e o cache é de cada um.
O processo que faz a alteração invalida o seu próprio cache logo.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import NamedTuple

from fastapi import HTTPException, status
from sqlmodel import Session, select

logger = logging.getLogger(__name__)

# Janela máxima em que um worker pode estar a decidir com política antiga.
TTL_SEGUNDOS = 30.0

# Desvios: (modulo, classe) → {papel: ambito}
Desvios = dict[tuple[str, str], dict[str, str]]


class Politica(NamedTuple):
    """O que uma leitura da base traz: o que mudou, e por que caminho.

    As duas coisas vêm juntas porque saem da mesma consulta — separá-las era
    duplicar a ida à base para responder a metade da pergunta.
    """

    desvios: Desvios
    # Células decididas na grelha, e não por um interruptor nomeado. Serve o
    # documento de funções e responsabilidades: editar a matriz à mão é um facto
    # que um auditor tem de conseguir ler.
    celulas_a_mao: frozenset[tuple[str, str, str]]


_VAZIA = Politica(desvios={}, celulas_a_mao=frozenset())

# empresa_id (str) → (instante de expiração, política)
_cache: dict[str, tuple[float, Politica]] = {}
_lock = threading.Lock()


def _sessao() -> Session:
    """Sessão independente para ler a política.

    Independente de propósito: a leitura acontece dentro de dependências e de
    serviços que nem sempre têm sessão em mão, e nunca escreve nada.
    """
    from app.database import engine

    return Session(engine)


def _ler_da_base(empresa_id: uuid.UUID | str) -> Politica:
    from app.politica.models import PoliticaCapacidade

    desvios: Desvios = {}
    a_mao: set[tuple[str, str, str]] = set()
    with _sessao() as db:
        linhas = db.exec(
            select(PoliticaCapacidade).where(
                PoliticaCapacidade.empresa_id == empresa_id
            )
        ).all()
    for linha in linhas:
        desvios.setdefault((linha.modulo, linha.classe), {})[linha.papel] = linha.ambito
        if linha.origem == "grelha":
            a_mao.add((linha.modulo, linha.classe, linha.papel))
    return Politica(desvios=desvios, celulas_a_mao=frozenset(a_mao))


def politica(empresa_id: uuid.UUID | str | None) -> Politica:
    """
    A política em vigor para esta empresa. Vazia = a empresa segue o defeito.

    Falha fechada: se não puder ser lida e não houver leitura anterior em cache,
    levanta 503 em vez de assumir o defeito. Assumir o defeito devolveria em
    silêncio capacidades que a empresa tinha retirado. Havendo leitura anterior,
    serve-se essa e regista-se o aviso — um problema de base de dados não deve
    alargar permissões nem parar a instalação.
    """
    if empresa_id is None:
        return _VAZIA

    chave = str(empresa_id)
    agora = time.monotonic()

    with _lock:
        entrada = _cache.get(chave)
    if entrada is not None and entrada[0] > agora:
        return entrada[1]

    try:
        lidos = _ler_da_base(empresa_id)
    except Exception:
        logger.exception("Não foi possível ler a política de capacidades")
        if entrada is not None:
            # Prolonga a validade do que já se tinha, para não martelar a base.
            with _lock:
                _cache[chave] = (agora + TTL_SEGUNDOS, entrada[1])
            return entrada[1]
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Política de permissões indisponível.",
        )

    with _lock:
        _cache[chave] = (agora + TTL_SEGUNDOS, lidos)
    return lidos


def desvios(empresa_id: uuid.UUID | str | None) -> Desvios:
    """Só o que a empresa mudou — a pergunta que a matriz faz em cada pedido."""
    return politica(empresa_id).desvios


def editada_a_mao(empresa_id: uuid.UUID | str | None) -> bool:
    """Se alguma célula foi decidida na grelha, e não por um interruptor."""
    return bool(politica(empresa_id).celulas_a_mao)


def invalidar(empresa_id: uuid.UUID | str | None = None) -> None:
    """Esquece o cache de uma empresa, ou de todas se não for indicada."""
    with _lock:
        if empresa_id is None:
            _cache.clear()
        else:
            _cache.pop(str(empresa_id), None)
