"""
Configuração centralizada da aplicação via variáveis de ambiente.
Usa pydantic-settings para validação automática dos valores.
"""
import os
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _validar_chave_fernet(v: str, nome: str) -> str:
    """
    Valida que `v` é uma chave Fernet (base64 url-safe, 32 bytes) ou vazio.

    Vazio é aceite (a chave é auto-gerada no entrypoint ou a cifra fica desativada).
    Rejeita o valor de placeholder e formatos inválidos com uma mensagem que indica
    como gerar uma chave correta.
    """
    import base64

    if not v:
        return v
    if v.startswith("CHANGE_ME"):
        raise ValueError(
            f"{nome} não pode ser o valor padrão. "
            "Gerar com: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        if len(base64.urlsafe_b64decode(v)) != 32:
            raise ValueError("Comprimento incorreto")
    except Exception:
        raise ValueError(
            f"{nome} inválida — deve ser uma chave Fernet (base64 url-safe, 32 bytes). "
            "Gerar com: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return v


class Settings(BaseSettings):
    """
    Todas as configurações são lidas de variáveis de ambiente (ou .env).
    Valores sem default SÃO OBRIGATÓRIOS — a app não arranca sem eles.
    """

    # --- Aplicação ---
    APP_NAME: str = "NIS2PME"
    APP_VERSION: str = "0.4.1"
    DEBUG: bool = False
    # Activar Swagger UI / ReDoc explicitamente, independente do flag DEBUG.
    # Em produção deve ser False mesmo que DEBUG fique True acidentalmente (CWE-215).
    ENABLE_API_DOCS: bool = False
    DEPLOYMENT_MODE: str = "onprem"  # "saas" | "onprem"
    # Distingue o SaaS de avaliação (trial) do SaaS pago. Só relevante quando
    # DEPLOYMENT_MODE=saas; seleciona a variante "saas-trial" dos textos legais
    # (conta de teste, dados eliminados aos 14 dias, não enviar PII).
    IS_TRIAL: bool = False
    APP_URL: str = "http://localhost:5173"

    # --- Proxy / rede ---
    # Confiar no header CF-Connecting-IP para determinar o IP do cliente.
    # SÓ activar quando a aplicação está atrás de Cloudflare Tunnel (o edge injecta
    # o header de forma fidedigna). Em on-prem com acesso directo via Nginx DEVE
    # ficar False — caso contrário qualquer cliente forja o IP nos audit logs e
    # contorna o rate limiting (CWE-348). On-prem usa o X-Real-IP definido pelo Nginx.
    TRUST_CLOUDFLARE_HEADERS: bool = False

    # --- SaaS / trial ---
    # Token interno partilhado com a borda de signup (saas-trial). Em modo SaaS o
    # registo público só é aceite quando acompanhado deste token: o core não é
    # exposto diretamente à internet — a borda é a única que invoca o /register.
    # Vazio em on-prem (lá o registo público está desativado).
    # Chega em ficheiro (SAAS_TRIAL_INTERNAL_TOKEN_FILE, os `secrets:` do compose).
    SAAS_TRIAL_INTERNAL_TOKEN: str = Field(default="", repr=False, validate_default=True)
    # Dias entre a suspensão do trial e a eliminação dos dados — o mesmo valor
    # que a borda e o superadmin usam; aqui só serve para o avisar.
    SAAS_TRIAL_GRACE_DIAS: int = 5
    # Teto do prazo do trial que a borda de registo pode pedir (o público usa 14;
    # a VM de teste, 30). Um fim de trial mais longínquo é recusado no registo.
    SAAS_TRIAL_DIAS_MAX: int = 31
    # Página pública dos planos, para o botão da faixa de fim do trial. Vazia =
    # a faixa diz só para contactar a equipa.
    SAAS_PLANOS_URL: str = ""

    # Token de gestão privilegiada de tenants (mecanismo, não política): autoriza
    # suspender um trial via API interna. NUNCA pela borda de signup
    # (separação de privilégios face ao SAAS_TRIAL_INTERNAL_TOKEN, que só cria).
    # Vazio → o router interno nem é montado (e só é montado em DEPLOYMENT_MODE=saas).
    CORE_SUSPEND_TOKEN: str = Field(default="", repr=False, validate_default=True)

    # --- TLS (apenas on-prem) ---
    # Em saas o TLS é sempre tratado a montante (Cloudflare) e estas variáveis são
    # ignoradas. Em on-prem, a postura é decidida no installer e aplicada no 1.º
    # arranque (pode ser alterada depois no wizard):
    #   self-signed : edge gera certificado autoassinado (default; sem HSTS).
    #   custom      : edge usa o certificado de confiança em TLS_CERT_PATH/TLS_KEY_PATH (com HSTS).
    #   proxy       : TLS a montante; edge serve HTTP e preserva o X-Forwarded-Proto recebido.
    TLS_MODE: str = "self-signed"
    TLS_CERT_PATH: str = ""   # caminho do certificado (modo custom)
    TLS_KEY_PATH: str = ""    # caminho da chave privada (modo custom)

    @field_validator("SAAS_TRIAL_INTERNAL_TOKEN", "CORE_SUSPEND_TOKEN", "RESEND_API_KEY", mode="before")
    @classmethod
    def ler_segredo_de_ficheiro(cls, v, info):
        """Os segredos partilhados com outros serviços chegam em ficheiro (`<NOME>_FILE`,
        os `secrets:` do compose), fora do ambiente do processo; com o ficheiro, manda
        ele. Sem ele, vale o valor do ambiente ou do .env, como antes. Todo o campo
        daqui que o gen-secrets.sh guarda em segredos/ tem de estar nesta lista."""
        if os.getenv(f"{info.field_name}_FILE"):
            from app.shared.segredo import ler_segredo

            return ler_segredo(info.field_name)
        return v

    @field_validator("TLS_MODE")
    @classmethod
    def validar_tls_mode(cls, v: str) -> str:
        """Garante que TLS_MODE é um dos valores aceites."""
        v = (v or "self-signed").strip().lower()
        if v not in ("self-signed", "proxy", "custom"):
            raise ValueError("TLS_MODE deve ser 'self-signed', 'proxy' ou 'custom'")
        return v

    # --- Verificação de atualizações ---
    # A app verifica periodicamente se há nova versão; o pedido envia apenas um
    # identificador anónimo da instância + a versão (nunca dados de clientes).
    # Desligável (VERIFY_UPDATES=false).
    VERIFY_UPDATES: bool = True

    # --- Backups (on-prem) ---
    # Nº máximo de backups guardados em /app/data/backups — os mais antigos são
    # apagados ANTES de criar um novo (a retenção nunca deixa o disco encher).
    BACKUP_RETENCAO: int = 7
    UPDATE_CHECK_URL: str = "https://update.nis2pme.pt/v1/check-updates"
    # Pasta partilhada com o agente de atualização do anfitrião: `pedido/` (escreve
    # o backend) e `estado/` (escreve o agente; entra só em leitura).
    ATUALIZACAO_DIR: str = "/app/atualizacao"
    # Canal de atualizações: `stable` (as instalações de clientes) ou `dev` (as de
    # desenvolvimento do fornecedor, que seguem builds de teste). O canal `dev` só
    # recebe resposta com o token certo e com um manifesto assinado para esse canal;
    # uma instalação `stable` nunca aceita nada de `dev`, mesmo que alguém o repita.
    UPDATE_CHANNEL: str = "stable"
    UPDATE_CHANNEL_TOKEN: str = ""

    @field_validator("UPDATE_CHANNEL")
    @classmethod
    def validar_canal_atualizacoes(cls, v: str) -> str:
        v = (v or "stable").strip().lower()
        if v not in ("stable", "dev"):
            raise ValueError("UPDATE_CHANNEL deve ser 'stable' ou 'dev'")
        return v

    # --- Diretório de auditores (ecossistema) ---
    # URL do serviço público que lista os auditores com selo verificado. A app
    # só o contacta quando um utilizador abre "Encontrar auditor", e envia
    # apenas os filtros escritos. Vazio = página indisponível, zero egresso.
    DIRETORIO_AUDITORES_URL: str = ""

    # Hosts do relay para onde o core envia um dossiê (separados por vírgula).
    # O URL do relay vem no convite, de fora: sem esta lista, um convite forjado
    # punha o servidor a fazer pedidos a qualquer endereço que ele alcance. A
    # plataforma do auditor só conhece o relay do fornecedor. Um relay de
    # desenvolvimento em http tem de estar aqui (ex.: localhost).
    RELAY_HOSTS_PERMITIDOS: str = "relay.nis2pme.pt"

    # CA privada do fornecedor (PEM, ou base64 do PEM). O relay dos dossiês e o
    # diretório de auditores podem ser servidos com um certificado dela: o core
    # junta-a às CAs públicas só nesses pedidos. Vazio = só as CAs públicas.
    NIS2PME_TLS_CA_PEM: str = ""

    # --- Ecossistema do dossiê: chave pública mestra NIS2PME ---
    # Valida as atestações de chave (a boleia que dispensa a confirmação manual do
    # fingerprint nos convites). É pública: pode viver no compose publicado.
    # Vazia = as atestações ficam "presentes, não verificadas" — nunca é gate.
    NIS2PME_MESTRA_PUBKEY: str = ""

    # --- Retenção do audit log ---
    # Dias que os registos de auditoria ficam VISÍVEIS na base. Ao passarem esta
    # janela são arquivados em /app/data/audit-archive e só depois apagados da
    # tabela — a retenção controla o que está à vista, não o que existe. 0 = nunca
    # purgar. Não há piso: o admin é livre de baixar o valor porque o arquivo
    # preserva a trilha na mesma. (Se algum dia se passar a apagar sem arquivar,
    # o piso volta a ser indispensável.)
    AUDIT_RETENCAO_DIAS: int = 365

    # Teto de linhas por exportação da trilha de auditoria. Acima disto o pedido
    # é recusado a pedir um intervalo de datas mais curto: um só clique não pode
    # arrancar uma leitura que nunca acaba, e o ficheiro resultante já não seria
    # utilizável de qualquer maneira.
    AUDIT_EXPORT_MAX_LINHAS: int = 100_000

    # --- Retenção das notificações ---
    # Dias que uma notificação JÁ LIDA fica guardada. Ao contrário da trilha,
    # não há arquivo: uma notificação é um lembrete, e o facto que a originou
    # continua na trilha e no módulo de onde veio. As não lidas nunca são
    # apagadas, tenham a idade que tiverem — são trabalho por fazer.
    NOTIF_RETENCAO_DIAS: int = 90

    # Dias que uma evidência sem controlo associado fica na reciclagem antes de
    # ser apagada. Enquanto lá está aparece numa lista, conta na quota e pode ser
    # religada — é o "desfazer" de quem retirou a prova do controlo errado.
    # Zero desliga a varredura: as órfãs acumulam-se e ninguém lhes toca.
    EVIDENCIA_ORFA_DIAS: int = 30

    # Prazo de conservação, em anos, de uma evidência que já foi prova e deixou
    # de sustentar qualquer controlo. Passado o prazo deixa de estar retida e
    # segue para a reciclagem. Zero = sem prazo: fica até alguém a apagar com
    # lápide. O valor é uma decisão do responsável pelo tratamento de dados e
    # não tem um defeito que sirva a todos.
    EVIDENCIA_RETENCAO_ANOS: int = 0

    # --- Premium (open-core ↔ módulos premium privados) ---
    # O core fala com o sidecar premium via gRPC (contrato premium.v1). DESLIGADO
    # por defeito: o open-core funciona sem qualquer sidecar. Ligar requer o
    # sidecar a correr e PREMIUM_SIDECAR_ADDR definido.
    PREMIUM_ENABLED: bool = False
    PREMIUM_SIDECAR_ADDR: str = ""           # ex.: "premium-sidecar:50051"
    PREMIUM_ENTITLEMENT_CACHE_TTL: int = 60  # segundos — cache curta

    # --- Selagem de envelope (custódia de dados premium) ---
    # Cifra-envelope X25519 (sealed box): chave PÚBLICA do gateway, em base64. O core
    # sela com ela o payload de cliente antes de o entregar ao sidecar; só o worker
    # (chave privada) decifra. KID etiqueta a chave (rotação). Fail-closed: sem chave
    # o core RECUSA selar — exceto se DEV_PLAINTEXT=true (só dev). O ciclo de vida do
    # job da IA (admissão/submissão/polling/estado) é do sidecar, não do core.
    PREMIUM_ENVELOPE_PUBKEY: str = ""
    PREMIUM_ENVELOPE_KID: str = ""
    PREMIUM_ENVELOPE_DEV_PLAINTEXT: bool = False
    # Teto do envelope de evidências que sai do núcleo para uma análise (bytes).
    # O que não cabe fica de fora, marcado, e o modelo sabe que existe. É o
    # primeiro elo de uma cadeia que tem de bater certo: o sidecar recusa um
    # envelope acima de 32 MiB; manda-o ao gateway em base64 (×4/3), e o ingress
    # e a borda `ia.` aceitam até 48 MiB. Ao mudar um, mudar os outros; o teste
    # da cadeia de tetos confere os quatro. 32 MiB de envelope são ~24 MiB de
    # payload, ~18 MB de ficheiros; o modelo lê no máximo 120 000 caracteres.
    PREMIUM_ENVELOPE_MAX_BYTES: int = 32 * 1024 * 1024

    # --- Base de dados ---
    DATABASE_URL: str
    # SQL echo separado de DEBUG para não expor queries com parâmetros em produção (CWE-532)
    SQL_ECHO: bool = False

    @field_validator("DATABASE_URL")
    @classmethod
    def _password_codificada(cls, v: str) -> str:
        # O compose cola a password do .env crua na URL; com @, /, : ou espaço a URL
        # deixava de se ler. Ver app/shared/url_base.py.
        from app.shared.url_base import codificar_password
        return codificar_password(v, os.environ.get("DB_PASSWORD"))


    # --- JWT (tenants) ---
    JWT_SECRET_KEY: str = ""
    # Reservado, não usado. Os refresh tokens são opacos — aleatórios + SHA-256 na base
    # de dados, ver auth/service.py — e validam-se por consulta à tabela, sem assinatura.
    # Esta chave não assina nem valida nada. Mantida porque as instalações existentes já
    # a têm no auto-secrets.env; removê-la partia-lhes o arranque sem ganho nenhum.
    JWT_REFRESH_SECRET_KEY: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    # Um refresh já rodado que volta a aparecer é uma cópia: termina as sessões
    # da conta. Menos quando chega logo a seguir à rotação — dois separadores do
    # mesmo browser que renovam ao mesmo tempo, ou uma resposta que se perdeu e o
    # pedido repetido. A janela só poupa esse caso: o token continua recusado.
    REFRESH_REUTILIZACAO_TOLERANCIA_S: int = 60

    # --- Bloqueio de conta no login (anti-brute-force por conta) ---
    # O rate-limit por IP (slowapi) não trava ataques distribuídos que rodam
    # muitos IPs contra UMA conta. Após LOGIN_MAX_TENTATIVAS falhas DENTRO de
    # LOGIN_JANELA_MINUTOS a conta fica bloqueada por LOGIN_BLOQUEIO_MINUTOS
    # (auto-expira). A janela evita trancar quem erra a password esporadicamente.
    LOGIN_MAX_TENTATIVAS: int = 5
    LOGIN_JANELA_MINUTOS: int = 15
    LOGIN_BLOQUEIO_MINUTOS: int = 15

    # Acumulador por IP (anti password-spray): conta falhas do mesmo IP entre
    # TODAS as contas (e contra emails inexistentes) numa janela deslizante. O
    # limiar é ALTO de propósito — um escritório inteiro atrás de um só IP (NAT,
    # comum em PME) não deve ser trancado por erros esporádicos. Resposta 429
    # (escopo de IP, não revela existência de conta). Complementa o slowapi 5/min.
    LOGIN_IP_MAX_FALHAS: int = 20
    LOGIN_IP_JANELA_MINUTOS: int = 15
    LOGIN_IP_BLOQUEIO_MINUTOS: int = 15

    # --- Limites do login por endereço e por GRUPO de endereços ---
    # Um cliente com IPv6 recebe um /64 inteiro (2^64 endereços) e um alojamento
    # dá muitas vezes um /48 (65 536 /64). Contar só o /64 (ou o IPv4) deixava um
    # atacante rodar prefixos e nunca chegar ao limite. Além do endereço, conta-se
    # o grupo: o /48 (IPv6) e o /24 (IPv4). Um escritório atrás de um só IPv4 (NAT)
    # partilha o /24 — é a camada do cookie de dispositivo que o protege.
    LOGIN_ENDERECO_POR_MINUTO: int = 5     # por /64 (IPv6) ou endereço (IPv4)
    LOGIN_GRUPO_POR_MINUTO: int = 20       # por /48 (IPv6) ou /24 (IPv4)
    LOGIN_RESET_GRUPO_POR_MINUTO: int = 10  # o mesmo, no pedido de recuperação

    # --- Camada 2: cookie de dispositivo (OWASP device cookie) ---
    # Depois de um login completo (com 2FA), o servidor põe um cookie assinado,
    # preso ao utilizador. Quem volta com um cookie válido para ESSE utilizador não
    # conta nos limites por endereço, tem uma vaga de argon2 reservada e o seu
    # próprio contador de falhas, em vez do bloqueio da conta. A chave é derivada
    # do JWT_SECRET_KEY (HKDF), não é um segredo novo.
    DISPOSITIVO_COOKIE_DIAS: int = 90       # validade longa
    DISPOSITIVO_MAX_FALHAS: int = 5         # falhas seguidas → o cookie deixa de valer
    DISPOSITIVO_POR_MINUTO: int = 5         # limite do login por cookie

    # --- Camada 3: prova de trabalho só sob carga (estilo ALTCHA) ---
    # Quando a fila de argon2 dos desconhecidos aperta, ou um endereço/grupo
    # acumula falhas, o login passa a exigir a solução de um desafio SHA-256. Sem
    # carga é invisível. A dificuldade é o teto do número secreto (~metade em
    # média): calibrado para ~0,5–1 s num portátil e multiplicado sob pressão.
    DESAFIO_DIFICULDADE_BASE: int = 200_000
    DESAFIO_DIFICULDADE_MAX: int = 2_000_000
    DESAFIO_VALIDADE_S: int = 120           # curta; uso único
    DESAFIO_FALHAS_PARA_EXIGIR: int = 10    # falhas por endereço/grupo que o ligam

    # Peer(s) em quem confiar para o cabeçalho X-Real-IP (CWE-348). O X-Real-IP é
    # posto pelo nginx à frente; um contentor da rede interna que fale direto com
    # o backend podia escolhê-lo e contornar os limites por endereço. Com esta
    # lista definida, o X-Real-IP só é aceite quando o peer do socket está nela
    # (dar ao proxy um IP fixo e pô-lo aqui). Vazio = aceitar como antes, para não
    # partir instalações onde o IP da bridge do Docker varia.
    PROXY_IP_CONFIAVEL: list[str] = []

    # COOKIE_SECURE: derivado automaticamente de APP_URL.
    # True se APP_URL usa https:// (domínio ou IP com SSL) — garante cookie httpOnly só enviado em HTTPS.
    # False se APP_URL usa http:// (acesso por IP sem SSL, desenvolvimento local).
    # NÃO configurar manualmente — definir APP_URL correctamente e este valor é calculado.
    @property
    def COOKIE_SECURE(self) -> bool:  # type: ignore[override]
        return self.APP_URL.startswith("https://")

    # --- TOTP (cifra Fernet em repouso) ---
    TOTP_ENCRYPTION_KEY: str = ""  # Auto-gerado no entrypoint se não definido

    # --- Rotação das chaves Fernet ---
    # Durante uma rotação, a chave que sai fica aqui: decifra-se com a atual ou
    # com esta, cifra-se sempre com a atual. Ver app/shared/chaves.py.
    TOTP_ENCRYPTION_KEY_PREV: str = ""
    EVIDENCE_ENCRYPTION_KEY_PREV: str = ""
    PII_ENCRYPTION_KEY_PREV: str = ""

    # --- CORS ---
    # Default derivado de APP_URL: mesmo origen — frontend e backend estao na mesma porta via nginx.
    CORS_ORIGINS: list[str] = []

    @field_validator("PROXY_IP_CONFIAVEL", mode="before")
    @classmethod
    def parse_proxy_ip_confiavel(cls, v):
        """Aceita lista, string separada por vírgulas, ou vazio."""
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        """Aceita JSON array, string separada por vírgulas, ou string vazia (auto-deriva depois)."""
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("["):
                import json
                try:
                    return json.loads(v)
                except json.JSONDecodeError:
                    return []
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    # --- Teto ao corpo dos pedidos (antes de a aplicação o ler) ---
    # Os mesmos valores do `client_max_body_size` do nginx à frente: o teto na
    # aplicação não muda o que o produto aceita, só deixa de depender do proxy
    # para o impor (quem chega pela rede interna e os corpos enviados aos
    # bocados). Ao mudar um lado, mudar o outro. Ver `teto_do_corpo` em main.py.
    CORPO_MAX_MB: int = 15              # geral (nginx: location /api/)
    CORPO_MAX_BACKUP_MB: int = 4096     # upload de um .nbk (nginx: /api/backups)
    CORPO_MAX_PARECER_MB: int = 256     # upload de um parecer (nginx: /api/dossie)
    # Corpos JSON (ver app/shared/guarda_json.py): o parse corre antes da
    # autenticação e multiplica a memória ~28 vezes. Medido: o maior JSON
    # legítimo do frontend tem 410 KiB e ~20 000 `{`/`[`/`,` (aplicar uma
    # importação com 5000 decisões); as rotas /api/auth/ recebem centenas de bytes.
    CORPO_MAX_JSON_KB: int = 1024
    CORPO_MAX_JSON_AUTH_KB: int = 64
    CORPO_MAX_JSON_ESTRUTURA: int = 50_000

    # --- Operações caras em simultâneo (ver app/shared/concorrencia.py) ---
    # Cada vaga argon2 reserva 64 MiB; a vaga pesada (scrypt dos backups, payload
    # da IA, dossiê) até ~512 MiB. Com os valores de omissão, o pior caso (2 × 64
    # + 512) sobre os ~140 MiB do processo em repouso cabe em 1 GiB. Com mais CPUs
    # no backend, subir as vagas argon2 dá mais logins por segundo; com 1 CPU,
    # mais de 2 não dá débito nenhum, só gasta memória.
    CONCORRENCIA_ARGON2: int = 2
    CONCORRENCIA_PESADAS: int = 1
    # Teto da fila de argon2: quem chega com este número já à espera leva 503 na
    # hora, sem prender uma thread do threadpool (que é partilhado por todas as
    # rotas `def`). Uma vaga fica reservada a quem traz um cookie de dispositivo
    # válido, por isso os desconhecidos ocupam no máximo CONCORRENCIA_ARGON2 - 1.
    CONCORRENCIA_ARGON2_FILA: int = 10
    CONCORRENCIA_ARGON2_RESERVADAS: int = 1

    # --- Uploads de evidências ---
    UPLOADS_DIR: str = "uploads"                        # directório raiz (relativo ao cwd)
    MAX_UPLOAD_SIZE_MB: int = 10                        # tamanho máximo por ficheiro
    # Quota total de armazenamento por empresa (0 = ilimitado). No SaaS pode ser
    # sobreposta por-plano via entitlement (limits.total_mb); no saas-trial fixa-se
    # aqui (ex.: 50). Imposta no upload de evidências (413 se excedida).
    EVIDENCE_QUOTA_MB: int = 0
    # Em SaaS, «ilimitado» não existe: com EVIDENCE_QUOTA_MB=0 e o plano ilegível
    # (sidecar em baixo), vale esta quota — nunca a ausência de quota.
    EVIDENCE_QUOTA_MB_SAAS_OMISSAO: int = 50
    ALLOWED_UPLOAD_MIME_TYPES: list[str] = [
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/plain",
        "application/zip",
    ]

    # --- Importação de dados de ferramentas externas (premium) ---
    # Allowlist PRÓPRIA, separada da das evidências: são coisas diferentes. A das
    # evidências é para anexos que ficam guardados; esta é para ficheiros que
    # atravessam o core e vão direitos para o sidecar. Alargar uma não alarga a
    # outra — e é assim que se quer.
    IMPORT_ALLOWED_MIME_TYPES: list[str] = [
        "text/csv",
        "application/csv",
        "text/plain",          # o que muitos browsers declaram para um .csv
        "application/json",
        "text/json",
        "application/vnd.ms-excel",  # o que o Windows declara para .csv com o Excel instalado
        "",                    # alguns clientes não declaram tipo nenhum
    ]
    # Teto de bytes do ficheiro importado. É uma medida de segurança (o processo
    # não pode ficar refém do tamanho de um input de terceiros), não uma alavanca
    # comercial: vale igual em SaaS e on-prem. O sidecar impõe o mesmo limite —
    # o core recusa cedo para nem sequer transportar o que vai ser recusado.
    IMPORT_MAX_SIZE_MB: int = 8

    @field_validator("IMPORT_ALLOWED_MIME_TYPES", mode="before")
    @classmethod
    def parse_import_mime_types(cls, v):
        """Aceita JSON array ou string separada por vírgulas."""
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json
                return json.loads(v)
            return [o.strip() for o in v.split(",")]
        return v

    @field_validator("ALLOWED_UPLOAD_MIME_TYPES", mode="before")
    @classmethod
    def parse_mime_types(cls, v):
        """Aceita JSON array ou string separada por vírgulas."""
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json
                return json.loads(v)
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    # --- Email ---
    # Desativar email completamente (dev local sem SMTP/Resend).
    # Quando False: sem validação de credenciais no arranque, sem envio — o link de reset é apenas logado.
    EMAIL_ENABLED: bool = False
    # Provedor: "smtp" (default, on-prem) ou "resend" (SaaS)
    EMAIL_PROVIDER: str = "smtp"
    # Notificações por email (alertas de prazos de incidentes + resumo semanal).
    # Interruptor mestre, independente do email transacional (reset de password):
    # só há envio quando EMAIL_NOTIFICACOES=true E o email está configurado
    # (EMAIL_ENABLED + provedor com credenciais). Desligar aqui não afeta o reset.
    EMAIL_NOTIFICACOES: bool = True

    # SMTP (usado quando EMAIL_PROVIDER="smtp")
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = Field(default="", repr=False)  # repr=False impede exposição em logs (CWE-532)
    SMTP_FROM_EMAIL: str = "noreply@nis2pme.pt"
    SMTP_FROM_NAME: str = "NIS2PME"
    # Cifra da ligação — as duas formas excluem-se mutuamente:
    #   SMTP_TLS  → liga em claro e sobe para TLS com STARTTLS (porta 587, o comum)
    #   SMTP_SSL  → liga já dentro de TLS, sem fase em claro (porta 465)
    # Se ambas vierem a true, ganha o SSL implícito (ver app/shared/email.py): a
    # biblioteca recusa a combinação, e recusar o envio por causa de um .env
    # editado à mão seria pior do que escolher a mais forte das duas.
    SMTP_TLS: bool = True
    SMTP_SSL: bool = False

    # Resend API (usado quando EMAIL_PROVIDER="resend"). Partilha a MESMA config do funil
    # saas-trial (mesma chave/remetente/URL) — um só serviço de email para toda a plataforma.
    RESEND_API_KEY: str = Field(default="", repr=False, validate_default=True)  # repr=False impede exposição em logs (CWE-532)
    RESEND_FROM: str = "NIS2PME <noreply@nis2pme.pt>"     # remetente verificado no Resend
    RESEND_API_URL: str = "https://api.resend.com/emails"

    @field_validator("RESEND_API_KEY", mode="before")
    @classmethod
    def normalizar_resend_api_key(cls, v: str) -> str:
        """Remove espaços/newlines acidentais na chave (causa comum de 401)."""
        if isinstance(v, str):
            v = v.strip()
        return v

    @field_validator("EMAIL_PROVIDER")
    @classmethod
    def validar_email_provider(cls, v: str) -> str:
        """Garante que EMAIL_PROVIDER é um dos valores suportados."""
        if v not in ("smtp", "resend"):
            raise ValueError("EMAIL_PROVIDER deve ser 'smtp' ou 'resend'")
        return v

    @model_validator(mode="after")
    def validar_configuracao_email(self) -> "Settings":
        """
        Valida que as credenciais necessárias para o provedor de email
        estão configuradas. Falha no arranque com mensagem clara em vez
        de 401/timeout em runtime durante o envio.
        Ignorado quando EMAIL_ENABLED=false (desenvolvimento local sem email).
        """
        if not self.EMAIL_ENABLED:
            return self
        if self.EMAIL_PROVIDER == "resend" and not self.RESEND_API_KEY:
            raise ValueError(
                "EMAIL_PROVIDER=resend mas RESEND_API_KEY está vazia. "
                "Configure RESEND_API_KEY no ficheiro .env."
            )
        if self.EMAIL_PROVIDER == "smtp" and not self.SMTP_HOST:
            raise ValueError(
                "EMAIL_PROVIDER=smtp mas SMTP_HOST está vazio. "
                "Configure SMTP_HOST no ficheiro .env (ou mude EMAIL_PROVIDER=resend)."
            )
        # APP_URL com localhost gera links de email inacessíveis em staging/prod.
        # Só é aceitável em desenvolvimento local (EMAIL_PROVIDER=smtp sem host real).
        if "localhost" in self.APP_URL and self.EMAIL_PROVIDER == "resend":
            raise ValueError(
                f"APP_URL='{self.APP_URL}' contém 'localhost' mas EMAIL_PROVIDER={self.EMAIL_PROVIDER}. "
                "Os links de reset de password enviados por email serão inacessíveis. "
                "Configure APP_URL com o domínio público (ex: https://nis2pme.pt) no .env."
            )
        return self

    @model_validator(mode="after")
    def derivar_cors_e_validar_secrets(self) -> "Settings":
        """
        1. Se CORS_ORIGINS estiver vazio, deriva-o de APP_URL (mesmo-origem via nginx).
        2. Valida que todos os secrets estão presentes — devem ter sido injectados pelo
           entrypoint.sh (auto-gerado em /app/data/auto-secrets.env) ou definidos no .env.
           Falhar aqui é mais útil que erros crípticos em runtime.
        """
        # --- Derivar CORS de APP_URL se não definido ---
        if not self.CORS_ORIGINS:
            self.CORS_ORIGINS = [self.APP_URL.rstrip("/")]

        # --- Validar secrets obrigatórios ---
        # Exigidos nos DOIS modos. Antes só se validavam em on-prem, o que deixava o
        # SaaS arrancar sem chave de cifra — e sem chave a PII era gravada em claro,
        # em silêncio. As chaves são geradas pelo entrypoint.sh, que também completa
        # as que faltem num ficheiro de uma versão anterior, portanto no Docker isto
        # nunca dispara; dispara a correr o backend fora do contentor, que é quando
        # faz falta.
        # A JWT_REFRESH_SECRET_KEY não entra aqui de propósito: não assina nada, e exigi-la
        # fazia uma instalação nova recusar arrancar por falta de um valor que ninguém lê.
        campos_obrigatorios = {
            "JWT_SECRET_KEY": self.JWT_SECRET_KEY,
            "TOTP_ENCRYPTION_KEY": self.TOTP_ENCRYPTION_KEY,
            "EVIDENCE_ENCRYPTION_KEY": self.EVIDENCE_ENCRYPTION_KEY,
            "PII_ENCRYPTION_KEY": self.PII_ENCRYPTION_KEY,
        }
        # O escape de dev da cifra de PII tem de ser honrado aqui também, senão a
        # validação recusava antes de ele chegar a ser consultado e o escape não
        # servia para nada. Continua a ser explícito e a deixar rasto no log.
        if os.getenv("PII_DEV_PLAINTEXT") == "1":
            # Em SaaS não há "dev": a base é partilhada por vários clientes e um
            # escape que grava PII em claro não pode sequer arrancar.
            if self.DEPLOYMENT_MODE == "saas":
                raise ValueError(
                    "PII_DEV_PLAINTEXT=1 não é aceite em DEPLOYMENT_MODE=saas: "
                    "a PII dos tenants ficaria em claro. Remova a variável."
                )
            campos_obrigatorios.pop("PII_ENCRYPTION_KEY")
        # Os outros escapes de desenvolvimento do premium, pela mesma razão: em
        # SaaS, o canal para o sidecar em claro ou as evidências para a IA sem
        # selo expunham dados de vários clientes. Cada guarda olha para o valor
        # tal como quem o usa o lê: o do envelope é um campo booleano (o pydantic
        # aceita `1`, `yes`, `on`…), e comparar o texto com «true» deixava-o
        # passar; o do mTLS é lido do ambiente, só com «1» (premium/client.py).
        if self.DEPLOYMENT_MODE == "saas":
            escapes = (
                ("PREMIUM_DEV_SEM_MTLS", (os.getenv("PREMIUM_DEV_SEM_MTLS") or "").strip() == "1"),
                ("PREMIUM_ENVELOPE_DEV_PLAINTEXT", self.PREMIUM_ENVELOPE_DEV_PLAINTEXT),
            )
            for var, ligado in escapes:
                if ligado:
                    raise ValueError(f"{var} não é aceite em DEPLOYMENT_MODE=saas. Remova a variável.")
        em_falta = [k for k, v in campos_obrigatorios.items() if not v]
        if em_falta:
            raise ValueError(
                f"Secrets em falta: {', '.join(em_falta)}. "
                "No Docker, o entrypoint.sh gera estes valores automaticamente. "
                "Se estás a executar o backend fora do Docker, copia os valores de "
                "/app/data/auto-secrets.env para o teu .env."
            )
        return self

    @field_validator("DEPLOYMENT_MODE")
    @classmethod
    def validar_deployment_mode(cls, v: str) -> str:
        """Garante que DEPLOYMENT_MODE é um dos valores aceites."""
        if v not in ("saas", "onprem"):
            raise ValueError(
                "DEPLOYMENT_MODE deve ser 'saas' ou 'onprem'"
            )
        return v

    # --- Cifra de ficheiros de evidência (Fernet) ---
    EVIDENCE_ENCRYPTION_KEY: str = ""  # Auto-gerado no entrypoint se não definido

    # --- Cifra de campos PII em repouso (Fernet) ---
    PII_ENCRYPTION_KEY: str = ""  # Auto-gerado no entrypoint se não definido

    @field_validator("JWT_SECRET_KEY")
    @classmethod
    def validar_chaves_jwt(cls, v: str, info) -> str:
        """
        Impede uso de chaves óbvias ou demasiado curtas. Vazio é aceite (auto-gerado no
        entrypoint). A JWT_REFRESH_SECRET_KEY não é validada: não assina nada, e impor-lhe
        um formato sugeria que o valor tinha importância.
        """
        if v and (v.startswith("CHANGE_ME") or len(v) < 32):
            raise ValueError(
                f"{info.field_name} deve ter pelo menos 32 caracteres "
                "e não pode ser o valor padrão."
            )
        return v

    @field_validator("TOTP_ENCRYPTION_KEY")
    @classmethod
    def validar_totp_encryption_key(cls, v: str) -> str:
        """Valida que TOTP_ENCRYPTION_KEY é uma chave Fernet válida. Vazio é aceite (auto-gerado)."""
        return _validar_chave_fernet(v, "TOTP_ENCRYPTION_KEY")

    @field_validator("EVIDENCE_ENCRYPTION_KEY")
    @classmethod
    def validar_evidence_encryption_key(cls, v: str) -> str:
        """Valida formato da chave Fernet de cifra de evidências (se definida)."""
        return _validar_chave_fernet(v, "EVIDENCE_ENCRYPTION_KEY")

    @field_validator("PII_ENCRYPTION_KEY")
    @classmethod
    def validar_pii_encryption_key(cls, v: str) -> str:
        """Valida formato da chave Fernet de cifra de campos PII (se definida)."""
        return _validar_chave_fernet(v, "PII_ENCRYPTION_KEY")

    @field_validator(
        "TOTP_ENCRYPTION_KEY_PREV", "EVIDENCE_ENCRYPTION_KEY_PREV", "PII_ENCRYPTION_KEY_PREV"
    )
    @classmethod
    def validar_chave_anterior(cls, v: str, info) -> str:
        """A chave anterior de uma rotação tem a forma de qualquer outra chave Fernet."""
        return _validar_chave_fernet(v, info.field_name)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        # Um erro de validação não ecoa o input: truncado, mostrava a cauda do
        # DATABASE_URL (com o fim da password) no log de um arranque falhado.
        hide_input_in_errors=True,
    )


@lru_cache
def get_settings() -> Settings:
    """
    Retorna singleton das settings (cached).
    Usar como dependência FastAPI: Depends(get_settings).
    """
    return Settings()
