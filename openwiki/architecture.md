# Architecture overview

This repository packages a small web application plus OCI infrastructure around a specific lab workflow: create lab users in Identity Domains, expose the lab console link, and provision the surrounding AI Data Platform resources that make the lab usable.

## System pieces

### Backend service
The FastAPI app in [`apps/backend/app/main.py`](../apps/backend/app/main.py) exposes the public registration API and the administrator API. It:
- validates registration input, including a strict `AAAA-0000` code format
- signs an admin session cookie with a locally generated session key
- coordinates Identity Domains with idempotent AIDP participant provisioning
- keeps the user pending until workspace, schemas, content, permissions, and developer promotion succeed
- returns only a minimal public config payload and a strict `/api/health` probe

The app is configured through [`apps/backend/app/config.py`](../apps/backend/app/config.py). OCI mode requires Identity Domains, OAuth client, group IDs, AIDP identifiers, `OCI_CONFIG_FILE`, `OBJECTSTORAGE_NAMESPACE`, and `BUCKET_NAME`. Local-only development can use in-memory adapters.

### Identity integration
[`apps/backend/app/identity.py`](../apps/backend/app/identity.py) implements the OCI-facing identity workflow. Important behaviors:
- OAuth client secrets are loaded from `IDENTITY_OAUTH_CLIENT_SECRET` or from OCI Vault via `OAUTH_SECRET_OCID`
- the client caches access tokens and retries once after a 401
- user lookup is based on email/userName, but unmanaged matches are rejected
- new or reconciled users enter pending before participant resources are created
- developer membership is added and pending membership removed only after provisioning succeeds
- lab-created users are tagged with `externalId == LAB_MARKER`

### Frontend
The frontend in [`apps/frontend/src/App.tsx`](../apps/frontend/src/App.tsx) renders the registration experience and the admin workspace. It is a single-page React app that handles:
- segmented input for the registration code
- phase-aware registration with bounded 2/4/8/16/30-second retries and a ten-minute deadline
- login/logout flows
- the admin user table, search, create, and delete actions
- modal confirmations and basic accessibility behavior such as focus trapping

### OCI infrastructure
The Terraform package under [`terraform/`](../terraform/) is what turns the app into a deployable OCI lab:
- tenancy-scoped Identity Domains resources and secrets
- regional networking, Compute, Object Storage, KMS, Vault, and AIDP resources
- a single Oracle-managed Object Storage data plane with the medallion prefixes called out in the README
- a registration VM whose bootstrap logic is generated from `templatefile/user_data.sh`
- post-apply reconciliation for the workspace, catalog, shared compute, root folder, roles, group membership, and permissions

### Participant isolation and RBAC
Folders and schemas use a deterministic opaque participant key, never an email address. Each participant receives `/Workspace/lab-users/<key>/<industry>`, four personal schemas, four notebooks, and one personal job. Data uses OCI URIs in the single `aidp-data-<suffix>` bucket; there are no external AIDP volumes and no explicit OSCS/OpenSearch resource.

Provisioning state is held under `/Workspace/lab-users/.control/<key>.json`, where only the provisioner has inherited administration. The manifest inside the participant folder is descriptive and deliberately not trusted for industry immutability, drift repair, or cleanup.

- Pending: workspace `USER` only and no OCI IAM grant to operate AIDP.
- Developer group: workspace `USER`, catalog `SELECT`, shared compute `USE`.
- API-only technical provisioner: descriptive `user_type="Service"`, formal `serviceUser` disabled because Oracle forbids API keys on it, no password or console, workspace `USER`, catalog `ADMIN`, shared compute `USE`, root folder `ADMIN`.
- Participant: root `READ` without cascade, own folder `ADMIN` with cascade, own job `MANAGE`, four personal schemas `ADMIN`.

Developer and provisioner IAM can `use ai-data-platforms`, read bucket metadata, and manage objects only in the exact `aidp-data-<suffix>` bucket. Pending participants have no AIDP IAM grant.

### Run modes
The repository supports three practical modes:
1. OCI deployment through Deploy Studio and Terraform
2. local-only development with `docker/docker-compose.dev.yml`
3. OCI-connected local testing with `docker/docker-compose.oci-local.yml` and `scripts/bootstrap_local_oci_env.py`

## Design boundaries
- Secrets stay out of source control. The repo uses hashes, Vault, ignored env files, and a dedicated least-privilege service API key. There is no instance-principal fallback for AIDP provisioning.
- The backend is the boundary between browser state and OCI identity state; the frontend never stores secrets in browser storage.
- OCI-local generates only a sanitized non-secret config and bind-mounts the original operator key read-only.
- The lab distinguishes managed users from pre-existing Identity Domains accounts, which prevents accidental deletion of unmanaged identities.
- The post-apply hook is designed to be idempotent and to reconcile missing resources without replacing live ones.

## Where to look first when changing behavior
- Registration/login/auth changes: backend main app, security helpers, and API tests
- Identity-domain logic: `apps/backend/app/identity.py` and `scripts/bootstrap_local_oci_env.py`
- User experience: frontend `App.tsx`, styles, and browser security tests
- OCI topology or permissions: Terraform and `terraform/hooks/post_apply.py`
