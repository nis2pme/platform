"""Limites do login por endereço, por grupo de endereços e por cookie de
dispositivo, mais a decisão de quando exigir a prova de trabalho.

É a camada 1 (limites por /64 e /48 em IPv6, por endereço e /24 em IPv4) e a
argamassa que liga a camada 2 (cookie) e a camada 3 (desafio). Vive em memória
do processo (o núcleo corre num só processo do uvicorn), como o limitador do
slowapi: por isso tem um `reset()` para os testes, e a defesa que NÃO depende
disto é o bloqueio por conta/IP na base de dados.

O caminho de quem NÃO traz cookie fica igual ao de sempre — os mesmos limites por
endereço, sem nada que dependa de a conta existir. Quem traz um cookie válido para
o utilizador certo troca os limites por endereço pelo seu próprio limite por
cookie.
"""
from __future__ import annotations

import ipaddress
import threading
from datetime import datetime

from fastapi import HTTPException, status

from app.config import get_settings
from app.shared.concorrencia import LIMITE_ARGON2
from app.shared.utils import chave_de_limite_ip

MSG_DEMASIADOS = "Demasiados pedidos. Tente novamente mais tarde."

# Janelas deslizantes em memória: cada chave guarda os instantes dos pedidos.
_eventos: dict[str, list[float]] = {}
_falhas: dict[str, list[float]] = {}
_trinco = threading.Lock()


def chave_grupo(ip: str) -> str:
    """O grupo de endereços a que `ip` pertence: /48 em IPv6, /24 em IPv4.

    É o degrau acima do que os limites por endereço já contam (o /64 ou o IPv4):
    um alojamento dá muitas vezes um /48 inteiro, e sem contar o grupo o atacante
    rodava os /64 dentro dele à vontade."""
    try:
        endereco = ipaddress.ip_address(ip.strip())
    except (ValueError, AttributeError):
        return ip
    if endereco.version == 6:
        if endereco.ipv4_mapped is not None:
            return str(ipaddress.ip_network(f"{endereco.ipv4_mapped}/24", strict=False))
        return str(ipaddress.ip_network(f"{endereco}/48", strict=False))
    return str(ipaddress.ip_network(f"{endereco}/24", strict=False))


def _contar(mapa: dict, chave: str, agora: float, janela_s: float, incrementar: bool) -> int:
    with _trinco:
        eventos = [t for t in mapa.get(chave, ()) if t > agora - janela_s]
        if incrementar:
            eventos.append(agora)
        mapa[chave] = eventos
        return len(eventos)


def registar_falha(ip: str, agora: datetime) -> None:
    """Conta uma falha de login para o endereço e para o grupo (só liga a prova de
    trabalho; o bloqueio a sério continua na base de dados)."""
    t = agora.timestamp()
    janela = get_settings().LOGIN_IP_JANELA_MINUTOS * 60
    _contar(_falhas, f"e:{chave_de_limite_ip(ip)}", t, janela, incrementar=True)
    _contar(_falhas, f"g:{chave_grupo(ip)}", t, janela, incrementar=True)


def _falhas_recentes(ip: str, agora: float) -> int:
    janela = get_settings().LOGIN_IP_JANELA_MINUTOS * 60
    return max(
        _contar(_falhas, f"e:{chave_de_limite_ip(ip)}", agora, janela, incrementar=False),
        _contar(_falhas, f"g:{chave_grupo(ip)}", agora, janela, incrementar=False),
    )


def precisa_desafio(ip: str, agora: datetime) -> bool:
    """Liga a prova de trabalho quando a fila de argon2 dos desconhecidos está
    cheia, ou quando o endereço/grupo já acumulou falhas."""
    if LIMITE_ARGON2.sob_pressao:
        return True
    limiar = get_settings().DESAFIO_FALHAS_PARA_EXIGIR
    return _falhas_recentes(ip, agora.timestamp()) >= limiar


