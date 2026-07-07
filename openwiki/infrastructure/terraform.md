# Terraform and OCI infrastructure

The Terraform package in [`terraform/`](../terraform/) is the deployment backbone for the lab. The current commit history shows repeated fixes around VM networking, OCI bootstrap, capacity selection, AIDP reconciliation, and lab-user management, which means this area is the most operationally sensitive part of the repo.

## What it provisions
Based on the root README and the Terraform file structure, the package manages:
- the OCI network and compute instance used as the registration VM
- the private Object Storage bucket that stores lab data in the four medallion prefixes
- tenancy-level Identity Domains resources such as groups and the registration OAuth application
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
The README says Deploy Studio preflight discovers the tenancy home region and checks compute capacity for the target shape before the apply proceeds. That means the deployment chooses the shape based on capacity report results rather than hard-coding a blind instance create.

### Post-apply
`terraform/hooks/post_apply.py` is responsible for reconciling missing AIDP resources after apply. The current repo guidance and tests indicate it:
- is idempotent
- can add missing resources
- must not delete or replace mismatched live resources
- authorizes catalog reconciliation and waits for asynchronous resources

### Safety contracts
The top-level README also documents two important constraints:
- the data bucket is intentionally not auto-deleted when non-empty
- tenancy-scoped Identity Domains resources use a provider alias pinned to the home region

## What to watch when editing
- Keep `terraform/deploy-studio.json` and the Terraform schema compatible with Deploy Studio v1.
- Preserve the single-bucket medallion contract.
- Treat OCI provider and resource behavior changes as backwards-compatibility risks; they have been the subject of multiple recent fixes.
- Be careful with identity and Vault changes, since secrets are deliberately split between OCI Vault, state, and local env files.

## Tests and checks
The README calls out the main validation sequence:
```powershell
cd terraform
terraform init -backend=false
terraform validate
```
The Terraform tests under [`terraform/tests/`](../terraform/tests/) and the hook tests are the place to update when changing resource wiring or post-apply logic.
