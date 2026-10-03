"""Enums partilhados entre módulos de negócio."""

from enum import Enum


class EstadoControlo(str, Enum):
    NAO_INICIADO = "nao_iniciado"
    EM_PROGRESSO = "em_progresso"
    IMPLEMENTADO = "implementado"
    APROVADO = "aprovado"
    NAO_APROVADO = "nao_aprovado"
    # Excluído do âmbito (scoping): não conta para scores nem pendências.
    # Exige justificação, fica visível e contestável pelo auditor.
    NAO_APLICAVEL = "nao_aplicavel"