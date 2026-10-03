"""Resolução da política de segurança e notificações do tenant.

`Empresa.config_seguranca` e `Empresa.config_notificacoes` existiam há muito como
JSONB, saíam nas respostas da API e **não eram lidos por ninguém**: quem decidia
era sempre o `config.py`. Um administrador podia gravar `password_min: 20`, ver o
valor guardado e continuar a poder criar contas com oito caracteres. Um parâmetro
de segurança que a aplicação mostra e não aplica é pior do que não existir — dá
uma garantia que ninguém está a cumprir.

## Precedência

    base de dados (tenant)  →  config.py (instalação)  →  default do código

## Quem garante é o servidor

O frontend restringe por conveniência; **os clamps estão aqui**. Um valor fora do
intervalo não é recusado com erro — é trazido para dentro dos limites, porque uma
política é um pedido de aperto e não uma instrução arbitrária. O que **nunca** se
aceita é um valor que afrouxe abaixo do mínimo da instalação: a política do
tenant só pode apertar.

## On-prem vs SaaS

Só faz sentido on-prem, onde há uma instalação por cliente e o administrador é o
dono da máquina. Em **SaaS os valores são fixos e invisíveis**, e a recusa está no
servidor — esconder o separador no frontend deixaria a restrição decorativa, que
é a diferença entre uma política e um adorno.
"""
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from sqlmodel import Session

from app.config import get_settings
from app.shared.utils import PASSWORD_MIN, validar_forca_password

settings = get_settings()


@dataclass(frozen=True)
class Politica:
    """Os parâmetros configuráveis por agora. O lockout e a sessão ficam para depois."""

    password_min: int
    backup_retencao_dias: int
    max_upload_mb: int
    email_notificacoes: bool


# Limites de cada parâmetro: (mínimo aceitável, máximo aceitável).
#
# O mínimo é o chão de segurança — a política do tenant não desce daqui, senão
# seria uma forma de enfraquecer a instalação a partir do ecrã de definições.
# O máximo evita valores que partiriam a aplicação sem o utilizador perceber
# porquê: uma password mínima de 200 caracteres tranca toda a gente cá fora.
CLAMPS: dict[str, tuple[int, int]] = {
    # O chão é o mínimo da plataforma: a empresa só pode apertar. 64 é folgado e
    # ainda memorizável numa frase-passe.
    "password_min": (PASSWORD_MIN, 64),
    # Menos de um dia de retenção não é retenção. O teto evita encher o disco de
    # uma instalação pequena sem aviso.
    "backup_retencao_dias": (1, 365),
    # O teto de 14 MB não é escolhido a olho: é o limite que o nginx à frente
    # aceita. Deixar configurar acima disso daria um erro do proxy, longe do
    # ecrã onde o valor foi posto, e ninguém ligaria as duas coisas.
    "max_upload_mb": (1, 14),
}


def _inteiro(valor: Any) -> int | None:
    """Aceita o que veio do JSONB sem confiar no tipo.

    O JSONB devolve o que lá foi escrito: um `"20"` gravado por um cliente
    antigo é texto, e um `True` é um booleano que o Python converteria em 1 sem
    se queixar. Um valor que não seja um inteiro limpo é ignorado — cai-se no
    escalão seguinte da precedência, que é sempre seguro.
    """
    if isinstance(valor, bool) or valor is None:
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _aplicar_clamp(chave: str, valor: int) -> int:
    minimo, maximo = CLAMPS[chave]
    return max(minimo, min(maximo, valor))


def _do_tenant(config: dict[str, Any] | None, chave: str) -> int | None:
    if not isinstance(config, dict):
        return None
    valor = _inteiro(config.get(chave))
    return None if valor is None else _aplicar_clamp(chave, valor)


def configuravel() -> bool:
    """A política só se configura on-prem — em SaaS é fixa e não visível."""
    return settings.DEPLOYMENT_MODE == "onprem"


