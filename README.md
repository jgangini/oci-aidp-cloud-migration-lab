# OCI AI Data Platform Cloud Migration Lab

An end-to-end Oracle Cloud Infrastructure laboratory for learning data engineering with Oracle AI Data Platform (AIDP). It deploys an AIDP platform and shared workspace, shared Spark compute, a governed Oracle-managed Object Storage data plane, and a self-service registration application.

Release v1.0.0 keeps one private `aidp-data-<suffix>` bucket with `01_landing/`, `02_bronze/`, `03_silver/`, and `04_gold/` prefixes. Notebooks address these locations with OCI URIs and external tables; the package creates neither external AIDP volumes nor an explicit OSCS/OpenSearch resource. The workspace uses the participant email as the visible folder name, while an opaque key still scopes jobs, Object Storage paths, and table names.

The active industry, exact workspace path, reconciliation phase, and idempotent administrator reset journal live in `/Workspace/medallon/.control/<participant>.json`, outside each participant's `ADMIN` subtree. The visible `lab-manifest.json` is tutorial metadata only; neither it nor the student-writable bucket controls authorization, overwrite behavior, or cleanup scope.

The data bucket uses the default Oracle-managed encryption key. The lab creates no OCI Vault, KMS key, OAuth client, dedicated provisioner identity, or additional OCI API key. Identity Domains and AIDP requests use the same operator profile uploaded to Deploy Studio.

## Participant data kits

Each industry has four deterministic synthetic CSV datasets with deliberate quality defects for the Silver-stage exercises:

| Industry | Dataset row counts |
| --- | --- |
| Banking | branches 20; customers 200; accounts 320; transactions 4,000 |
| Telecommunications | plans 12; network sites 30; subscribers 250; usage events 6,000 |
| Retail | customers 300; products 150; orders 1,200; order items 3,000 |
| Healthcare | patients 240; providers 48; appointments 900; encounters 700 |

The participant root is `/Workspace/medallon/<normalized-email>/<industry>`. Four notebooks move data through Landing, Bronze, Silver, and Gold, and a personal workflow runs those stages on the shared compute. All participants collaborate through `oci_landing`, `oci_bronze`, `oci_silver`, and `oci_gold`; table names retain the opaque participant key and industry to prevent collisions.

## RBAC and registration lifecycle

The permissions are intentionally split:

| Principal | AIDP access |
| --- | --- |
| Pending participants | Workspace `USER` only; no OCI IAM permission to operate AIDP |
| Developer group | Workspace `USER`, catalog `SELECT`, shared compute `USE`, and `ADMIN` on the four collaborative schemas |
| Deployment operator | Built-in `AI_DATA_PLATFORM_ADMIN`, inherited from creating the platform and verified by post-apply |
| Individual participant | Root `READ` without cascade, own email-named folder `ADMIN` with cascade, and own job `MANAGE` |

Developer IAM can `use ai-data-platforms`, read bucket metadata, and manage objects only in the exact `aidp-data-<suffix>` bucket. The deployment operator retains its existing administrative identity; the lab creates no operator-specific user, group, role, policy, grant, or API key. Pending participants receive no AIDP IAM grant.

Registration first creates or reconciles the Identity Domains user in the pending group. The API idempotently prepares the workspace, schemas, content, and permissions. Only after every phase succeeds does it add the user to the developer group and remove pending membership. Public registration keeps the selected industry immutable. An administrator can instead use **Reset AIDP** to remove only that participant's AIDP job, tables, bucket objects, and workspace files, select another industry, and reinstall the kit without deleting the OCI Identity account. A durable operation ID makes retries resume the same reset rather than deleting the new environment again.

## Safety contract

