"""
Resposta JSON da API com as datas-hora marcadas como UTC.

As colunas guardam instantes em UTC sem fuso (`datetime.utcnow()`), e o
Pydantic serializa-os como vieram: "2026-09-25T02:14:13". O browser lê uma
data-hora sem fuso como hora LOCAL — em Portugal, no verão, tudo aparecia uma
hora mais cedo (e a hora de um incidente fica deslocada dos prazos legais, que
já saíam com fuso).

Corrige-se aqui, à saída, e não campo a campo: as respostas vêm de esquemas,
de modelos devolvidos diretamente e de dicionários, e um campo esquecido voltava
a dar o erro. Só se mexe numa string que seja INTEIRA uma data-hora sem fuso;
texto livre, datas sem hora e datas que já tragam fuso ficam como estão.
"""
import re
from datetime import datetime, timezone
from typing import Any

from fastapi.responses import JSONResponse

# Uma data-hora ISO completa, sem fuso: é o que o Pydantic escreve para um
# `datetime` sem tzinfo.
_SEM_FUSO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?")


def marcar_utc(valor: Any) -> Any:
    """Percorre a resposta e acrescenta o fuso UTC às datas-hora que não o têm."""
    if isinstance(valor, str):
        # Filtro barato antes do regex: a maior parte das strings não é uma data.
        if 19 <= len(valor) <= 26 and valor[4:5] == "-" and _SEM_FUSO.fullmatch(valor):
            return valor + "+00:00"
        return valor
    if isinstance(valor, dict):
        return {k: marcar_utc(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [marcar_utc(v) for v in valor]
    if isinstance(valor, datetime) and valor.tzinfo is None:
        return valor.replace(tzinfo=timezone.utc)
    return valor


class UtcJSONResponse(JSONResponse):
    """A resposta por omissão da API (ver `main.py`)."""

    def render(self, content: Any) -> bytes:
        return super().render(marcar_utc(content))
