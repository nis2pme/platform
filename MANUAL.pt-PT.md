# NIS2PME — Manual de Instalação e Operação

[English](MANUAL.md) · **[Português 🇵🇹](MANUAL.pt-PT.md)**

> Versão: On-Prem (imagens GHCR)
> Data: 2026-10-01

---

## Índice

1. [Pré-requisitos](#1-pré-requisitos)
2. [Instalação](#2-instalação)
3. [Instalar o Docker (se necessário)](#3-instalar-o-docker-se-necessário)
4. [Primeiro acesso — Assistente de configuração](#4-primeiro-acesso--assistente-de-configuração)
5. [Configuração avançada](#5-configuração-avançada)
6. [Manutenção e updates](#6-manutenção-e-updates)
7. [Comandos úteis e troubleshooting](#7-comandos-úteis-e-troubleshooting)

---

## 1. Pré-requisitos

### Hardware mínimo
| Recurso | Mínimo | Recomendado |
|---------|--------|-------------|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 2 GB | 4 GB |
| Disco | 20 GB | 50 GB |

### Sistema operativo suportado
- Linux (qualquer distribuição com kernel ≥ 4.0): Ubuntu 20.04+, Debian 11+, RHEL 8+, Rocky Linux 8+, Fedora 37+, openSUSE Leap 15+, Arch Linux, Alpine 3.16+
- Não suportado como host: Windows / macOS (use uma VM Linux ou Docker Desktop com WSL2)

### Software
- **Docker Engine 20.10+** com **Docker Compose v2**
- Acesso à internet para descarregar as imagens do **GitHub Container Registry (GHCR)** (`ghcr.io`)

### Portas necessárias
- **80/TCP** (HTTP) — obrigatória
- **443/TCP** (HTTPS) — necessária por omissão (TLS ativo desde o arranque); só dispensável no modo proxy

---

## 2. Instalação

As imagens são **pré-construídas** e publicadas no GHCR — não é preciso compilar nada.

### Opção A — Instalação numa linha (mais simples)

O script deteta o IP do servidor, gera uma password segura para a base de dados, cria o `.env`, puxa as imagens do GHCR e arranca tudo:

```bash
curl -fsSL https://raw.githubusercontent.com/nis2pme/platform/main/start_nis2pme.sh | bash
```

> 🔎 **Boa prática de segurança:** enviar um script diretamente para o `bash` executa código remoto. Para o inspecionar primeiro:
> ```bash
> curl -fsSL https://raw.githubusercontent.com/nis2pme/platform/main/start_nis2pme.sh -o start_nis2pme.sh
> less start_nis2pme.sh
> sh start_nis2pme.sh
> ```

Por defeito, o script instala numa pasta `./nis2pme`. Para escolher outra:

```bash
NIS2PME_DIR=/opt/nis2pme sh start_nis2pme.sh
```

> **O one-liner corre sem perguntas.** Ao ser enviado para o `bash` não tem terminal, por isso usa defaults: o menu de idioma é saltado (**Português**) e é gerado um **certificado temporário self-signed**. Para escolheres o idioma e o modo TLS (ver [Passo 4](#passo-4--segurança-da-ligação-https)), descarrega e corre antes (`sh start_nis2pme.sh`). O certificado também pode ser definido ou substituído no assistente de primeira configuração.

### Opção B — Docker Compose manual

```bash
# 1. Obter o ficheiro compose
curl -fsSL https://raw.githubusercontent.com/nis2pme/platform/main/docker-compose.yml -o docker-compose.yml

# 2. Criar um .env mínimo
cat > .env <<'EOF'
APP_URL=https://IP_DO_TEU_SERVIDOR
DB_PASSWORD=uma-string-longa-e-aleatoria
TLS_MODE=self-signed
EOF

# 3. Puxar as imagens e arrancar
docker compose pull
docker compose up -d
```

> O `TLS_MODE` pode ser `self-signed` (default — gera um certificado temporário), `custom` (o teu certificado + chave — ver [`.env.example`](.env.example)), ou `proxy` (TLS terminado a montante; nesse caso usa `APP_URL=http://…`). A pasta `./certs` é criada automaticamente.

### Verificar estado

```bash
docker compose ps
docker compose logs -f
```

> O backend faz as migrações da base de dados no arranque, pelo que a primeira inicialização pode demorar alguns minutos.

---

## 3. Instalar o Docker (se necessário)

Se o Docker não estiver instalado, o `start_nis2pme.sh` deteta-o e mostra as instruções para a tua distribuição. Resumo:

### Ubuntu / Debian
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

### RHEL / CentOS / Rocky / AlmaLinux / Fedora
```bash
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker
```

### Outras distribuições
Consulta o guia oficial: <https://docs.docker.com/engine/install/>

Depois de instalar o Docker, volta à [Secção 2](#2-instalação).

---

## 4. Primeiro acesso — Assistente de configuração

Após o sistema arrancar, abre o browser no endereço indicado (ex: `https://192.168.1.50`).

O **assistente de configuração** guia-te em 5 passos:

### Passo 1 — Dados da empresa
- Nome, sector de actividade, dimensão (micro/pequena/média/grande), classificação NIS2 (importante/essencial) e nível de conformidade QNRCS (Básico/Substancial/Elevado) — és tu que os escolhes
- O assistente pré-preenche a classificação a partir do sector e o nível a partir da classificação; ambos podem ser ajustados

### Passo 2 — Conta de administrador
- Nome, email e password do utilizador administrador principal

### Passo 3 — Email (SMTP) — opcional
- Definições de envio de email usadas para reposição de password e notificações
- Pode ser ignorado e configurado mais tarde; sem isto, a reposição de password não envia email

### Passo 4 — Segurança da ligação (HTTPS)

A ligação é **sempre cifrada por omissão**. O modo TLS é escolhido logo no instalador (`start_nis2pme.sh`):

| Opção no instalador | Quando usar | Resultado |
|---|---|---|
| **Já tenho um certificado** | Tens certificado + chave (domínio com Let's Encrypt, CA empresarial…) | Cifrado e **de confiança**, sem aviso no browser — recomendado em produção |
| **Atrás de proxy/firewall** | Cloudflare/Traefik/Nginx já tratam o HTTPS | A app serve HTTP interno; o TLS é terminado a montante |
| **Gerar certificado temporário** *(predefinição)* | Não tens certificado | Autoassinado: ligação cifrada, mas o browser avisa na 1.ª visita |

> **Aviso do browser (certificado temporário):** é esperado. Clica em **Avançado → Prosseguir**. O autoassinado protege contra escuta passiva, mas **não** contra um atacante ativo na rede — para proteção completa, usa um certificado de confiança.

No **assistente**, o passo "Segurança da Ligação" mostra o estado atual e deixa-te **manter** o que está ou **carregar/substituir** por um certificado de confiança (`.crt`/`.pem` + chave `.key`/`.pem` **sem password**). Com um certificado de confiança já ativo, basta avançar.

> **Atrás de proxy:** garante que o salto proxy↔servidor é local (mesma máquina/rede Docker); caso contrário esse troço viaja em claro.

### Passo 5 — Consentimentos
- Aceitação dos Termos e Condições e da Política de Privacidade (obrigatório por RGPD)
- Adesão opcional à verificação de atualizações
- O framework QNRCS 2026 é carregado automaticamente — vem incorporado na imagem

Após completar o assistente, enrolas a **autenticação de dois fatores (TOTP), obrigatória**, e és depois redirecionado para o **dashboard de maturidade**.

---

## 5. Configuração avançada

### Ficheiro .env

Criado automaticamente pelo `start_nis2pme.sh`. Contém:

```env
APP_URL=https://192.168.1.50      # URL detectado automaticamente
DB_PASSWORD=a3f8c2...             # Password gerada aleatoriamente
TLS_MODE=self-signed              # Modo de segurança da ligação (self-signed | custom | proxy)
```

**Todas as outras variáveis** (secrets JWT, chaves Fernet, etc.) são **auto-geradas** pelo backend no primeiro arranque e guardadas no volume Docker `nis2pme_data`. Não precisas de as definir.

### Variáveis opcionais disponíveis

Adicionar ao `.env` se necessário:

```env
# Usar um domínio em vez de IP
APP_URL=https://nis2pme.empresa.pt

# Portos (por defeito 80 e 443)
PORT=8080
HTTPS_PORT=8443

# Email (para reset de password)
# Sem estas variáveis, o reset de password não envia email
EMAIL_ENABLED=true
EMAIL_PROVIDER=smtp
SMTP_HOST=mail.empresa.pt
SMTP_PORT=587
SMTP_USER=noreply@empresa.pt
SMTP_PASSWORD=password_do_email
SMTP_FROM_EMAIL=noreply@empresa.pt
SMTP_FROM_NAME=NIS2PME
SMTP_TLS=true
# SMTP_SSL=true                     # TLS implícito (porta 465); exclui-se com SMTP_TLS
```

> Outras variáveis opcionais (notificações por email, retenção da trilha, interface de escuta e IP de confiança do proxy) estão documentadas e comentadas no [`.env.example`](.env.example).

### Fixar uma versão das imagens

Por defeito o compose usa a tag `:latest`. Para fixar uma versão específica, edita o `docker-compose.yml` e substitui, por exemplo, `ghcr.io/nis2pme/backend:latest` por `ghcr.io/nis2pme/backend:0.4.0`. O instalador também aceita `NIS2PME_VERSION=v0.4.0`, que fixa o compose publicado e o confere contra o checksum publicado.

### Volumes Docker (persistência de dados)

| Volume | Conteúdo | Impacto se perdido |
|--------|----------|--------------------|
| `nis2pme_pgdata` | Base de dados completa | **Total** — perda de todos os dados |
| `nis2pme_uploads` | Ficheiros de evidências | Perda dos ficheiros carregados |
| `nis2pme_data` | Secrets auto-gerados (JWT, Fernet) | Todos os tokens invalidados; cifras perdem-se |
| `nis2pme_nginx` | Configuração HTTPS dinâmica | Nginx reverte para HTTP por defeito |

> **Fazer backup regularmente de `nis2pme_pgdata`, `nis2pme_uploads` e `nis2pme_data`.**

---

## 6. Manutenção e updates

### Parar o sistema

```bash
docker compose down
```

### Parar e apagar tudo (CUIDADO — apaga dados)

```bash
# Apaga containers e volumes — IRREVERSÍVEL
docker compose down -v
```

### Reiniciar após alteração ao .env

```bash
docker compose down
docker compose up -d
```

### Actualizar para nova versão

O caminho recomendado é voltar a correr o instalador: mantém o `.env`, atualiza o `docker-compose.yml`, faz um backup antes de mexer nas imagens e espera que a aplicação fique operacional.

```bash
curl -fsSL https://raw.githubusercontent.com/nis2pme/platform/main/start_nis2pme.sh | sudo sh
```

(ou `sh start_nis2pme.sh`, se já tiver o instalador descarregado). Se a pasta de instalação foi criada com `sudo`, corra-o também com `sudo`.

> `docker compose pull && docker compose up -d` também puxa as imagens, mas não atualiza o `docker-compose.yml` nem faz o backup de antes: só serve quando a versão nova não muda o compose.

> As migrações de base de dados são aplicadas automaticamente no arranque (`alembic upgrade head`).

#### Atualizar pelo interface (opcional)

Num servidor Linux com systemd, o administrador pode atualizar a partir de **Definições → Atualizações**, sem terminal. O instalador instala o agente por omissão quando corre como root (`sudo`) num servidor com systemd; para o instalar numa instalação que já existe, ou se o instalador correu sem `sudo`, corra-o uma vez no servidor:

```bash
curl -fsSL https://raw.githubusercontent.com/nis2pme/platform/main/start_nis2pme.sh | sudo sh -s -- --agente
```

Instala um agente (unidade systemd, sem porta de rede, e sem dar a nenhum contentor acesso ao Docker) que só aplica versões **assinadas pela NIS2PME**: confere a assinatura e o resumo de cada ficheiro antes de executar o que quer que seja. Quando há uma versão nova, o botão «Atualizar agora» pede a sua password, faz o backup, atualiza e mostra o progresso; se a versão nova não arrancar e a base de dados não tiver mudado, a anterior é reposta sozinha. A aplicação fica indisponível 1 a 3 minutos. Para retirar o agente: `sudo /usr/local/lib/nis2pme/nis2pme-agente.sh --desinstalar`. Sem o agente, o ecrã mostra o comando acima. Para não o instalar, use `--sem-agente`.

### Backups embutidos (recomendado)

A app tem um sistema de backups próprio em **Definições → Sistema**: define uma
frase-secreta (obrigatória — sem ela não há backups) e a partir daí é criado um
backup **diário automático** (03:00, configurável); também podes criar um manual
a qualquer momento, em modo completo ou só base de dados. Cada ficheiro `.nbk` é
**cifrado** e, em modo completo, contém tudo: base de dados, evidências, dados
premium (se existirem), secrets e `.env`.

> **A frase-secreta e uma cópia dos backups devem viver FORA deste servidor**
> (ex.: cofre de palavras-passe + descarregar o `.nbk` para outra máquina).
> Sem a frase-secreta, o backup é irrecuperável.

### Restaurar um backup

```bash
# 1. Inspecionar o conteúdo (não altera nada; pede a frase-secreta)
sh restaurar_backup.sh nis2pme-backup-20260715-030000.nbk

# 2. Executar mesmo (cria backup de segurança, pára a API, restaura e reinicia)
sh restaurar_backup.sh nis2pme-backup-20260715-030000.nbk --confirmo
```

O primeiro argumento pode ser um backup guardado no servidor ou um ficheiro
`.nbk` local (ex.: descarregado da UI). O restauro é protegido: cifra
autenticada, verificação de versões/migrações (um backup de uma versão mais
recente da app é recusado), backup de segurança automático do estado atual,
modo manutenção, e no fim o backend reinicia e aplica as migrações sozinho.
O relatório final fica em `data/restauro-relatorio.json` (dentro do volume) e
no registo de auditoria.

Flags úteis: `--sem-premium` (ignorar os dados premium do backup),
`--maquina-nova` (primeira reposição num servidor novo — aplica também o `.env`
do backup, preservando as passwords de base de dados geradas nesta máquina).

> Num servidor novo: instala normalmente com o `start_nis2pme.sh` (cria o `.env`;
> a app gera os secrets no primeiro arranque), copia o `.nbk` para a pasta do
> compose e corre o restauro com `--maquina-nova`.
> Se a licença premium usar ativação anti-clone, pode ser preciso reativá-la.

---

## 7. Comandos úteis e troubleshooting

### Estado dos serviços

```bash
docker compose ps
```

### Ver logs em tempo real

```bash
docker compose logs -f              # todos os serviços
docker compose logs -f backend      # só o backend
docker compose logs -f frontend     # só o nginx/frontend
docker compose logs -f db           # só a base de dados
```

### Entrar no container do backend

```bash
docker exec -it nis2pme_backend sh
```

### Verificar saúde da API

```bash
curl http://localhost/api/health
```

### Problemas comuns

| Sintoma | Causa provável | Solução |
|---------|---------------|---------|
| `DB_PASSWORD not set` | `.env` não existe ou variável em falta | Correr `sh start_nis2pme.sh` de novo |
| Erro a puxar a imagem (`pull access denied` / `manifest unknown`) | Imagem não publicada ou tag errada | Confirmar `ghcr.io/nis2pme/backend:latest` e ligação à internet |
| Backend não arranca | DB não está pronta | Aguardar 30s; ver `docker compose logs db` |
| "502 Bad Gateway" | Backend a arrancar | Aguardar 60s; backend faz migrações no arranque |
| Browser alerta certificado | Certificado auto-assinado | Normal — aceitar excepção de segurança no browser |
| Não consegue ligar no IP | Firewall | `sudo ufw allow 80/tcp && sudo ufw allow 443/tcp` |
| `Permission denied` no script | Falta `chmod +x` | `chmod +x start_nis2pme.sh` (ou correr com `sh start_nis2pme.sh`) |

### Reset de credenciais do administrador (password ou 2FA perdidos)

Se o administrador já não consegue entrar — password esquecida, dispositivo de 2FA perdido — e **não há SMTP/email configurado** para um reset self-service, recupere o acesso diretamente no servidor:

```bash
docker exec -it nis2pme_backend python scripts/reset_admin.py
```

Corra-o **na máquina onde o NIS2PME está instalado** — não é preciso login (atua diretamente sobre a base de dados). O script interativo (disponível em **português ou inglês**) permite:

- **Redefinir a password**, **desativar o 2FA (MFA)**, ou **ambos**.
- **Lista as contas de administrador e subadministrador**, deixa escolher qual, e pede para **confirmar escrevendo o email desse utilizador** antes de alterar seja o que for.
- A nova password segue as regras padrão da plataforma: **pelo menos 12 caracteres**, com uma letra maiúscula, um dígito e um caráter especial.
- Redefinir a password **revoga automaticamente todas as sessões ativas**.

> As flags `-it` são obrigatórias (perguntas interativas). No fim, inicie sessão com a nova password e reative o 2FA nas definições de conta, se o tiver desativado.

---

> Para construir as imagens a partir do código-fonte (em vez de as puxar do GHCR), consulta o **CONTRIBUTING.md** e usa `docker compose -f docker-compose.build.yml up -d --build`.
