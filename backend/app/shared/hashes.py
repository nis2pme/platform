"""
Impressões determinísticas de valores que também são guardados cifrados.

A cifra de PII usa Fernet, que é deliberadamente não determinístico: o mesmo IP
cifrado duas vezes dá dois criptogramas diferentes. Isso protege os dados mas
impede qualquer agregação — não há `COUNT(DISTINCT)` nem filtro de igualdade sem
decifrar a tabela toda em memória.

A solução é guardar, ao lado do valor cifrado, uma impressão determinística. Tem
de ser com chave: o espaço de endereços IPv4 tem pouco mais de 4 mil milhões de
valores e um SHA-256 sem chave percorre-se por força bruta em segundos. Como a
linha já guarda o IP cifrado, uma impressão sem chave entregaria a quem leia a
base — e não tenha a chave — exatamente aquilo que a cifra existe para negar.

Consequência a assumir: rodar a PII_ENCRYPTION_KEY separa as impressões
anteriores das posteriores, tal como já torna ilegível o campo cifrado.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)

# Separação de domínio: cada impressão tem o seu pepper, e nenhum serve para mais
# nada, mesmo derivando todos da mesma chave que a cifra de PII.
_PERSONALIZACAO = b"audit-ip"
_PERSONALIZACAO_BLOQUEIO = b"login-ip"
_PERSONALIZACAO_DOCUMENTO = b"doc-anexar"

_peppers: dict[bytes, bytes] = {}


def _get_pepper(personalizacao: bytes = _PERSONALIZACAO) -> bytes | None:
    """Deriva o pepper da PII_ENCRYPTION_KEY. None se a chave não estiver definida."""
    if personalizacao in _peppers:
        return _peppers[personalizacao]

    from app.config import get_settings

    chave = get_settings().PII_ENCRYPTION_KEY
    if not chave:
        return None

    _peppers[personalizacao] = hashlib.blake2b(
        chave.encode(), person=personalizacao, digest_size=32
    ).digest()
    return _peppers[personalizacao]


def hash_ip_bloqueio(ip: str) -> str:
    """Impressão do IP para o acumulador anti-spray do login.

    Com chave pela mesma razão que a da trilha: a tabela de bloqueios guarda os
    IPs de quem falhou o login, e um SHA-256 sem chave de um IPv4 inverte-se em
    segundos. O pepper é outro, para as duas tabelas não se cruzarem.
    """
    return _impressao(ip, _PERSONALIZACAO_BLOQUEIO)


def hash_ip_auditoria(ip: str | None) -> str | None:
    """
    Devolve a impressão HMAC-SHA256 de um IP, para contagem e filtro em SQL.

    FAIL-CLOSED, como a cifra: sem chave levanta em vez de cair para um hash sem
    chave. Degradar em silêncio produziria impressões reversíveis indistinguíveis
    das outras, e ninguém repararia. O escape de desenvolvimento local é o mesmo
    que a cifra usa.
    """
    if not ip:
        return None
    return _impressao(ip, _PERSONALIZACAO)


def _impressao(ip: str, personalizacao: bytes) -> str:
    pepper = _get_pepper(personalizacao)
    if pepper is None:
        from app.shared.pii import _escape_dev_ativo

        if _escape_dev_ativo():
            logger.warning(
                "[SEGURANCA] impressão de IP sem chave (PII_DEV_PLAINTEXT=1) — "
                "apenas para desenvolvimento local."
            )
            return hashlib.sha256(ip.encode("utf-8")).hexdigest()
        raise RuntimeError(
            "impressão de IP sem chave (fail-closed): PII_ENCRYPTION_KEY ausente. "
            "No Docker o entrypoint.sh gera-a automaticamente; fora do contentor, "
            "copiar de /app/data/auto-secrets.env, ou PII_DEV_PLAINTEXT=1 em dev."
        )

    return hmac.new(pepper, ip.encode("utf-8"), hashlib.sha256).hexdigest()


def selo_documento(mensagem: str) -> str | None:
    """HMAC do núcleo sobre `mensagem`, para um valor que dá uma volta pelo
    browser e tem de voltar sem ter sido trocado (a impressão estável de um
    documento gerado, que o browser devolve ao anexá-lo como evidência).

    None sem chave: quem verifica recusa, e o valor não é aceite — o lado
    seguro, que só custa a deduplicação desse documento."""
    pepper = _get_pepper(_PERSONALIZACAO_DOCUMENTO)
    if pepper is None:
        return None
    return hmac.new(pepper, mensagem.encode("utf-8"), hashlib.sha256).hexdigest()
