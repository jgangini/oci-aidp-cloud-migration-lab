# OCI AI Data Platform Cloud Migration Lab

An end-to-end Oracle Cloud Infrastructure laboratory for learning data engineering with Oracle AI Data Platform (AIDP). It deploys an AIDP platform and shared workspace, shared Spark compute, a governed Oracle-managed Object Storage data plane, and a self-service registration application.

Version 2 keeps one private `aidp-data-<suffix>` bucket with `01_landing/`, `02_bronze/`, `03_silver/`, and `04_gold/` prefixes. Notebooks address these locations with OCI URIs and external tables; the package creates neither external AIDP volumes nor an explicit OSCS/OpenSearch resource. Every participant gets an opaque key that is used for workspace folders and four per-participant schemas. Email addresses never appear in those names.

The immutable industry and reconciliation phase live in `/Workspace/lab-users/.control/<participant>.json`, outside each participant's `ADMIN` subtree. The visible `lab-manifest.json` is tutorial metadata only; neither it nor the student-writable bucket controls authorization, overwrite behavior, or cleanup scope.

The data bucket uses the default Oracle-managed encryption key. The lab Vault/KMS key protects only the Identity Domains OAuth secret; it is not assigned to Object Storage.

## Participant data kits

Each industry has four deterministic synthetic CSV datasets with deliberate quality defects for the Silver-stage exercises:

| Industry | Dataset row counts |
| --- | --- |
| Banking | branches 20; customers 200; accounts 320; transactions 4,000 |
| Telecommunications | plans 12; network sites 30; subscribers 250; usage events 6,000 |
| Retail | customers 300; products 150; orders 1,200; order items 3,000 |
| Healthcare | patients 240; providers 48; appointments 900; encounters 700 |

The participant root is `/Workspace/lab-users/<opaque-key>/<industry>`. Four notebooks move data through Landing, Bronze, Silver, and Gold, and a personal workflow runs those stages on the shared compute. The participant schemas are `<opaque-key>_landing`, `<opaque-key>_bronze`, `<opaque-key>_silver`, and `<opaque-key>_gold`.

## RBAC and registration lifecycle

The permissions are intentionally split:

| Principal | AIDP access |
| --- | --- |
| Pending participants | Workspace `USER` only; no OCI IAM permission to operate AIDP |
| Developer group | Workspace `USER`, catalog `SELECT`, shared compute `USE` |
| API-only technical provisioner | Workspace `USER`, catalog `ADMIN`, shared compute `USE`, and `ADMIN` on `/Workspace/lab-users` |
| Individual participant | Root `READ` without cascade, own folder `ADMIN` with cascade, own job `MANAGE`, and `ADMIN` on the four personal schemas |

Developer and provisioner IAM can `use ai-data-platforms`, read bucket metadata, and manage objects only in the exact `aidp-data-<suffix>` bucket. Pending participants receive no AIDP IAM grant.

Registration first creates or reconciles the Identity Domains user in the pending group. The API idempotently prepares the workspace, schemas, content, and permissions. Only after every phase succeeds does it add the user to the developer group and remove pending membership. The selected industry is immutable for an existing participant; a `409` instructs the operator to delete and recreate that participant instead of mixing two kits.

## Safety contract

