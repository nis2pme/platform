"""Validação de entrada partilhada pelos schemas de atualização (PATCH).

Num PATCH um campo pode ser omitido (não muda) — mas `null` explícito num campo
que a base não aceita vazio chegava ao UPDATE e rebentava com 500. `SemNulos`
recusa-o com 422 antes de tocar na base.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, ClassVar

from pydantic import BaseModel, model_validator


class SemNulos(BaseModel):
    """Recusa `null` explícito nos campos listados em `nao_anulaveis`."""

    nao_anulaveis: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _recusar_nulos(cls, dados: Any) -> Any:
        if isinstance(dados, dict):
            nulos = sorted(c for c in cls.nao_anulaveis if c in dados and dados[c] is None)
            if nulos:
                raise ValueError(f"campos que não podem ser nulos: {', '.join(nulos)}")
        return dados


def teto_em_bytes(valor: str | None, maximo: int, campo: str) -> str | None:
    """Recusa texto que, em UTF-8, passa de `maximo` bytes (campos cifrados em
    repouso: o criptograma tem de caber na coluna)."""
    if valor is not None and len(valor.encode("utf-8")) > maximo:
        raise ValueError(f"{campo} demasiado longo")
    return valor


# O fuso mais adiantado do mundo (Kiribati, UTC+14).
_FUSO_MAIS_ADIANTADO = timedelta(hours=14)


def e_data_futura(dia: date, agora: datetime | None = None) -> bool:
    """True se `dia` ainda não chegou em nenhum fuso horário.

    O servidor não sabe o fuso de quem escreve, e o «hoje» do browser pode já
    ser amanhã em UTC: em Portugal, no verão, entre as 00:00 e a 01:00. Comparar
    com a data UTC recusava a data de hoje nessa hora. Só é futuro com certeza o
    que vem depois de hoje no fuso mais adiantado.
    """
    agora = agora or datetime.now(timezone.utc)
    return dia > (agora.astimezone(timezone.utc) + _FUSO_MAIS_ADIANTADO).date()
