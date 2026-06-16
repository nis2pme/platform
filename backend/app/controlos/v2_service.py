"""Compatibilidade temporária para imports antigos do serviço de controlos."""

from app.controlos.service import (
    alterar_estado,
    aprovar_controlo,
    calcular_dashboard,
    concluir_check,
    delegar_controlo,
    get_controlo_detalhe,
    get_historico_relatorios,
    inicializar_controlos_empresa,
    inicializar_controlos_empresa_v2,
    listar_controlos,
    listar_dominios,
    reprovar_controlo,
    reverter_check,
)

__all__ = [
    "alterar_estado",
    "aprovar_controlo",
    "calcular_dashboard",
    "concluir_check",
    "delegar_controlo",
    "get_controlo_detalhe",
    "get_historico_relatorios",
    "inicializar_controlos_empresa",
    "inicializar_controlos_empresa_v2",
    "listar_controlos",
    "listar_dominios",
    "reprovar_controlo",
    "reverter_check",
]