- Deploy Studio operator credentials are never copied into the repository or VM. The deployed service uses its own least-privilege API key; the OCI-local profile bind-mounts a sanitized config and the original operator key read-only and never copies or prints the private key.
- Oracle forbids API keys on formal Identity Domains users with `serviceUser=true`. The provisioner therefore uses descriptive `user_type="Service"` with the formal service-user flag omitted, no password, no console access, and API keys as its only enabled credential type.
- The administrator password and registration code reach Terraform only as PBKDF2 hashes. Lab users activate their own Identity Domains password from the standard OCI welcome email.
- The Identity Domains OAuth client secret is written to OCI Vault. It remains sensitive in Resource Manager state because the provider returns it when the app is created; restrict access to the stack and its state.
- Participant and developer access is granted through AIDP RBAC, not broad OCI IAM. The provisioner is the only runtime principal with the administrative AIDP permissions listed above.
- The runtime uses the mounted API-key config and does not fall back to an instance principal. The generated OCI-local config contains only non-secret API metadata and points to the read-only mounted key.
- The v2 path has no explicit OSCS/OpenSearch deployment and no external AIDP volumes.
- The Default Identity Domain must enable **Access Signing Certificate** so AIDP's API Gateway can read the domain's public JWK. Deploy Studio preflight fails before provisioning when this prerequisite is disabled.
- OCI Provider 8.21 does not expose `force_destroy`; its native delete refuses a non-empty data bucket, preventing automatic lab-data deletion.
- The HTTPS certificate is self-signed and includes the public IP/FQDN as SANs, so browsers will show a trust warning.
- Tenancy-level IAM and Identity Domains resources use an OCI provider alias pinned to the tenancy home region; regional AIDP, Compute, Networking, Object Storage, KMS, and Vault resources continue to use the deployment region.

## Local application

```powershell
docker build -f docker/Dockerfile -t aidp-lab .
docker run --rm -p 8080:80 -p 8443:443 --env-file .env aidp-lab
```

Required runtime values are documented in `apps/backend/.env.example`. They include `OCI_CONFIG_FILE=/etc/aidp-lab/oci/config`, `OBJECTSTORAGE_NAMESPACE`, and `BUCKET_NAME`. For development only, a plaintext OAuth secret may be supplied through `IDENTITY_OAUTH_CLIENT_SECRET`; deployed OCI continues to read that OAuth secret from `OAUTH_SECRET_OCID`.

`GET /api/health` is strict: missing runtime configuration, an unusable Identity Domains client, a failed technical signature, or inaccessible required AIDP workspace/catalog/compute or exact data bucket returns `503`. It returns `200 {"status":"ok"}` only when those registration dependencies are usable; upstream details and secrets are never returned.

### Local VM-equivalent profile

The development profile runs the same nginx, FastAPI and React image as the VM, but substitutes Identity Domains with in-memory users. It is intentionally local-only and cannot validate OCI policies, Vault access or AIDP permissions.

```powershell
Copy-Item .env.example .env.dev
docker compose -f docker/docker-compose.dev.yml up --build -d
```

Open `https://localhost:18444` and accept the local self-signed certificate. The sample profile uses `admin` / `admin` and registration code `AIDP-2026`; change the hashes in `.env.dev` before sharing the environment. Stop it with `docker compose -f docker/docker-compose.dev.yml down`.

### OCI-connected local profile

To exercise the same image against deployed Identity Domains and AIDP resources, generate the ignored `.env` and sanitized OCI config, then use the localhost-only profile:

```powershell
.\.venv\Scripts\python.exe .\scripts\bootstrap_local_oci_env.py --config <oci-config> --key <oci-key.pem>
docker compose --env-file .env -f docker/docker-compose.oci-local.yml config --quiet
docker compose --env-file .env -f docker/docker-compose.oci-local.yml up --build --detach
```

The bootstrap discovers exactly one active lab, its `aidp-data-<suffix>` bucket, and its Object Storage namespace. It writes runtime values to ignored `.env` and a non-secret config to `.tmp/oci-local/<suffix>/config`; that config rewrites only `key_file` to `/etc/aidp-lab/oci/key.pem`. Compose bind-mounts the generated config and the original `--key` file read-only. Neither file contents nor host paths are printed. Because passphrases are never copied, this local profile requires a dedicated unencrypted API key.

Open `http://127.0.0.1:18082`. This profile has no restart policy and binds only to `127.0.0.1`; it is for development testing, not a replacement for the OCI VM. It deliberately uses HTTP so DOM-based tests can run without accepting the VM-style self-signed certificate. Stop it with `docker compose --env-file .env -f docker/docker-compose.oci-local.yml down`.

## Terraform

```powershell
cd terraform
terraform init -backend=false
terraform validate
```

