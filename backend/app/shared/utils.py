"""
Utilitários partilhados: geração de tokens, validação de passwords,
locale e helpers de autenticação.
"""
import hashlib
import re
import secrets
from typing import Any

from argon2 import PasswordHasher, Type
from fastapi import HTTPException, status
from jose import JWTError, jwt


# ---------------------------------------------------------------------------
# Rede — IP do cliente (resistente a spoofing)
# ---------------------------------------------------------------------------

def _peer_e_proxy_confiavel(request: Any, confiaveis: list[str]) -> bool:
    """True se o peer do socket (quem falou mesmo com o backend) é um proxy de
    confiança, e portanto o X-Real-IP que ele pôs pode ser acreditado.

    Sem lista de proxies (o padrão), devolve True: o comportamento fica como
    sempre foi, para não partir instalações onde o IP da bridge do Docker varia.
    Com lista definida, um contentor da rede interna que fale direto com o backend
    (e não seja o nginx) não é de confiança — o X-Real-IP que escolher é ignorado,
    e os limites por endereço passam a contar o IP real dele (CWE-348)."""
    if not confiaveis:
        return True
    client = getattr(request, "client", None)
    peer = getattr(client, "host", None) if client else None
    if not peer:
        return False
    import ipaddress

    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for cidr in confiaveis:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def obter_ip_cliente(request: Any) -> str:
    """Resolve o IP real do cliente de forma resistente a spoofing (CWE-348).

    Fonte única partilhada pela auditoria e pelo rate limiting:
    - CF-Connecting-IP só é considerado quando TRUST_CLOUDFLARE_HEADERS=True
      (deployment atrás de Cloudflare Tunnel, onde o edge injecta o header e o
      cliente final não o consegue forjar).
    - Caso contrário usa-se X-Real-IP, que o Nginx define com $remote_addr — valor
      que o cliente não controla. É a fonte fidedigna no modo on-prem.
    - Os cabeçalhos acima só são acreditados quando o peer do socket é um proxy de
      confiança (PROXY_IP_CONFIAVEL). Sem essa lista, aceitam-se como antes.
    - Fallback: endereço do socket (ligação directa / desenvolvimento).
    """
    from app.config import get_settings

    settings = get_settings()
    confiavel = _peer_e_proxy_confiavel(request, settings.PROXY_IP_CONFIAVEL)

    if confiavel and settings.TRUST_CLOUDFLARE_HEADERS:
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.split(",")[0].strip()[:45]

    if confiavel:
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.split(",")[0].strip()[:45]

    client = getattr(request, "client", None)
    if client:
        return str(client.host)[:45]

    return "unknown"


def chave_de_limite_ip(ip: str) -> str:
    """A chave com que os limites por IP contam um cliente.

    Em IPv4, o próprio endereço. Em IPv6, o /64 a que pertence: um fornecedor de
    acesso dá a um só cliente um /64 inteiro (2^64 endereços), e contar cada
    endereço à parte deixava-o rodar a origem a cada pedido e nunca chegar ao
    limite. Um IPv4 escrito como IPv6 (`::ffff:a.b.c.d`) conta como o IPv4. Um
    valor que não seja um endereço fica como veio.

    Serve só para contar: a trilha de auditoria guarda o endereço completo.
    """
    import ipaddress

    try:
        endereco = ipaddress.ip_address(ip.strip())
    except ValueError:
        return ip
    if endereco.version == 6:
        if endereco.ipv4_mapped is not None:
            return str(endereco.ipv4_mapped)
        return str(ipaddress.ip_network(f"{endereco}/64", strict=False))
    return str(endereco)


def obter_chave_limite(request: Any) -> str:
    """Chave dos limites por IP (slowapi): o IP do cliente reduzido ao /64."""
    return chave_de_limite_ip(obter_ip_cliente(request))


