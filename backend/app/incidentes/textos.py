"""
Nomes dos marcos de notificação e a base legal de cada um, em pt e en.

Num módulo à parte, sem dependências, para o serviço, os documentos e os emails
dizerem o mesmo.
"""
from __future__ import annotations

MARCOS_TXT = {
    "pt": {
        "notificacao_inicial": "Notificação inicial (24 h)",
        "atualizacao": "Atualização (72 h, se necessária)",
        "fim_impacto": "Notificação de fim de impacto (24 h)",
        "relatorio_final": "Relatório final (30 dias úteis)",
        "intercalar": "Relatório intercalar (semanal, a pedido)",
        "cnpd": "CNPD (72 h, RGPD)",
    },
    "en": {
        "notificacao_inicial": "Initial notification (24h)",
        "atualizacao": "Update (72h, if needed)",
        "fim_impacto": "End-of-impact notification (24h)",
        "relatorio_final": "Final report (30 working days)",
        "intercalar": "Interim report (weekly, on request)",
        "cnpd": "CNPD (72h, GDPR)",
    },
}

REFERENCIA_MARCO = {
    "pt": {
        "notificacao_inicial": "RJC, art. 42.º, n.º 1",
        "atualizacao": "RJC, art. 42.º, n.º 3",
        "fim_impacto": "RJC, art. 43.º, n.º 1",
        "relatorio_final": "RJC, art. 44.º, n.º 1",
        "intercalar": "RJC, art. 44.º, n.º 3",
        "cnpd": "RGPD, art. 33.º",
    },
    "en": {
        "notificacao_inicial": "RJC, article 42(1)",
        "atualizacao": "RJC, article 42(3)",
        "fim_impacto": "RJC, article 43(1)",
        "relatorio_final": "RJC, article 44(1)",
        "intercalar": "RJC, article 44(3)",
        "cnpd": "GDPR, article 33",
    },
}


def lingua(locale: str | None) -> str:
    """'en' para qualquer variante inglesa; o resto em português."""
    return "en" if (locale or "").lower().startswith("en") else "pt"