- Operator credentials never enter Git, Terraform variables, Terraform state, VM metadata, hook artifacts, or logs. Post-apply delivers the uploaded `config` and `key.pem` to the VM exactly once through an application-encrypted Object Storage envelope.
- The uploaded `key.pem` must be an unencrypted RSA API key. Preflight rejects encrypted, unreadable, or non-RSA keys before OCI provisioning and never echoes key material or passphrases.
- The VM generates a temporary 3072-bit RSA bootstrap key. Post-apply wraps an AES-256-GCM data key with RSA-OAEP/SHA-256 and uploads only the encrypted envelope as `.bootstrap/operator-credentials.json`; no Vault, KMS key, OAuth client, or additional OCI API key is created.
- The administrator password and registration code reach Terraform only as PBKDF2 hashes. Lab users activate their own Identity Domains password from the standard OCI welcome email.
- The VM decrypts the envelope locally, verifies that the profile user matches the preflight operator OCID and that `key.pem` matches the configured fingerprint, writes both files atomically with mode `0600`, then deletes the Object Storage object and verifies its absence. The temporary bootstrap key is removed afterward.
- Participant and developer access is granted through AIDP RBAC. Post-apply verifies that the deployment operator is a direct member of built-in `AI_DATA_PLATFORM_ADMIN`; it never creates `AIDP_LAB_PROVISIONER`.
- The runtime signs both Identity Domains and AIDP requests with the installed operator profile selected by `OCI_CONFIG_FILE`. Instance principals can access only the exact one-use bootstrap object and are not a runtime authentication fallback. The VM `.env` contains identifiers and PBKDF2 hashes, but no private key, OAuth secret, or plaintext administrator credential.
- The v1.0.0 path has no explicit OSCS/OpenSearch deployment and no external AIDP volumes.
- The lab does not require the Default Identity Domain's **Access Signing Certificate** setting and does not request public JWK access; that setting remains a tenant security-policy decision.
- OCI Provider 8.21 does not expose `force_destroy`; its native delete refuses a non-empty data bucket. The medallion prefixes therefore stay virtual until the first workload write, while real lab data must be emptied before destroying the stack.
- The HTTPS certificate is self-signed and includes the public IP/FQDN as SANs, so browsers will show a trust warning.
- Tenancy-level IAM and Identity Domains resources use an OCI provider alias pinned to the tenancy home region; regional AIDP, Compute, Networking, and Object Storage resources continue to use the deployment region.

## Local application

```powershell
docker build -f docker/Dockerfile -t aidp-lab .
docker run --rm -p 8080:80 -p 8443:443 --env-file .env aidp-lab
```

Required runtime values are documented in `apps/backend/.env.example`. They include `IDENTITY_DOMAIN_URL`, `OCI_CONFIG_FILE=/etc/aidp-lab/oci/config`, `OBJECTSTORAGE_NAMESPACE`, and `BUCKET_NAME`. Identity and AIDP use the uploaded operator profile installed at `OCI_CONFIG_FILE`; there is no OAuth secret or separate provisioner setting.

`GET /api/health` is strict: missing runtime configuration, a failed signed Identity Domains query, or an inaccessible required AIDP workspace/catalog/compute or exact data bucket returns `503`. It returns `200 {"status":"ok"}` only when those registration dependencies are usable; upstream details and credentials are never returned. Successful deep probes are cached for 30 seconds and failures for 5 seconds so Docker and browser polling cannot throttle OCI.

### Local VM-equivalent profile

The development profile runs the same nginx, FastAPI and React image as the VM, but substitutes Identity Domains with in-memory users. It is intentionally local-only and cannot validate OCI policies, API signing, or AIDP permissions.

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

The bootstrap discovers exactly one active lab, its `aidp-data-<suffix>` bucket, and its Object Storage namespace. It writes runtime values to ignored `.env` and a non-secret config to `.tmp/oci-local/<suffix>/config`; that config rewrites only `key_file` to `/etc/aidp-lab/oci/key.pem`. Compose bind-mounts the generated config and the original operator `--key` file read-only. Neither file contents nor host paths are printed. Because passphrases are never copied, the supplied operator key must be unencrypted for this local profile.

Open `http://127.0.0.1:18082`. This profile has no restart policy and binds only to `127.0.0.1`; it is for development testing, not a replacement for the OCI VM. It deliberately uses HTTP so DOM-based tests can run without accepting the VM-style self-signed certificate. Stop it with `docker compose --env-file .env -f docker/docker-compose.oci-local.yml down`.

## Terraform

```powershell
cd terraform
terraform init -backend=false
terraform validate
```

