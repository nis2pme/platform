"""
Mascaramento de dados pessoais nos campos JSON do audit log.

As colunas `dados_anteriores` e `dados_novos` do AuditLog são JSON em claro e
transportam email e nome em muitas ações (criação de utilizador, falhas de login,
delegação de controlos, atribuição de responsáveis). Ao contrário do `ip_address`
e do `user_agent`, não passam por `cifrar_pii`.

Há dois consumidores, com regras diferentes:

- **Apresentação** (`audit_logs/router.py`): mascara só quando o log diz respeito a
  um utilizador já anonimizado. A linha na base fica intacta — a máscara é aplicada
  na resposta, ao abrigo do Art. 17(3)(b) do RGPD, que permite reter o registo da
  ação sem os identificadores diretos.
- **Arquivo** (`auditoria/arquivo.py`): mascara SEMPRE. Um ficheiro de arquivo é
  escrito uma única vez e nunca reaberto, portanto não existe momento posterior em
  que uma anonimização o possa alcançar. Quem for anonimizado depois do arquivo ser
  fechado ficaria com o email lá dentro para sempre. O `utilizador_id` continua
  presente, e é ele que dá a rastreabilidade enquanto a conta existir.
"""
from __future__ import annotations

import json

# Campos que identificam uma pessoa diretamente. O `password_hash` e o
# `totp_secret_cifrado` não deviam sequer chegar aqui (o `_sanitize_dados` do
# registar_acao já os substitui por [REDACTED]), mas a lista cobre-os para o caso
# de um registo antigo, gravado antes dessa proteção existir.
CAMPOS_PII_UTILIZADOR = frozenset({
    "email", "nome", "password_hash", "totp_secret_cifrado",
    "implementador_email", "implementador_nome",
    "admin_email", "responsavel_nome", "dono_nome",
    # O remetente do sistema: numa PME é muitas vezes a caixa de uma pessoa.
    "smtp_from_email",
})

MASCARA = "[anonimizado]"


def mascarar_pii_em_dados(dados_json: str | None) -> str | None:
    """
    Substitui os campos identificativos por `[anonimizado]` num JSON serializado.

    Preserva a estrutura e todos os restantes campos: quem lê continua a saber o que
    aconteceu, deixa é de saber a quem. Um valor que não seja JSON válido é devolvido
    tal como veio — não é papel desta função decidir o que fazer com dados corrompidos,
    e engoli-los em silêncio esconderia o problema.
    """
    if not dados_json:
        return dados_json
    try:
        dados = json.loads(dados_json)
    except (TypeError, ValueError):
        return dados_json
    if not isinstance(dados, dict):
        return dados_json
    for campo in CAMPOS_PII_UTILIZADOR:
        if campo in dados:
            dados[campo] = MASCARA
    return json.dumps(dados, ensure_ascii=False)
