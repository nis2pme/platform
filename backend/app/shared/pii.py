"""
Cifragem centralizada de campos PII (Personally Identifiable Information).

Usa AES-128-CBC via Fernet (biblioteca cryptography), mesma abordagem
usada para TOTP secrets e conteúdo de evidências.

Campos cobertos:
- Utilizador.nome
- Empresa.nome, Empresa.nif, Empresa.email, Empresa.website
- AuditLog.ip_address, AuditLog.user_agent
- TokenRefresh.ip_address, TokenRefresh.user_agent
- PasswordResetToken.ip_address
- Evidencia.ficheiro_nome
- Notificacao.params — só as chaves que o catálogo de notificações declara
  (nome de quem auditou, texto do pedido de esclarecimento)

A chave `PII_ENCRYPTION_KEY` é OBRIGATÓRIA: sem ela a app não arranca (validada em
`config.py`) e a cifra recusa-se a trabalhar. O escape `PII_DEV_PLAINTEXT=1` existe
só para desenvolvimento local fora do contentor e deixa rasto no log.

Na leitura, um valor que não decifra devolve `None` — nunca o texto que entrou. Um
`InvalidToken` significa chave errada ou dados corrompidos, e devolver o criptograma
faria a app mostrá-lo no ecrã como se fosse o nome de uma pessoa.
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy.types import Text, TypeDecorator

logger = logging.getLogger(__name__)

# Bytes fixos que um token Fernet acrescenta ao texto cifrado, antes do base64:
# 1 de versão + 8 de timestamp + 16 de IV + 32 de HMAC-SHA256.
_FERNET_OVERHEAD = 57

# Instância singleton — inicializada uma vez na primeira chamada
_fernet: Fernet | MultiFernet | None = None


def _get_fernet() -> Fernet | MultiFernet | None:
    """
    Devolve a cifra da PII_ENCRYPTION_KEY (com a anterior, durante uma rotação).
    Devolve None se a chave não estiver definida (modo sem cifra).
    """
    global _fernet
    if _fernet is not None:
        return _fernet

    from app.shared.chaves import fernet
    _fernet = fernet("PII_ENCRYPTION_KEY")
    return _fernet


def _escape_dev_ativo() -> bool:
    """True se o escape de desenvolvimento local estiver ligado explicitamente.

    Nunca em SaaS: a base é partilhada por vários clientes, e o `config.py` já
    recusa arrancar nesse caso — esta segunda verificação existe para o escape
    não voltar a valer por um caminho que não passe pela validação do arranque.
    """
    import os

    if os.getenv("PII_DEV_PLAINTEXT") != "1":
        return False
    return os.getenv("DEPLOYMENT_MODE", "onprem") != "saas"


def cifra_ativa() -> bool:
    """Indica se a cifra de PII está operacional. Usado no arranque para o estado
    ficar registado — sem isto não há forma de saber se os dados estão cifrados."""
    return _get_fernet() is not None


def cifrar_pii(valor: str | None) -> str | None:
    """
    Cifra um campo PII com Fernet (AES-128-CBC + HMAC-SHA256).

    - Se valor é None: devolve None.
    - Caso contrário: devolve o token Fernet em string UTF-8.

    FAIL-CLOSED: sem chave levanta, em vez de devolver o valor em claro. Gravar PII
    sem cifra é uma perda de proteção que ninguém notaria — não há erro, não há
    aviso, e os dados ficam legíveis a quem leia a base de dados.
    """
    if valor is None:
        return None
    fernet = _get_fernet()
    if fernet is None:
        if _escape_dev_ativo():
            logger.warning(
                "[SEGURANCA] PII gravada SEM cifra (PII_DEV_PLAINTEXT=1) — "
                "apenas para desenvolvimento local."
            )
            return valor
        raise RuntimeError(
            "cifra de PII sem chave (fail-closed): PII_ENCRYPTION_KEY ausente. "
            "No Docker o entrypoint.sh gera-a automaticamente; fora do contentor, "
            "copiar de /app/data/auto-secrets.env, ou PII_DEV_PLAINTEXT=1 em dev."
        )
    return fernet.encrypt(valor.encode()).decode()


def truncar_para_cifra(valor: str | None, limite_coluna: int = 700) -> str | None:
    """
    Corta um texto para que o criptograma Fernet caiba numa coluna de N caracteres.

    O Fernet expande o que cifra: 57 bytes de versão, timestamp, IV e HMAC, mais o
    texto alinhado ao bloco de 16, tudo codificado em base64. Um texto de 500 bytes
    produz 760 caracteres e não cabe num varchar(700) — o INSERT rebenta. Como o
    User-Agent chega no pedido, isso é um erro que qualquer cliente pode provocar
    à vontade, e nos caminhos de autenticação transforma a resposta esperada num 500.

    Invertendo a expansão: o token cabe em `limite_coluna` se ocupar no máximo
    `limite_coluna // 4 * 3` bytes; ao texto limpo sobra o maior múltiplo de 16 que
    reste depois dos 57 fixos, menos 1 byte — o padding PKCS7 acrescenta sempre pelo
    menos um e por isso um texto de exatamente 16 bytes já ocupa dois blocos.

    Corta em BYTES, não em caracteres: um User-Agent pode trazer algo fora de ASCII,
    e cortar a meio de uma sequência UTF-8 produziria texto que já não codifica.
    """
    if valor is None:
        return None

    limite_bytes = (limite_coluna // 4 * 3 - _FERNET_OVERHEAD) // 16 * 16 - 1
    if limite_bytes <= 0:
        raise ValueError(
            f"limite_coluna={limite_coluna} é pequeno demais para um token Fernet."
        )

    bruto = valor.encode("utf-8")
    if len(bruto) <= limite_bytes:
        return valor
    # errors="ignore" descarta a sequência incompleta que sobra no corte.
    return bruto[:limite_bytes].decode("utf-8", errors="ignore")


def decifrar_pii(cifrado: str | None) -> str | None:
    """
    Decifra um campo PII cifrado com Fernet.

    - Se cifrado é None: devolve None.
    - Se não decifrar: devolve None e regista o erro.

    Devolver `None` e não o valor recebido é deliberado. Um `InvalidToken` significa
    chave errada ou dados corrompidos — e devolver o criptograma faria a app
    apresentá-lo como se fosse o valor real, incluindo em dossiês e relatórios
    exportados. Também não levanta: na leitura, recusar não protege nada que já não
    esteja comprometido, e uma linha corrompida deitaria abaixo a listagem inteira
    que a contém. Quem diagnostica é o log.
    """
    if cifrado is None:
        return None
    fernet = _get_fernet()
    if fernet is None:
        if _escape_dev_ativo():
            return cifrado
        raise RuntimeError(
            "leitura de PII sem chave (fail-closed): PII_ENCRYPTION_KEY ausente. "
            "No Docker o entrypoint.sh gera-a automaticamente; fora do contentor, "
            "copiar de /app/data/auto-secrets.env, ou PII_DEV_PLAINTEXT=1 em dev."
        )
    try:
        return fernet.decrypt(cifrado.encode()).decode()
    except InvalidToken:
        logger.error(
            "decifrar_pii: valor não decifrável (chave errada ou dados corrompidos). "
            "O campo fica vazio — verificar se a PII_ENCRYPTION_KEY é a da instalação "
            "que gravou estes dados."
        )
        return None


class TextoCifrado(TypeDecorator):
    """Texto livre cifrado em repouso com a PII_ENCRYPTION_KEY.

    Para campos onde se escreve o que calha — notas, lições aprendidas, a linha
    temporal de um incidente — e que por isso acabam por levar nomes, contactos e
    pormenores de pessoas. Cifra ao gravar e decifra ao ler: quem usa o modelo
    continua a ver texto, e os relatórios, o dossiê e as exportações não mudam.

    Só para colunas que nunca entram num WHERE ou num ORDER BY: o Fernet dá um
    criptograma diferente de cada vez, e a pesquisa deixaria de as encontrar.
    Um texto vazio fica vazio — não há nada a proteger nele. Um valor que não
    decifra sai vazio, como em `decifrar_pii`.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or value == "":
            return value
        return cifrar_pii(value)

    def process_result_value(self, value, dialect):
        if value is None or value == "":
            return value
        claro = decifrar_pii(value)
        return "" if claro is None else claro
