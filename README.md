# OCI AI Data Platform Cloud Migration Lab

An end-to-end Oracle Cloud Infrastructure laboratory for learning data engineering with Oracle AI Data Platform (AIDP). It deploys a private medallion data lake, an AIDP platform and shared workspace, a shared Spark compute environment, and a self-service registration application for lab participants.

The laboratory creates one governed `aidp-data-*` Object Storage bucket with `01_landing`, `02_bronze`, `03_silver`, and `04_gold` prefixes; the corresponding AIDP catalog, schemas, and external volumes; Identity Domains groups and roles for developers; and a HTTPS registration/admin VM. Each participant selects an industry and receives an individual workspace folder with synthetic CSV data, documented notebooks that move data through Landing, Bronze, Silver, and Gold, plus a workflow that runs the four notebook stages on the shared compute. Administrators can monitor users, configure the direct AIDP Workbench URL, and manage lab access.

## Safety contract

- OCI API credentials are uploaded to Deploy Studio and are never copied into this repository or the VM.
- The administrator password and registration code reach Terraform only as PBKDF2 hashes. Lab users activate their own Identity Domains password from the standard OCI welcome email.
- The Identity Domains OAuth client secret is written to OCI Vault. It remains sensitive in Resource Manager state because the provider returns it when the app is created; restrict access to the stack and its state.
- Developer IAM can list bucket metadata in the lab compartment but can manage objects only in `aidp-data-<suffix>`; AIDP service object access follows Oracle's governing AIDP tag conditions.
- The Default Identity Domain must enable **Access Signing Certificate** so AIDP's API Gateway can read the domain's public JWK. Deploy Studio preflight fails before provisioning when this prerequisite is disabled.
- OCI Provider 8.21 does not expose `force_destroy`; its native delete refuses a non-empty data bucket, preventing automatic lab-data deletion.
- The HTTPS certificate is self-signed and includes the public IP/FQDN as SANs, so browsers will show a trust warning.
- Tenancy-level IAM and Identity Domains resources use an OCI provider alias pinned to the tenancy home region; regional AIDP, Compute, Networking, Object Storage, KMS, and Vault resources continue to use the deployment region.

## Local application

```powershell
docker build -f docker/Dockerfile -t aidp-lab .
docker run --rm -p 8080:80 -p 8443:443 --env-file .env aidp-lab
```

Required runtime values are documented in `apps/backend/.env.example`. For development only, a plaintext OAuth secret may be supplied through `IDENTITY_OAUTH_CLIENT_SECRET`; OCI uses `OAUTH_SECRET_OCID` and an instance principal instead.
`GET /api/health` reports healthy only after obtaining a usable OAuth token and completing a minimal Identity Domains Users query; upstream details and secrets are never returned.

### Local VM-equivalent profile

The development profile runs the same nginx, FastAPI and React image as the VM, but substitutes Identity Domains with in-memory users. It is intentionally local-only and cannot validate OCI policies, Vault access or AIDP permissions.

```powershell
Copy-Item .env.example .env.dev
docker compose -f docker/docker-compose.dev.yml up --build -d
```

Open `https://localhost:18444` and accept the local self-signed certificate. The sample profile uses `admin` / `admin` and registration code `AIDP-2026`; change the hashes in `.env.dev` before sharing the environment. Stop it with `docker compose -f docker/docker-compose.dev.yml down`.

### OCI-connected local profile

To exercise the same image against the already deployed Identity Domains and AIDP resources, generate the ignored `.env` locally and use the localhost-only profile:

```powershell
.\.venv\Scripts\python.exe .\scripts\bootstrap_local_oci_env.py --config <oci-config> --key <oci-key.pem>
docker compose -f docker/docker-compose.oci-local.yml up --build --detach
```

Open `http://127.0.0.1:18082`. This profile has no restart policy and binds only to `127.0.0.1`; it is for development testing, not a replacement for the OCI VM. It deliberately uses HTTP so the Codex browser can test the application without accepting the VM-style self-signed certificate.

## Terraform

```powershell
cd terraform
terraform init -backend=false
terraform validate
```

Deploy Studio creates or resolves the target compartment before starting Resource Manager. The repository preflight discovers the tenancy home region and uses OCI `create_compute_capacity_report` in the selected availability domain for E5/E4 Flex with the requested OCPUs and memory. It then supplies those non-secret selections with the compartment OCID, Object Storage namespace, deployment region, immutable 40-character source commit, and transformed secret fields. `post_apply.py` reconciles the Master Catalog, four schemas, four external volumes, the AIDP developer role, group membership, workspace/catalog/volume permissions, and the shared Quickstart Spark compute. Each activated user receives an industry-specific folder, four synthetic CSVs, four medallion notebooks, and a chained workflow in the shared workspace. The direct Workbench URL is derived from OCI's WebSocket endpoint when available; otherwise an administrator can set it in Settings without blocking deployment. The shared workspace intentionally does not provide private isolation between developer-group members. It waits for asynchronous resources and the HTTPS application to become active, and refuses ambiguous or conflicting resources.

The VM shape remains explicit per APPLY. The capacity report is a preselection, not a reservation, so capacity can change before instance creation. When the report says E5 is unavailable and E4 is available, preflight selects E4 without requiring another secret or user choice. If creation later fails because capacity changed, run a new APPLY; this package deliberately does not claim an automatic post-failure retry.

## License

This project is licensed under the [MIT License](https://github.com/jgangini/oci-aidp-cloud-migration-lab/blob/main/LICENSE).

OCI AIDP Cloud Migration Lab is an independent project and is not an official Oracle product. It is not affiliated with, endorsed by, or sponsored by Oracle Corporation. Oracle, OCI, and related marks are trademarks or registered trademarks of Oracle and/or its affiliates. Third-party trademarks, logos, service names, and assets remain the property of their respective owners.