The v2 preflight accepts only the trusted repository at ref `v2.0.0`, a full lowercase commit SHA, region `us-chicago-1`, and a new compartment named exactly `oci-aidp-cloud-migration-lab-4`. It also rejects forbidden infrastructure and policies, requires the Default Identity Domain public JWK, two available virtual Vault slots, no conflicting lab-4 AIDP work request, and current VM capacity. For a saved Terraform plan, run `python terraform/release_gate.py --plan-json <plan.json>`; it fails unless every managed resource action is create-only.

Deploy Studio manifest v1 currently has no hook between Resource Manager PLAN and its automatic APPLY, and it does not pass the plan JSON to repository preflight. Therefore the create-only plan check is available for manual/CI validation but cannot be enforced by this repository inside the current CloudTechNext PLAN/APPLY sequence. Do not start the lab-4 APPLY until that explicit plan check has passed or CloudTechNext adds a post-plan/pre-apply hook.

Deploy Studio creates or resolves the target compartment before starting Resource Manager. The repository preflight discovers the tenancy home region and uses OCI `create_compute_capacity_report` in the selected availability domain for E5/E4 Flex with the requested OCPUs and memory. It then supplies those non-secret selections with the compartment OCID, Object Storage namespace, deployment region, immutable 40-character source commit, and transformed secret fields.

The base deployment reconciles the AIDP workspace, catalog, shared compute, root `/Workspace/lab-users` folder, and the pending/developer/provisioner permission model. Registration then idempotently creates the opaque participant folder, four personal schemas, synthetic content, notebooks, job, and individual permissions before promoting pending membership. The bucket is addressed directly through OCI URIs; no external volumes or explicit OSCS/OpenSearch deployment participate in this path. The direct Workbench URL is derived from OCI's WebSocket endpoint when available; otherwise an administrator can set it in Settings without blocking deployment. Reconciliation waits for asynchronous resources and the HTTPS application to become strictly healthy, and refuses ambiguous or conflicting resources.

The VM shape remains explicit per APPLY. The capacity report is a preselection, not a reservation, so capacity can change before instance creation. When the report says E5 is unavailable and E4 is available, preflight selects E4 without requiring another secret or user choice. If creation later fails because capacity changed, run a new APPLY; this package deliberately does not claim an automatic post-failure retry.

## Lab acceptance

For a release candidate, use structured deployment events, API responses, logs, and DOM/accessibility state rather than screenshots:

1. Require a successful Resource Manager APPLY and post-apply result, then verify `GET /api/health` returns exactly `200` and `{"status":"ok"}`.
2. Verify the workspace, catalog, shared compute, `/Workspace/lab-users` root, and the RBAC matrix above. Confirm there are no external volumes and no explicit OSCS/OpenSearch resource.
3. Register one real Banking participant. Observe pending phases for identity, workspace, schemas, content, and permissions; the user must remain pending until all provisioning succeeds and then move to developers.
4. Confirm the email address is absent from folder and schema names. The Banking source folder must contain 20 branches, 200 customers, 320 accounts, and 4,000 transactions; the four personal schemas, four notebooks, and 15 expected catalog tables must exist.
5. Run the participant workflow through Landing, Bronze, Silver, and Gold. Require a successful terminal run, `quality_issues > 0`, Bronze totals equal to Landing totals, Silver totals no greater than Bronze, and Gold `banking_customer_value` plus `banking_branch_daily`.
6. Run the workflow a second time and require the same row counts; retries must be idempotent.
7. Leave that participant and the lab active for follow-up testing; cleanup is a separate, explicit operation.

## License

This project is licensed under the [MIT License](https://github.com/jgangini/oci-aidp-cloud-migration-lab/blob/main/LICENSE).

OCI AIDP Cloud Migration Lab is an independent project and is not an official Oracle product. It is not affiliated with, endorsed by, or sponsored by Oracle Corporation. Oracle, OCI, and related marks are trademarks or registered trademarks of Oracle and/or its affiliates. Third-party trademarks, logos, service names, and assets remain the property of their respective owners.
