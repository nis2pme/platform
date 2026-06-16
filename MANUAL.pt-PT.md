# NIS2PME — Manual de Instalação e Operação

[English](MANUAL.md) · **[Português 🇵🇹](MANUAL.pt-PT.md)**

> Versão: On-Prem (imagens GHCR)
> Data: 2026-06-12

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
| CPU | 1 vCPU | 2 vCPU |
| RAM | 1 GB | 2 GB |
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

### Opção B — Docker Compose manual

```bash
# 1. Obter o ficheiro compose
curl -fsSL https://raw.githubusercontent.com/nis2pme/platform/main/docker-compose.yml -o docker-compose.yml

# 2. Criar um .env mínimo
cat > .env <<'EOF'
APP_URL=http://IP_DO_TEU_SERVIDOR
DB_PASSWORD=uma-string-longa-e-aleatoria
EOF

# 3. Puxar as imagens e arrancar
docker compose pull
docker compose up -d
```

### Verificar estado

```bash
docker compose ps
docker compose logs -f
```

> O backend faz as migrações da base de dados no arranque, pelo que a primeira inicialização pode demorar até 1–2 minutos.

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

Após o sistema arrancar, abre o browser no endereço indicado (ex: `http://192.168.1.50`).

O **assistente de configuração** guia-te em 5 passos:

### Passo 1 — Dados da empresa
- Nome, sector de actividade, dimensão (micro/pequena/média)
- O sistema determina automaticamente o nível de conformidade exigido (entidade importante ou essencial)

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

Criado automaticamente pelo `start_nis2pme.sh`. Contém apenas:

```env
APP_URL=http://192.168.1.50       # URL detectado automaticamente
DB_PASSWORD=a3f8c2...             # Password gerada aleatoriamente
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
```

### Fixar uma versão das imagens

Por defeito o compose usa a tag `:latest`. Para fixar uma versão específica, edita o `docker-compose.yml` e substitui, por exemplo, `ghcr.io/nis2pme/backend:latest` por `ghcr.io/nis2pme/backend:0.2`.

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

```bash
# Puxar as imagens mais recentes do GHCR e reiniciar
docker compose pull
docker compose up -d
```

Ou simplesmente voltar a correr o instalador, que faz o mesmo (mantém o `.env` existente):

```bash
sh start_nis2pme.sh
```

> As migrações de base de dados são aplicadas automaticamente no arranque (`alembic upgrade head`).

### Backup manual da base de dados

```bash
# Criar backup
docker exec nis2pme_db pg_dump -U nis2pme nis2pme > backup_$(date +%Y%m%d).sql

# Restaurar (parar o sistema primeiro)
docker compose down
docker compose up -d db
docker exec -i nis2pme_db psql -U nis2pme nis2pme < backup_20260612.sql
docker compose up -d
```

### Backup dos volumes (evidências + secrets)

```bash
# Evidências
docker run --rm -v nis2pme_uploads:/data -v $(pwd):/backup alpine \
    tar czf /backup/uploads_backup_$(date +%Y%m%d).tar.gz -C /data .

# Secrets (crítico — sem este backup não consegues restaurar cifras)
docker run --rm -v nis2pme_data:/data -v $(pwd):/backup alpine \
    tar czf /backup/secrets_backup_$(date +%Y%m%d).tar.gz -C /data .
```

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
- A nova password segue as regras padrão da plataforma: **pelo menos 8 caracteres**, com maiúsculas, minúsculas, um dígito e um caráter especial.
- Redefinir a password **revoga automaticamente todas as sessões ativas**.

> As flags `-it` são obrigatórias (perguntas interativas). No fim, inicie sessão com a nova password e reative o 2FA nas definições de conta, se o tiver desativado.

---

> Para construir as imagens a partir do código-fonte (em vez de as puxar do GHCR), consulta o **CONTRIBUTING.md** e usa `docker compose -f docker-compose.build.yml up -d --build`.
