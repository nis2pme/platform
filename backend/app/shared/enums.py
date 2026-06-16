"""Enums partilhados entre módulos de negócio."""

from enum import Enum


class EstadoControlo(str, Enum):
    NAO_INICIADO = "nao_iniciado"
    EM_PROGRESSO = "em_progresso"
    IMPLEMENTADO = "implementado"
    APROVADO = "aprovado"
    NAO_APROVADO = "nao_aprovado"