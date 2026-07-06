# Architecture overview

This repository packages a small web application plus OCI infrastructure around a specific lab workflow: create lab users in Identity Domains, expose the lab console link, and provision the surrounding AI Data Platform resources that make the lab usable.

## System pieces

### Backend service
The FastAPI app in [`apps/backend/app/main.py`](../apps/backend/app/main.py) exposes the public registration API and the administrator API. It:
- validates registration input, including a strict `AAAA-0000` code format
- signs an admin session cookie with a locally generated session key
- routes user creation/deletion to an Identity Domains client
- returns only a minimal public config payload and a `/api/health` probe

The app is configured from environment variables through [`apps/backend/app/config.py`](../apps/backend/app/config.py). In OCI deployment mode it expects Identity Domains, OAuth client, group IDs, and the AIDP console URL. In local development mode it can bypass OCI identity with in-memory users.

### Identity integration
[`apps/backend/app/identity.py`](../apps/backend/app/identity.py) implements the OCI-facing identity workflow. Important behaviors:
- OAuth client secrets are loaded from `IDENTITY_OAUTH_CLIENT_SECRET` or from OCI Vault via `OAUTH_SECRET_OCID`
- the client caches access tokens and retries once after a 401
- user lookup is based on email/userName, but unmanaged matches are rejected
- the lab distinguishes active and pending membership with two groups
- lab-created users are tagged with `externalId == LAB_MARKER`

### Frontend
The frontend in [`apps/frontend/src/App.tsx`](../apps/frontend/src/App.tsx) renders the registration experience and the admin workspace. It is a single-page React app that handles:
- segmented input for the registration code
- password generation and reveal controls
- login/logout flows
- the admin user table, search, create, and delete actions
- modal confirmations and basic accessibility behavior such as focus trapping

### OCI infrastructure
The Terraform package under [`infra/terraform/`](../infra/terraform/) is what turns the app into a deployable OCI lab:
- tenancy-scoped Identity Domains resources and secrets
- regional networking, Compute, Object Storage, KMS, Vault, and AIDP resources
- a single data bucket with the medallion prefixes called out in the README
- a registration VM whose bootstrap logic is generated from `templatefile/user_data.sh`
- post-apply reconciliation logic for the catalog, schemas, volumes, roles, group membership, and permissions

### Run modes
The repository supports three practical modes:
1. OCI deployment through Deploy Studio and Terraform
2. local-only development with `docker-compose.dev.yml`
3. OCI-connected local testing with `docker-compose.oci-local.yml` and `scripts/bootstrap_local_oci_env.py`

## Design boundaries
- Secrets stay out of source control. The repo uses hashes, Vault, env files, and instance principals rather than plaintext secrets.
- The backend is the boundary between browser state and OCI identity state; the frontend never stores secrets in browser storage.
- The lab distinguishes managed users from pre-existing Identity Domains accounts, which prevents accidental deletion of unmanaged identities.
- The post-apply hook is designed to be idempotent and to reconcile missing resources without replacing live ones.

## Where to look first when changing behavior
- Registration/login/auth changes: backend main app, security helpers, and API tests
- Identity-domain logic: `apps/backend/app/identity.py` and `scripts/bootstrap_local_oci_env.py`
- User experience: frontend `App.tsx`, styles, and browser security tests
- OCI topology or permissions: Terraform and `infra/terraform/hooks/post_apply.py`