def pedido_e_seguro(request: Any) -> bool:
    """True se o pedido chegou via HTTPS (CWE-614 / CWE-1004).

    Usa o X-Forwarded-Proto definido pelo Nginx (`$scheme`, não controlável pelo
    cliente) e cai no esquema do próprio pedido. Permite definir o atributo Secure
    dos cookies em função da ligação real, em vez de o inferir de uma string de
    configuração (APP_URL) — o cookie fica Secure exactamente quando se serve HTTPS,
    sem reinício e independentemente de o endereço ser IP ou domínio.
    """
    xfp = request.headers.get("x-forwarded-proto", "")
    if xfp and xfp.split(",")[0].strip().lower() == "https":
        return True
    url = getattr(request, "url", None)
    return getattr(url, "scheme", None) == "https"


def host_e_dominio(host: str | None) -> bool:
    """True se `host` for um nome de domínio (não um IP literal nem localhost).

    Usado para decidir se é seguro enviar HSTS: num deployment por IP — forçosamente
    com certificado self-signed — o HSTS impediria o click-through do aviso de
    certificado e poderia trancar o acesso ao browser. Espera um hostname já sem porta
    (ex.: `urlparse(APP_URL).hostname`).
    """
    if not host:
        return False
    import ipaddress

    h = host.strip().lower()
    if h == "localhost":
        return False
    try:
        ipaddress.ip_address(h)
        return False  # é um IP literal
    except ValueError:
        return True  # é um nome de domínio


def normalizar_host_rede(host: str) -> str:
    """Valida um host de servidor (nome de domínio ou IP) e devolve-o normalizado.

    Levanta `ValueError` com uma mensagem que diz o que está errado. Um endereço
    de servidor mal escrito só dava erro na altura de ligar — longe do ecrã onde
    foi escrito, e sem dizer que o problema era esse.

    Aceita IPv4 e IPv6 literais: um relé de correio interno é muitas vezes um IP.
    """
    import ipaddress

    h = (host or "").strip().lower()
    if not h:
        raise ValueError("O endereço do servidor é obrigatório.")
    if len(h) > 255:
        raise ValueError("O endereço do servidor é demasiado longo.")
    if "://" in h:
        raise ValueError("Indique apenas o endereço do servidor, sem http:// nem https://.")
    if any(c.isspace() for c in h) or any(ord(c) < 32 or ord(c) == 127 for c in h):
        raise ValueError("O endereço do servidor não pode conter espaços.")

    # Um IP literal é um host legítimo e não segue as regras de nomes de domínio.
    try:
        ipaddress.ip_address(h)
        return h
    except ValueError:
        pass

    if "/" in h or "@" in h or ":" in h:
        raise ValueError("Indique apenas o endereço do servidor, sem porta nem caminho.")
    etiquetas = h.rstrip(".").split(".")
    for etiqueta in etiquetas:
        if not 1 <= len(etiqueta) <= 63:
            raise ValueError("O endereço do servidor não é um nome válido.")
        if etiqueta.startswith("-") or etiqueta.endswith("-"):
            raise ValueError("O endereço do servidor não é um nome válido.")
        # O sublinhado não é de um nome de domínio público, mas é o de um serviço
        # na rede do Docker (`nis2pme_mailpit`), que resolve e é o caso normal de
        # um relé no mesmo compose. Recusá-lo impedia gravar a configuração que já
        # estava em vigor.
        if not all(c.isalnum() and c.isascii() or c in "-_" for c in etiqueta):
            raise ValueError("O endereço do servidor não é um nome válido.")
    return h


# ---------------------------------------------------------------------------
# Tokens seguros
# ---------------------------------------------------------------------------

def decodificar_jwt_temp(token: str, secret: str, tipo_esperado: str, algoritmo: str = "HS256") -> dict:
    """Valida e decodifica um token temporário JWT. Lança 401 se inválido ou tipo errado.

    Genérico: o segredo é passado pelo chamador, não fixo neste módulo.
    """
    try:
        payload = jwt.decode(token, secret, algorithms=[algoritmo])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token temporário inválido ou expirado.",
        )
    if payload.get("type") != tipo_esperado:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token temporário inválido para esta operação.",
        )
    return payload