def politica(db: Session, empresa_id) -> Politica:
    """Resolve a política em vigor para uma empresa.

    Sem cache, de propósito. É um `SELECT` por chave primária e o on-prem tem uma
    empresa só; cache introduz invalidação, e invalidação introduz o bug em que
    alguém muda a política e ela não pega. Medir primeiro, otimizar depois se
    doer.
    """
    seguranca: dict[str, Any] | None = None
    notificacoes: dict[str, Any] | None = None

    if configuravel() and empresa_id is not None:
        from app.empresas.models import Empresa

        empresa = db.get(Empresa, empresa_id)
        if empresa is not None:
            seguranca = empresa.config_seguranca
            notificacoes = empresa.config_notificacoes

    password_min = _do_tenant(seguranca, "password_min") or PASSWORD_MIN
    retencao = _do_tenant(seguranca, "backup_retencao_dias") or settings.BACKUP_RETENCAO
    upload = _do_tenant(seguranca, "max_upload_mb") or settings.MAX_UPLOAD_SIZE_MB

    email = settings.EMAIL_NOTIFICACOES
    if isinstance(notificacoes, dict) and isinstance(notificacoes.get("email"), bool):
        # O interruptor do tenant só DESLIGA. Se a instalação não tem email
        # configurado, ligá-lo aqui prometeria avisos que nunca sairiam.
        email = email and notificacoes["email"]

    return Politica(
        password_min=_aplicar_clamp("password_min", password_min),
        backup_retencao_dias=_aplicar_clamp("backup_retencao_dias", retencao),
        max_upload_mb=_aplicar_clamp("max_upload_mb", upload),
        email_notificacoes=email,
    )


# Chaves que o tenant pode mesmo alterar. Uma chave fora desta lista é ignorada
# em vez de gravada: guardar o que não se aplica é como isto começou.
CHAVES_SEGURANCA = frozenset(CLAMPS)
CHAVES_NOTIFICACOES = frozenset({"email"})

# Interruptores que NUNCA se desligam por esta via, mesmo que alguém os envie.
# Validado aqui e não só na UI — esta camada existe porque o que estava só na UI
# não estava em lado nenhum.
NUNCA_DESLIGAVEIS = frozenset({"exigir_2fa", "lockout_ativo"})


def sanear_config(
    recebido: dict[str, Any] | None, *, chaves_validas: frozenset[str]
) -> dict[str, Any]:
    """Reduz o que chegou do cliente ao que é aceitável guardar.

    Descarta chaves desconhecidas, normaliza tipos e aplica os clamps **antes** de
    gravar, para que o que fica na base seja o que vai ser aplicado. Guardar um
    valor e aplicar outro é a origem exata deste achado.
    """
    if not isinstance(recebido, dict):
        return {}
    saneado: dict[str, Any] = {}
    for chave, valor in recebido.items():
        if chave in NUNCA_DESLIGAVEIS:
            continue
        if chave not in chaves_validas:
            continue
        if chave in CLAMPS:
            numero = _inteiro(valor)
            if numero is not None:
                saneado[chave] = _aplicar_clamp(chave, numero)
        elif isinstance(valor, bool):
            saneado[chave] = valor
    return saneado


def config_efetiva(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """A configuração como vai ser aplicada, para a mostrar.

    Um valor gravado antes de um limite subir (um `password_min: 8` de quando o
    chão era 8) continua na base, mas aplica-se o do limite. Mostrar o gravado
    seria mostrar uma política que não está em vigor.
    """
    if not isinstance(config, dict):
        return config
    efetiva = dict(config)
    for chave in CLAMPS:
        numero = _inteiro(efetiva.get(chave))
        if numero is None:
            efetiva.pop(chave, None)
        else:
            efetiva[chave] = _aplicar_clamp(chave, numero)
    return efetiva


def password_min(db: Session | None, empresa_id) -> int:
    """O comprimento mínimo em vigor: o da empresa ou, sem empresa, o da plataforma."""
    if db is None or empresa_id is None:
        return PASSWORD_MIN
    return politica(db, empresa_id).password_min


def exigir_password_valida(password: str, *, db: Session | None = None, empresa_id=None) -> None:
    """400 se a password não cumprir a regra da plataforma e o mínimo da empresa.

    É a única verificação de força de password: todos os caminhos que definem uma
    passam por aqui. Sem empresa (setup, registo, conta de operador) vale o mínimo
    da plataforma.
    """
    valida, mensagem = validar_forca_password(password, minimo=password_min(db, empresa_id))
    if not valida:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=mensagem)
