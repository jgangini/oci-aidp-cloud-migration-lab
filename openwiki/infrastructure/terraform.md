# Terraform and OCI infrastructure

The Terraform package in [`terraform/`](../terraform/) is the deployment backbone for release v1.0.0. The current commit history shows repeated fixes around VM networking, OCI bootstrap, capacity selection, AIDP reconciliation, and lab-user management, which means this area is the most operationally sensitive part of the repo.

## What it provisions
Based on the root README and the Terraform file structure, the package manages:
- the OCI network and compute instance used as the registration VM
- the private `aidp-data-<suffix>` bucket used by the Oracle-managed Object Storage data plane
- tenancy-level pending/developer Identity Domains groups and scoped IAM policies; no dedicated provisioner identity or role
- the Oracle AI Data Platform resource and its associated workspace/catalog setup
- outputs needed by Deploy Studio and the post-apply workflow

## File layout
The current Terraform files are intentionally split by major resource group:
- [`a_versions.tf`](../terraform/a_versions.tf) — versions and providers
- [`b_variables.tf`](../terraform/b_variables.tf) — inputs
- [`c_naming.tf`](../terraform/c_naming.tf) — naming helpers
- [`d_main.tf`](../terraform/d_main.tf) — provider and shared setup
- [`e_oci_core_vcn.tf`](../terraform/e_oci_core_vcn.tf) — networking
- [`f_oci_objectstorage_bucket.tf`](../terraform/f_oci_objectstorage_bucket.tf) — data bucket
- [`g_oci_core_instance.tf`](../terraform/g_oci_core_instance.tf) — lab VM
- [`h_oci_identity.tf`](../terraform/h_oci_identity.tf) — Identity Domains and IAM
- [`i_oci_ai_data_platform.tf`](../terraform/i_oci_ai_data_platform.tf) — AIDP resources
- [`j_outputs.tf`](../terraform/j_outputs.tf) — outputs
- [`hooks/post_apply.py`](../terraform/hooks/post_apply.py) — reconciliation hook

## Important operational behavior
### Preflight
The README says Deploy Studio preflight discovers the tenancy home region and operator user OCID, then checks compute capacity for the target shape before the apply proceeds. The non-secret OCID reaches Terraform; the uploaded config and key remain private hook inputs until one-use delivery. The deployment chooses the shape based on capacity report results rather than hard-coding a blind instance create.

### Post-apply
`terraform/hooks/post_apply.py` is responsible for reconciling missing AIDP resources after apply. The current repo guidance and tests indicate it:
- is idempotent
- can add missing resources
- must not delete or replace mismatched live resources
- authorizes catalog reconciliation and waits for asynchronous resources
- verifies the operator's direct built-in `AI_DATA_PLATFORM_ADMIN` membership and aligns the workspace, catalog, shared compute, `/Workspace/medallon` root, four collaborative schemas, and pending/developer RBAC

Participant provisioning uses external tables over OCI URIs in the one lab bucket. It does not create external AIDP volumes or an explicit OSCS/OpenSearch resource. Developer IAM may `use ai-data-platforms`, read bucket metadata, and manage objects only in the exact `aidp-data-<suffix>` bucket; the operator keeps the administrative identity used to create AIDP, and AIDP-internal permissions remain the primary authorization layer.

The bucket intentionally omits `kms_key_id`, so OCI encrypts it with an Oracle-managed key. Release v1.0.0 creates no Vault, KMS key, secret, OAuth application, dedicated provisioner, or additional API key. The VM creates a temporary RSA 3072-bit bootstrap key. Post-apply encrypts the exact operator config/key with AES-256-GCM, wraps the data key with RSA-OAEP/SHA-256, and uploads the envelope to `.bootstrap/operator-credentials.json`. The VM validates the operator OCID and fingerprint, installs the profile atomically with mode `0600`, deletes and verifies removal of the object, and removes the temporary RSA key before runtime readiness.

### Safety contracts
The top-level README also documents these important constraints:
- the data bucket is intentionally not auto-deleted when non-empty
- tenancy-scoped Identity Domains resources use a provider alias pinned to the home region
- Deploy Studio operator credentials are intentionally installed in the root-only VM runtime directory only after authenticated decryption and exact identity/fingerprint validation; they never enter Terraform variables/state, metadata, artifacts, or logs
- the instance principal can manage only the exact one-use bootstrap object and is never a runtime authentication fallback

## What to watch when editing
- Keep `terraform/deploy-studio.json` and the Terraform schema compatible with Deploy Studio v1.
- Preserve the single-bucket medallion contract.
- Treat OCI provider and resource behavior changes as backwards-compatibility risks; they have been the subject of multiple recent fixes.
- Be careful with the exact-object bootstrap policy, envelope encryption, fingerprint validation, verified object deletion, and temporary-key cleanup. `.env` stores identifiers and the `OCI_CONFIG_FILE` path rather than private-key material.

## Tests and checks
The README calls out the main validation sequence:
```powershell
cd terraform
terraform init -backend=false
terraform validate
```
The Terraform tests under [`terraform/tests/`](../terraform/tests/) and the hook tests are the place to update when changing resource wiring or post-apply logic.