def gerar_token_opaco(nbytes: int = 32) -> str:
    """
    Gera um token opaco criptograficamente seguro (URL-safe base64).
    Usado para refresh tokens e reset de password.

    Returns:
        Token em texto limpo — para enviar ao cliente UMA vez.
        Armazena sempre o hash SHA-256, nunca o token em texto limpo.
    """
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """
    Calcula o SHA-256 hex do token para armazenamento seguro na DB.
    Não usar bcrypt aqui — SHA-256 é suficiente para tokens com entropia alta.
    """
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Hashing de password — argon2id
# ---------------------------------------------------------------------------

# Os parâmetros são fixados aqui, não herdados dos defaults do argon2-cffi. O
# custo de um hash é um parâmetro de segurança e não deve mudar sozinho quando a
# versão da biblioteca muda. Os valores são os que a biblioteca usa hoje, por
# isso fixá-los não invalida nenhum hash existente.
#
# Um hash argon2 transporta os parâmetros com que foi gerado, por isso alterar
# estes valores só afeta hashes NOVOS — os antigos continuam a validar, com o
# custo antigo, até serem gerados de novo.
#
# Antes de subir: 64 MiB são reservados por verificação em curso, e o container
# do backend tem um teto de memória. Medir com o número real de logins
# concorrentes.
ARGON2_TIME_COST = 3        # iterações
ARGON2_MEMORY_COST = 65536  # 64 MiB por verificação
ARGON2_PARALLELISM = 4      # lanes
ARGON2_HASH_LEN = 32        # bytes de saída
ARGON2_SALT_LEN = 16        # bytes de salt aleatório


def criar_password_hasher() -> PasswordHasher:
    """
    Devolve um PasswordHasher com os parâmetros acima.

    Fonte única: tudo o que gera ou verifica hashes de password chama esta
    função, para não existirem dois custos diferentes na mesma instalação.
    """
    return PasswordHasher(
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_HASH_LEN,
        salt_len=ARGON2_SALT_LEN,
        type=Type.ID,  # híbrido: resiste a side-channels e a GPU
    )


# ---------------------------------------------------------------------------
# Validação de password
# ---------------------------------------------------------------------------

# A regra é uma só, para todas as contas: comprimento mínimo, um dígito, uma
# maiúscula e um carácter especial. Os três frontends têm uma cópia destas duas
# constantes, e um teste falha se deixarem de coincidir.
PASSWORD_MIN = 12
PASSWORD_ESPECIAIS = "!@#$%^&*()_+-=[]{};':\"\\|,.<>/?"


def validar_forca_password(password: str, minimo: int = PASSWORD_MIN) -> tuple[bool, str]:
    """
    Valida uma password contra a regra da plataforma.

    `minimo` é o da política da empresa, quando quem chama a conhece; nunca desce
    abaixo de `PASSWORD_MIN`. Dígitos e maiúsculas contam-se só em ASCII, como no
    browser — senão um «٣» passava aqui e era recusado lá.

    Returns:
        Tuple (valida, mensagem_erro).
        Se valida=True, mensagem_erro é string vazia.
    """
    minimo = max(PASSWORD_MIN, minimo)
    if len(password) < minimo:
        return False, f"A password deve ter pelo menos {minimo} caracteres."
    if not any("0" <= c <= "9" for c in password):
        return False, "A password deve conter pelo menos um dígito."
    if not any("A" <= c <= "Z" for c in password):
        return False, "A password deve conter pelo menos uma letra maiúscula."
    if not any(c in PASSWORD_ESPECIAIS for c in password):
        return False, "A password deve conter pelo menos um carácter especial."
    return True, ""


# ---------------------------------------------------------------------------
# Códigos de backup 2FA
# ---------------------------------------------------------------------------

# Alfabeto dos códigos de backup, sem caracteres ambíguos (0/O, 1/I/l), e o
# comprimento normalizado (12 chars = três grupos de quatro).
ALFABETO_CODIGO_BACKUP = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
COMPRIMENTO_CODIGO_BACKUP = 12


