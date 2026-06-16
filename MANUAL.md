# NIS2PME — Installation & Operation Manual

**[English](MANUAL.md)** · [Português 🇵🇹](MANUAL.pt-PT.md)

> Edition: On-Prem (GHCR images)
> Date: 2026-06-12

---

## Table of contents

1. [Prerequisites](#1-prerequisites)
2. [Installation](#2-installation)
3. [Installing Docker (if needed)](#3-installing-docker-if-needed)
4. [First access — Setup wizard](#4-first-access--setup-wizard)
5. [Advanced configuration](#5-advanced-configuration)
6. [Maintenance and updates](#6-maintenance-and-updates)
7. [Useful commands and troubleshooting](#7-useful-commands-and-troubleshooting)

---

## 1. Prerequisites

### Minimum hardware
| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 1 vCPU | 2 vCPU |
| RAM | 1 GB | 2 GB |
| Disk | 20 GB | 50 GB |

### Supported operating system
- Linux (any distribution with kernel ≥ 4.0): Ubuntu 20.04+, Debian 11+, RHEL 8+, Rocky Linux 8+, Fedora 37+, openSUSE Leap 15+, Arch Linux, Alpine 3.16+
- Not supported as a host: Windows / macOS (use a Linux VM or Docker Desktop with WSL2)

### Software
- **Docker Engine 20.10+** with **Docker Compose v2**
- Internet access to pull the images from the **GitHub Container Registry (GHCR)** (`ghcr.io`)

### Required ports
- **80/TCP** (HTTP) — mandatory
- **443/TCP** (HTTPS) — required by default (TLS active from first boot); only optional in proxy mode

---

## 2. Installation

The images are **pre-built** and published on GHCR — nothing needs to be compiled.

### Option A — One-line install (simplest)

The script detects the server IP, generates a secure database password, creates the `.env`, pulls the images from GHCR and starts everything:

```bash
curl -fsSL https://raw.githubusercontent.com/nis2pme/platform/main/start_nis2pme.sh | bash
```

> 🔎 **Security best practice:** piping a script straight into `bash` runs remote code. To inspect it first:
> ```bash
> curl -fsSL https://raw.githubusercontent.com/nis2pme/platform/main/start_nis2pme.sh -o start_nis2pme.sh
> less start_nis2pme.sh
> sh start_nis2pme.sh
> ```

By default the script installs into a `./nis2pme` folder. To choose another:

```bash
NIS2PME_DIR=/opt/nis2pme sh start_nis2pme.sh
```

### Option B — Manual Docker Compose

```bash
# 1. Get the compose file
curl -fsSL https://raw.githubusercontent.com/nis2pme/platform/main/docker-compose.yml -o docker-compose.yml

# 2. Create a minimal .env
cat > .env <<'EOF'
APP_URL=http://YOUR_SERVER_IP
DB_PASSWORD=a-long-random-string
EOF

# 3. Pull the images and start
docker compose pull
docker compose up -d
```

### Check status

```bash
docker compose ps
docker compose logs -f
```

> The backend runs the database migrations at startup, so the first boot can take up to 1–2 minutes.

---

## 3. Installing Docker (if needed)

If Docker is not installed, `start_nis2pme.sh` detects it and shows the instructions for your distribution. Summary:

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

### Other distributions
See the official guide: <https://docs.docker.com/engine/install/>

After installing Docker, go back to [Section 2](#2-installation).

---

## 4. First access — Setup wizard

Once the system is up, open the browser at the address shown (e.g. `http://192.168.1.50`).

The **setup wizard** guides you through 5 steps:

### Step 1 — Company details
- Name, sector of activity, size (micro/small/medium)
- The system automatically determines the required compliance level (important or essential entity)

### Step 2 — Administrator account
- Name, email and password of the main administrator user

### Step 3 — Email (SMTP) — optional
- Outbound email settings used for password reset and notifications
- Can be skipped and configured later; without it, password reset cannot send email

### Step 4 — Connection security (HTTPS)

The connection is **always encrypted by default**. The TLS mode is chosen right in the installer (`start_nis2pme.sh`):

| Installer option | When to use | Result |
|---|---|---|
| **I already have a certificate** | You have a certificate + key (domain with Let's Encrypt, corporate CA…) | Encrypted and **trusted**, no browser warning — recommended for production |
| **Behind a proxy/firewall** | Cloudflare/Traefik/Nginx already handle HTTPS | The app serves HTTP internally; TLS is terminated upstream |
| **Generate a temporary certificate** *(default)* | You have no certificate | Self-signed: encrypted, but the browser warns on the first visit |

> **Browser warning (temporary certificate):** this is expected. Click **Advanced → Proceed**. The self-signed certificate protects against passive eavesdropping, but **not** against an active attacker on the network — for full protection, use a trusted certificate.

In the **setup wizard**, the "Connection Security" step shows the current state and lets you **keep** what is in place or **upload/replace** it with a trusted certificate (`.crt`/`.pem` + `.key`/`.pem` key **without a password**). If a trusted certificate is already active, just continue.

> **Behind a proxy:** make sure the proxy↔server hop is local (same host/Docker network); otherwise that hop travels in clear text.

### Step 5 — Consents
- Acceptance of the Terms & Conditions and the Privacy Policy (mandatory under GDPR)
- Optional opt-in to the update check
- The QNRCS 2026 framework is loaded automatically — it is bundled in the image

After completing the wizard, you enrol the **mandatory two-factor authentication (TOTP)** and are then redirected to the **maturity dashboard**.

---

## 5. Advanced configuration

### The .env file

Created automatically by `start_nis2pme.sh`. It contains only:

```env
APP_URL=http://192.168.1.50       # Auto-detected URL
DB_PASSWORD=a3f8c2...             # Randomly generated password
```

**All other variables** (JWT secrets, Fernet keys, etc.) are **auto-generated** by the backend on first boot and stored in the Docker volume `nis2pme_data`. You do not need to set them.

### Available optional variables

Add to `.env` if needed:

```env
# Use a domain instead of an IP
APP_URL=https://nis2pme.company.pt

# Ports (80 and 443 by default)
PORT=8080
HTTPS_PORT=8443

# Email (for password reset)
# Without these variables, password reset does not send email
EMAIL_ENABLED=true
EMAIL_PROVIDER=smtp
SMTP_HOST=mail.company.pt
SMTP_PORT=587
SMTP_USER=noreply@company.pt
SMTP_PASSWORD=email_password
SMTP_FROM_EMAIL=noreply@company.pt
SMTP_FROM_NAME=NIS2PME
SMTP_TLS=true
```

### Pinning an image version

By default the compose file uses the `:latest` tag. To pin a specific version, edit `docker-compose.yml` and replace, for example, `ghcr.io/nis2pme/backend:latest` with `ghcr.io/nis2pme/backend:0.2`.

### Docker volumes (data persistence)

| Volume | Contents | Impact if lost |
|--------|----------|----------------|
| `nis2pme_pgdata` | Full database | **Total** — loss of all data |
| `nis2pme_uploads` | Evidence files | Loss of uploaded files |
| `nis2pme_data` | Auto-generated secrets (JWT, Fernet) | All tokens invalidated; encrypted data becomes unrecoverable |
| `nis2pme_nginx` | Dynamic HTTPS configuration | Nginx reverts to HTTP by default |

> **Back up `nis2pme_pgdata`, `nis2pme_uploads` and `nis2pme_data` regularly.**

---

## 6. Maintenance and updates

### Stop the system

```bash
docker compose down
```

### Stop and delete everything (CAUTION — deletes data)

```bash
# Deletes containers and volumes — IRREVERSIBLE
docker compose down -v
```

### Restart after changing .env

```bash
docker compose down
docker compose up -d
```

### Update to a new version

```bash
# Pull the latest images from GHCR and restart
docker compose pull
docker compose up -d
```

Or simply run the installer again, which does the same (keeps the existing `.env`):

```bash
sh start_nis2pme.sh
```

> Database migrations are applied automatically at startup (`alembic upgrade head`).

### Manual database backup

```bash
# Create a backup
docker exec nis2pme_db pg_dump -U nis2pme nis2pme > backup_$(date +%Y%m%d).sql

# Restore (stop the system first)
docker compose down
docker compose up -d db
docker exec -i nis2pme_db psql -U nis2pme nis2pme < backup_20260612.sql
docker compose up -d
```

### Volume backup (evidence + secrets)

```bash
# Evidence
docker run --rm -v nis2pme_uploads:/data -v $(pwd):/backup alpine \
    tar czf /backup/uploads_backup_$(date +%Y%m%d).tar.gz -C /data .

# Secrets (critical — without this backup you cannot restore encrypted data)
docker run --rm -v nis2pme_data:/data -v $(pwd):/backup alpine \
    tar czf /backup/secrets_backup_$(date +%Y%m%d).tar.gz -C /data .
```

---

## 7. Useful commands and troubleshooting

### Service status

```bash
docker compose ps
```

### View live logs

```bash
docker compose logs -f              # all services
docker compose logs -f backend      # backend only
docker compose logs -f frontend     # nginx/frontend only
docker compose logs -f db           # database only
```

### Open a shell in the backend container

```bash
docker exec -it nis2pme_backend sh
```

### Check API health

```bash
curl http://localhost/api/health
```

### Common problems

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `DB_PASSWORD not set` | `.env` missing or variable absent | Run `sh start_nis2pme.sh` again |
| Image pull error (`pull access denied` / `manifest unknown`) | Image not published or wrong tag | Confirm `ghcr.io/nis2pme/backend:latest` and internet access |
| Backend won't start | DB not ready | Wait 30s; check `docker compose logs db` |
| "502 Bad Gateway" | Backend still starting | Wait 60s; backend runs migrations at startup |
| Browser certificate warning | Self-signed certificate | Normal — accept the security exception in the browser |
| Cannot connect on the IP | Firewall | `sudo ufw allow 80/tcp && sudo ufw allow 443/tcp` |
| `Permission denied` on the script | Missing `chmod +x` | `chmod +x start_nis2pme.sh` (or run with `sh start_nis2pme.sh`) |

### Reset administrator credentials (lost password or 2FA)

If the administrator can no longer sign in — forgotten password, lost 2FA device — and there is **no SMTP/email configured** for a self-service reset, recover access directly on the server:

```bash
docker exec -it nis2pme_backend python scripts/reset_admin.py
```

Run it **on the machine where NIS2PME is installed** — no login is required (it works directly against the database). The interactive script (available in **Portuguese or English**) lets you:

- **Reset the password**, **disable 2FA (MFA)**, or **both**.
- It **lists the administrator and sub-administrator accounts**, lets you choose which one, and asks you to **confirm by typing that user's email** before changing anything.
- A new password must meet the platform's standard rules: **at least 8 characters**, with upper- and lower-case letters, a digit and a special character.
- Resetting the password automatically **revokes all active sessions**.

> The `-it` flags are required (interactive prompts). After finishing, sign in with the new password and re-enable 2FA in the account settings if you disabled it.

---

> To build the images from source (instead of pulling them from GHCR), see **CONTRIBUTING.md** and use `docker compose -f docker-compose.build.yml up -d --build`.
