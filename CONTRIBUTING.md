# Contributing to NIS2PME

Thanks for considering a contribution! NIS2PME is an open-source project and contributions —
bug reports, fixes, features, documentation, translations — are welcome.

> 🇵🇹 Este guia está em inglês para alcançar mais contribuidores, mas és bem-vindo a abrir
> issues e PRs em português.

---

## Before you start

- All contributors must agree to the **[Contributor License Agreement (CLA)](CLA.md)**. By
  opening a pull request you confirm you have read and accept it.
- By contributing, you agree your work is licensed under the project's **[AGPLv3](LICENSE)**.

## Reporting bugs & requesting features

Please use **[GitHub Issues](https://github.com/nis2pme/platform/issues)**. For bugs, include:

- What you expected vs. what happened
- Steps to reproduce
- Your environment (OS, Docker version, NIS2PME image tag)
- Relevant logs: `docker compose logs backend` (redact any secrets)

> 🔐 **Do not report security vulnerabilities in public issues.** Email `contact@nis2pme.pt`
> instead.

## Submitting changes

1. Fork the repository and create a branch from `main`.
2. Make your change with clear, focused commits.
3. Open a pull request describing **what** changed and **why**.
4. Add the CLA confirmation line to the PR description (see [CLA.md](CLA.md)).

---

## Building from source (maintainers & advanced users)

The published images live on GHCR (`ghcr.io/nis2pme/backend`, `ghcr.io/nis2pme/frontend`).
To build them locally instead of pulling, use the build compose file:

```bash
# Build backend + frontend images from source and run
docker compose -f docker-compose.build.yml up -d --build
```

Project layout:

```
.
├── backend/                  # FastAPI app + Alembic migrations + Dockerfile
├── frontend/                 # Vue.js 3 build + nginx config + Dockerfile
├── frameworks/               # Control frameworks (e.g. QNRCS 2026)
├── docker-compose.yml        # End-user: pulls images from GHCR
├── docker-compose.build.yml  # Maintainers: builds images from source
└── start_nis2pme.sh          # One-line installer (pulls from GHCR)
```

### Publishing images to GHCR (maintainers)

```bash
# Build with the registry tags
docker compose -f docker-compose.build.yml build

# Tag (if needed) and push
docker push ghcr.io/nis2pme/backend:latest
docker push ghcr.io/nis2pme/frontend:latest
```

> Prefer tagging releases with a version (e.g. `ghcr.io/nis2pme/backend:0.2`) in addition to
> `latest`, so end users can pin a specific version.

### Database migrations

Migrations run automatically on backend startup (`alembic upgrade head`). When changing models,
add a new Alembic revision under `backend/alembic/versions/`.

---

## Code style

- **Backend:** follow the existing FastAPI module structure (`app/<domain>/{router,service,models,schemas}.py`).
- **Frontend:** follow the existing Vue 3 + Pinia + PrimeVue conventions.
- Keep changes consistent with the surrounding code.

Thank you for helping make NIS2PME better! 🙌