def dificuldade_atual(ip: str, agora: datetime) -> int:
    """A dificuldade sobe com a pressão da fila e com as falhas do grupo."""
    s = get_settings()
    nivel = LIMITE_ARGON2.a_espera + _falhas_recentes(ip, agora.timestamp()) // max(
        1, s.DESAFIO_FALHAS_PARA_EXIGIR
    )
    return min(s.DESAFIO_DIFICULDADE_BASE * (1 + nivel), s.DESAFIO_DIFICULDADE_MAX)


def _ler_solucao(request) -> dict | None:
    """Lê a solução do desafio do cabeçalho `X-Desafio` (JSON em base64url)."""
    import base64
    import json

    bruto = request.headers.get("x-desafio") if request is not None else None
    if not bruto:
        return None
    try:
        preenchido = bruto + "=" * (-len(bruto) % 4)
        return json.loads(base64.urlsafe_b64decode(preenchido))
    except (ValueError, TypeError):
        return None


def _429(detalhe) -> HTTPException:
    return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detalhe)


def guarda_login(request, *, prioritario: bool, agora: datetime, nonce: str | None = None) -> None:
    """A camada 1 do login. Levanta 429 se um limite for excedido; sob carga (ou
    depois de falhas) exige a prova de trabalho a quem não traz cookie.

    `prioritario` é True quando quem chama traz um cookie de dispositivo válido
    para o utilizador que tenta entrar: nesse caso conta só o limite por cookie
    (identificado pelo `nonce` desse cookie).
    """
    from app.shared.audit import _extrair_ip

    ip = _extrair_ip(request) if request is not None else None
    if not ip:
        return  # sem IP fidedigno não há por onde contar (só em testes de unidade)
    t = agora.timestamp()
    s = get_settings()

    if prioritario:
        # Reconhecido: fora dos limites por endereço, com o seu próprio limite.
        if _contar(_eventos, f"c:{nonce}", t, 60, incrementar=True) > s.DISPOSITIVO_POR_MINUTO:
            raise _429(MSG_DEMASIADOS)
        return

    # Desconhecido: limite por endereço e por grupo.
    if _contar(_eventos, f"e:{chave_de_limite_ip(ip)}", t, 60, incrementar=True) > s.LOGIN_ENDERECO_POR_MINUTO:
        raise _429(MSG_DEMASIADOS)
    if _contar(_eventos, f"g:{chave_grupo(ip)}", t, 60, incrementar=True) > s.LOGIN_GRUPO_POR_MINUTO:
        raise _429(MSG_DEMASIADOS)

    # Sob carga (ou depois de falhas), exige a prova de trabalho antes do argon2.
    if precisa_desafio(ip, agora):
        from app.auth import desafio as desafio_mod

        grupo = chave_grupo(ip)
        if not desafio_mod.verificar(_ler_solucao(request), grupo):
            raise _429({
                "codigo": "desafio_necessario",
                "desafio": desafio_mod.emitir(grupo, dificuldade_atual(ip, agora)),
            })


def guarda_grupo_reset(request, *, agora: datetime) -> None:
    """No pedido de recuperação: limite por grupo (o por endereço/rota já existe
    no slowapi). Sem cookie nem prova de trabalho — o pedido não gasta argon2 de
    verificação, mas convém não deixar um grupo inteiro martelar o envio."""
    from app.shared.audit import _extrair_ip

    ip = _extrair_ip(request) if request is not None else None
    if not ip:
        return
    t = agora.timestamp()
    limite = get_settings().LOGIN_RESET_GRUPO_POR_MINUTO
    if _contar(_eventos, f"gr:{chave_grupo(ip)}", t, 60, incrementar=True) > limite:
        raise _429(MSG_DEMASIADOS)


def reset() -> None:
    """Limpa as janelas em memória (usado nos testes)."""
    with _trinco:
        _eventos.clear()
        _falhas.clear()
