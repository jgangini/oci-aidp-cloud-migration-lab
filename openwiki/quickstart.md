# OpenWiki quickstart

## What this repository is
OCI AI Data Platform Cloud Migration Lab v1.0.2 is a Deploy Studio package and supporting application for standing up a structured Oracle AI Data Platform lab. The repository combines:

- a FastAPI backend that handles registration, administrator login, and Identity Domains user management
- a React/Vite frontend for the registration and admin experience
- Terraform and post-apply hooks that provision the OCI lab environment
- local and OCI-connected Docker Compose profiles for development and validation
- helper scripts that bootstrap local OCI testing and pre/postflight checks

The top-level README is the user-facing source of truth for safety constraints, local run modes, and Terraform flow: see [`README.md`](../README.md).

## Start here
- [Architecture overview](architecture.md)
- [Backend API and identity flows](backend/api.md)
- [Frontend UI notes](frontend/ui.md)
- [Terraform and OCI infrastructure](infrastructure/terraform.md)
- [Operations and local run modes](operations.md)
- [Testing guide](testing.md)

## Repository map

### Application runtime
- Backend app: [`apps/backend/app/main.py`](../apps/backend/app/main.py)
- Backend settings: [`apps/backend/app/config.py`](../apps/backend/app/config.py)
- Identity client: [`apps/backend/app/identity.py`](../apps/backend/app/identity.py)
- AIDP provisioning client: [`apps/backend/app/aidp.py`](../apps/backend/app/aidp.py)
- One-use operator credential installer: [`apps/backend/app/credential_bootstrap.py`](../apps/backend/app/credential_bootstrap.py)
- Industry data contracts: [`apps/backend/app/industry_kits.py`](../apps/backend/app/industry_kits.py)
- Medallion notebooks: [`apps/backend/app/notebooks.py`](../apps/backend/app/notebooks.py)
- Security helpers: [`apps/backend/app/security.py`](../apps/backend/app/security.py)
- Frontend entrypoint: [`apps/frontend/src/App.tsx`](../apps/frontend/src/App.tsx)
- Frontend styles: [`apps/frontend/src/styles.css`](../apps/frontend/src/styles.css)

### Infrastructure and packaging
- Terraform root: [`terraform/`](../terraform/)
- Deploy Studio package metadata: [`terraform/deploy-studio.json`](../terraform/deploy-studio.json)
- Container orchestration: [`docker/docker-compose.yml`](../docker/docker-compose.yml), [`docker/docker-compose.dev.yml`](../docker/docker-compose.dev.yml), [`docker/docker-compose.oci-local.yml`](../docker/docker-compose.oci-local.yml)
- Environment bootstrap: [`scripts/bootstrap_local_oci_env.py`](../scripts/bootstrap_local_oci_env.py)

### Tests
- Backend API tests: [`apps/backend/tests/test_api.py`](../apps/backend/tests/test_api.py)
- Frontend security tests: [`apps/frontend/tests/security.test.mjs`](../apps/frontend/tests/security.test.mjs)
- Terraform tests: [`terraform/tests/`](../terraform/tests/)

## Key concepts
- Release v1.0.2 uses a single private `aidp-data-<suffix>` bucket with medallion prefixes `01_landing/`, `02_bronze/`, `03_silver/`, and `04_gold/`; it uses OCI URIs and external tables, not external volumes or explicit OSCS/OpenSearch.
- Participant folders use the normalized email; jobs, bucket paths, and tables retain an opaque key inside four shared medallion schemas.
- Registration moves a user from pending to developers only after workspace, schemas, content, and permissions are complete.
- AIDP access is split between pending, developer, operator-admin, and per-participant grants. The uploaded operator remains a direct member of built-in `AI_DATA_PLATFORM_ADMIN`; its exact profile is delivered once through an authenticated encrypted envelope. There is no `AIDP_LAB_PROVISIONER`, additional API key, OAuth client, Vault secret, or runtime instance-principal fallback.
- Registration and administrator passwords are handled as PBKDF2 hashes, not plaintext secrets.
- The admin session is an HMAC-signed cookie named `__Host-aidp_lab_admin`.
- OCI-connected local development uses the same image as the VM, localhost-only ports, a sanitized ignored OCI config, and read-only binds for that config and the original operator key.

## When changing the repo
- Start by identifying which runtime mode you are touching: backend API, frontend UI, Terraform, or local operations.
- Keep the safety contract in mind from the README: never commit real secrets, keys, or Terraform state.
- If you change registration, user management, or admin auth, update the backend tests and the frontend security tests together.
- If you change OCI resource wiring, update the Terraform tests and the post-apply hook logic together.