def gerar_codigos_backup(n: int = 10) -> list[str]:
    """
    Gera n códigos de backup para 2FA no formato XXXX-XXXX-XXXX.
    Cada código tem 12 chars alfanuméricos (sem ambiguidade: sem 0/O, 1/I/l).

    Returns:
        Lista de n códigos em texto limpo — mostrar ao utilizador UMA vez.
        Armazenar sempre os hashes na DB, nunca o texto.
    """
    alphabet = ALFABETO_CODIGO_BACKUP
    codigos = []
    for _ in range(n):
        parte1 = "".join(secrets.choice(alphabet) for _ in range(4))
        parte2 = "".join(secrets.choice(alphabet) for _ in range(4))
        parte3 = "".join(secrets.choice(alphabet) for _ in range(4))
        codigos.append(f"{parte1}-{parte2}-{parte3}")
    return codigos


def normalizar_codigo_backup(codigo: str) -> str:
    """Normaliza um código de backup: uppercase e remove hífens e espaços."""
    return codigo.upper().replace("-", "").replace(" ", "")


def tem_formato_de_codigo_backup(codigo_normalizado: str) -> bool:
    """Diz se um código já normalizado PODE ser um código de backup.

    Um código que não tem o comprimento nem o alfabeto dos códigos gerados
    (um TOTP de 6 dígitos, por exemplo) nunca vai bater com hash nenhum — e
    cada comparação custa uma verificação argon2id. Filtrar pela forma evita
    pagar dez dessas por cada TOTP errado, sem enfraquecer nada: a resposta
    para um código com a forma certa continua a depender só do hash.
    """
    return len(codigo_normalizado) == COMPRIMENTO_CODIGO_BACKUP and all(
        c in ALFABETO_CODIGO_BACKUP for c in codigo_normalizado
    )


# ---------------------------------------------------------------------------
# Locale helpers
# ---------------------------------------------------------------------------

def parse_accept_language(header: str | None) -> str | None:
    """
    Extrai o primary language tag do header Accept-Language.

    Ex: 'pt-PT,pt;q=0.9,en;q=0.8' → 'pt'
    Ex: 'en' → 'en'
    Ex: None → None
    """
    if not header:
        return None
    # Primeiro tag (antes da vírgula), sem qualidade nem região
    primary = header.split(",")[0].split(";")[0].strip()
    lang = primary.split("-")[0].lower()
    return lang if lang else None


def resolver_locale(
    empresa: Any,
    framework: Any,
    *,
    request: Any | None = None,
) -> str:
    """Resolve o locale para operações de dados.

    Ordem de preferência: Accept-Language header → empresa.locale_preferido → framework.default_locale.
    Ponto único partilhado entre relatorios, controlos, evidencias e plano_prioritario.
    """
    if request is not None:
        locale_header = parse_accept_language(
            getattr(request, "headers", {}).get("accept-language")
        )
        if locale_header:
            return locale_header
    return getattr(empresa, "locale_preferido", None) or getattr(framework, "default_locale", "pt")


def content_disposition_anexo(nome: str) -> str:
    """Cabeçalho `Content-Disposition` para descarregar um ficheiro com qualquer nome.

    Os cabeçalhos HTTP vão em Latin-1: um nome em cirílico ou polaco rebentava
    com 500. Leva um nome ASCII de recurso (para clientes antigos) e o nome
    verdadeiro em `filename*` (RFC 5987), que os browsers preferem.
    """
    import unicodedata
    from urllib.parse import quote

    ascii_nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode("ascii")
    ascii_nome = re.sub(r'[^A-Za-z0-9._ -]', "_", ascii_nome).strip() or "ficheiro"
    return f"attachment; filename=\"{ascii_nome}\"; filename*=UTF-8''{quote(nome, safe='')}"


# Uma célula que começa por um destes caracteres é interpretada como fórmula
# pelas folhas de cálculo. O conteúdo das exportações vem de campos que um
# utilizador (ou um atacante) escolhe — o título de uma evidência, o nome de uma
# empresa registada no trial — e sem isto a exportação transporta código que é
# executado ao abrir o ficheiro.
PREFIXOS_DE_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def celula_csv(valor: Any) -> str:
    """Prepara um valor para uma célula de CSV, neutralizando fórmulas.

    O valor fica como está, só com uma aspa simples à frente quando começaria
    uma fórmula: a folha de cálculo mostra o texto e não o executa."""
    if valor is None:
        return ""
    texto = str(valor)
    if texto.startswith(PREFIXOS_DE_FORMULA):
        return "'" + texto
    return texto
