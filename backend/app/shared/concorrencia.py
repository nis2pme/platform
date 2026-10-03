"""Quantas operações caras correm ao mesmo tempo no núcleo.

Algumas operações pedem de uma vez muita memória (medido com os parâmetros da
aplicação):
  - argon2 (hash ou verificação de uma password ou de um código 2FA): 64 MiB;
  - scrypt da frase-passe dos backups: 128 MiB;
  - montagem do payload de uma análise IA: perto de 9 vezes o tamanho das
    evidências do controlo (446 MiB para os 50 MB do teto);
  - geração de um dossiê: o keyslot por frase-passe usa o scrypt do age, que se
    calibra pela velocidade do CPU (128 a 512 MiB), e cada evidência de 14 MB
    passa por ~108 MiB enquanto é cifrada.

O núcleo corre num só processo do uvicorn. Sem limite, N pedidos em paralelo
pedem N vezes isto, e o processo morre por falta de memória — em SaaS, com
todos os clientes lá dentro.

O limite é um semáforo de THREADS, e não do asyncio: as rotas que fazem este
trabalho são `def`, e o FastAPI corre-as no threadpool do anyio (40 threads),
fora do event loop. Quem chega com as vagas cheias espera um pouco nessa thread;
se nenhuma vaga abrir a tempo, recebe 503 com Retry-After, em vez de derrubar o
processo. Se, por engano, alguém o usar de dentro do event loop, não espera:
recusa logo, porque esperar ali pararia o processo inteiro.
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from contextlib import contextmanager

from fastapi import HTTPException, status

from app.config import get_settings

logger = logging.getLogger(__name__)

MSG_OCUPADO = "O servidor está ocupado. Tente de novo dentro de momentos."


class ServidorOcupado(HTTPException):
    """As vagas de uma operação cara estão todas ocupadas: tentar daqui a pouco."""

    def __init__(self, espera_s: float) -> None:
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=MSG_OCUPADO,
            headers={"Retry-After": str(max(1, math.ceil(espera_s)))},
        )


def _no_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class LimiteDeConcorrencia:
    """No máximo `vagas` operações ao mesmo tempo; quem chega a seguir espera
    até `espera_s` segundos por uma vaga e, sem ela, recebe `ServidorOcupado`.

    Com `fila_max` definido, só esperam `fila_max` pedidos de cada vez; os que
    chegam com a fila cheia recebem 503 **logo**, sem prender uma thread. Sem
    isto, uma rajada punha tantas threads a dormir quantas os pedidos, e o
    threadpool do anyio (partilhado por todas as rotas `def`) esgotava-se.

    Com `reservadas` > 0, esse número de vagas fica guardado para quem chama com
    `prioritario=True` (o utilizador com cookie de dispositivo válido): os outros
    ocupam no máximo `vagas - reservadas`, e há sempre uma vaga para quem volta.
    """

    def __init__(
        self,
        nome: str,
        vagas: int,
        espera_s: float,
        fila_max: int | None = None,
        reservadas: int = 0,
    ) -> None:
        if vagas < 1:
            raise ValueError(f"o limite '{nome}' precisa de pelo menos uma vaga")
        if reservadas < 0 or reservadas >= vagas:
            raise ValueError(f"o limite '{nome}': reservadas tem de ser < vagas")
        self.nome = nome
        self.vagas = vagas
        self.espera_s = espera_s
        self.fila_max = fila_max
        self.reservadas = reservadas
        self._cond = threading.Condition()
        self._ocupadas = 0
        self._a_espera = 0

    @property
    def pendentes(self) -> int:
        """Quantos ocupam uma vaga ou esperam por ela (para provas e métricas)."""
        with self._cond:
            return self._ocupadas + self._a_espera

    @property
    def a_espera(self) -> int:
        """Quantos estão à espera de uma vaga agora."""
        with self._cond:
            return self._a_espera

    @property
    def sob_pressao(self) -> bool:
        """True quando as vagas dos não-prioritários estão todas ocupadas: um
        pedido de um desconhecido teria de esperar."""
        with self._cond:
            return self._ocupadas >= self._teto(prioritario=False)

    def _teto(self, prioritario: bool) -> int:
        return self.vagas if prioritario else self.vagas - self.reservadas

    @contextmanager
    def ocupar(self, prioritario: bool = False):
        espera = 0 if _no_event_loop() else self.espera_s
        teto = self._teto(prioritario)
        with self._cond:
            if self._ocupadas < teto:
                self._ocupadas += 1
            else:
                # Sem vaga: entra na fila se houver espaço, senão 503 já.
                if self.fila_max is not None and self._a_espera >= self.fila_max:
                    logger.warning(
                        "Limite '%s' com a fila cheia (%d à espera): 503 imediato.",
                        self.nome, self._a_espera,
                    )
                    raise ServidorOcupado(self.espera_s)
                prazo = time.monotonic() + espera
                self._a_espera += 1
                try:
                    while self._ocupadas >= teto:
                        restante = prazo - time.monotonic()
                        if restante <= 0:
                            logger.warning(
                                "Limite '%s' cheio (%d vagas): pedido recusado com 503.",
                                self.nome, self.vagas,
                            )
                            raise ServidorOcupado(self.espera_s)
                        self._cond.wait(timeout=restante)
                    self._ocupadas += 1
                finally:
                    self._a_espera -= 1
        try:
            yield
        finally:
            with self._cond:
                self._ocupadas -= 1
                self._cond.notify()


_settings = get_settings()

# argon2: cada operação dura ~0,3 s num contentor com 1 CPU, e a espera de 5 s
# deixa passar uma rajada antes de recusar. A fila tem teto para uma enxurrada
# não prender todas as threads do threadpool; uma vaga fica reservada para quem
# volta com um cookie de dispositivo válido (prioritario=True), por isso os
# desconhecidos nunca ocupam a instalação inteira.
LIMITE_ARGON2 = LimiteDeConcorrencia(
    "argon2",
    vagas=_settings.CONCORRENCIA_ARGON2,
    espera_s=5,
    fila_max=_settings.CONCORRENCIA_ARGON2_FILA,
    reservadas=_settings.CONCORRENCIA_ARGON2_RESERVADAS,
)

# As operações pesadas partilham UMA fila: cada uma, sozinha, já pode passar dos
# 400 MiB, e duas ao mesmo tempo não cabem na memória do núcleo. São raras
# (ações de administração e de análise), e a mais longa (um dossiê com muitas
# evidências) leva segundos a dezenas de segundos — daí a espera maior.
LIMITE_OPERACOES_PESADAS = LimiteDeConcorrencia(
    "operacoes_pesadas", vagas=_settings.CONCORRENCIA_PESADAS, espera_s=30
)