The v1.0.0 preflight accepts only the trusted repository and immutable release SHA in `us-chicago-1`, while keeping the compartment name as the editable Deploy Studio input. Names follow OCI's 1-100 character alphanumeric, period, hyphen, and underscore contract. In `new` mode validation confirms that the exact name is available to create; in `existing` mode it confirms one unambiguous ACTIVE compartment. It also rejects forbidden infrastructure and policies, rejects conflicting AIDP work requests for the selected name, and checks current VM capacity. For a saved Terraform plan, run `python terraform/release_gate.py --plan-json <plan.json>`; it fails unless every managed resource action is create-only.

Deploy Studio manifest v1 currently has no hook between Resource Manager PLAN and its automatic APPLY, and it does not pass the plan JSON to repository preflight. Therefore the create-only plan check is available for manual/CI validation but cannot be enforced by this repository inside the current CloudTechNext PLAN/APPLY sequence. Do not start a lab APPLY until that explicit plan check has passed or CloudTechNext adds a post-plan/pre-apply hook.

Deploy Studio creates or resolves the target compartment before starting Resource Manager. The repository preflight discovers the tenancy home region and operator user OCID, then uses OCI `create_compute_capacity_report` in the selected availability domain for E5/E4 Flex with the requested OCPUs and memory. Only the non-secret operator OCID reaches Terraform; the uploaded config and key remain hook inputs until the encrypted one-use delivery.

The base deployment reconciles the AIDP workspace, catalog, shared compute, four collaborative schemas, root `/Workspace/medallon` folder, and pending/developer roles after verifying the operator's built-in platform administration. Registration then idempotently creates the email-named participant folder, synthetic content, notebooks, job, and individual folder/job permissions before promoting pending membership. The bucket is addressed directly through OCI URIs; no external volumes or explicit OSCS/OpenSearch deployment participate in this path. Reconciliation waits for credential consumption, asynchronous AIDP resources, and strict HTTPS health, and refuses ambiguous or conflicting resources.

The VM shape remains explicit per APPLY. The capacity report is a preselection, not a reservation, so capacity can change before instance creation. When the report says E5 is unavailable and E4 is available, preflight selects E4 without requiring another secret or user choice. If creation later fails because capacity changed, run a new APPLY; this package deliberately does not claim an automatic post-failure retry.

## Lab acceptance

For a release candidate, use structured deployment events, API responses, logs, and DOM/accessibility state rather than screenshots:

1. Require a successful Resource Manager APPLY and post-apply result, then verify `GET /api/health` returns exactly `200` and `{"status":"ok"}`.
2. Verify the workspace, catalog, shared compute, `/Workspace/medallon` root, four collaborative schemas, and the RBAC matrix above. Confirm operator membership in `AI_DATA_PLATFORM_ADMIN`, absence of `AIDP_LAB_PROVISIONER`, deletion of `.bootstrap/operator-credentials.json`, zero external volumes, and no explicit OSCS/OpenSearch resource.
3. Register one real Banking participant. Observe pending phases for identity, workspace, schemas, content, and permissions; the user must remain pending until all provisioning succeeds and then move to developers.
4. Confirm the normalized email names the participant folder, four notebooks are present, and the Banking source folder contains 20 branches, 200 customers, 320 accounts, and 4,000 transactions. The four shared schemas must contain exactly the participant's 15 opaque-key-prefixed tables without colliding with other users.
5. Run the participant workflow through Landing, Bronze, Silver, and Gold. Require a successful terminal run, `quality_issues > 0`, Bronze totals equal to Landing totals, Silver totals no greater than Bronze, and Gold `banking_customer_value` plus `banking_branch_daily`.
6. Run the workflow a second time and require the same row counts; retries must be idempotent.
7. Leave that participant and the lab active for follow-up testing; cleanup is a separate, explicit operation.

## License

This project is licensed under the [MIT License](https://github.com/jgangini/oci-aidp-cloud-migration-lab/blob/main/LICENSE).

OCI AIDP Cloud Migration Lab is an independent project and is not an official Oracle product. It is not affiliated with, endorsed by, or sponsored by Oracle Corporation. Oracle, OCI, and related marks are trademarks or registered trademarks of Oracle and/or its affiliates. Third-party trademarks, logos, service names, and assets remain the property of their respective owners.
