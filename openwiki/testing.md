# Testing guide

Release v1.0.0 leans on tests to protect the lab’s security and workflow boundaries. The most useful thing to know is not just where tests live, but what each suite is defending.

## Backend tests
[`apps/backend/tests/test_api.py`](../apps/backend/tests/test_api.py) verifies the service contract around:
- registration code rejection before any identity calls
- strict health-check behavior
- SCIM filter safety in email validation
- active/pending/conflict registration results
- phase progression, immutable industry conflicts, and pending-to-developer promotion
- opaque participant keys, four per-participant schemas, RBAC alignment, and idempotent retries
- deterministic industry row counts and medallion notebook contracts
- admin session handling and protected routes
- admin create/delete lifecycle
- local-development mode without OCI
- identity client singleton behavior and shutdown

## Frontend tests
[`apps/frontend/tests/security.test.mjs`](../apps/frontend/tests/security.test.mjs) is a source-based safety suite. It guards UI behavior that is easy to regress accidentally, such as:
- not storing secrets in browser storage
- keeping password inputs as password fields
- preserving the segmented registration code control
- preserving phased backoff/deadline/abort behavior and exposing the AIDP link only for active users
- keeping admin routes and destructive actions behind protected UI flows

## Terraform tests
The Terraform tests under [`terraform/tests/`](../terraform/tests/) cover infrastructure shape and post-apply behavior. The recent git history shows many fixes in this area, so these tests are especially important when changing:
- VM bootstrapping or networking
- OCI capacity selection and public IP handling
- Identity Domains reconciliation
- provisioner API-key generation, public-key registration, and signed Identity Domains requests
- AIDP workspace/catalog/compute/folder/schema/job permissions
- post-apply idempotency and conflict handling

## OCI-local and live acceptance
Run `python scripts/bootstrap_local_oci_env.py --self-check`, then validate `docker/docker-compose.oci-local.yml` with the generated `.env`. The profile must mount the sanitized config and original key read-only, bind only to localhost, and report healthy only when a signed Identity Domains query plus the provisioner-key AIDP workspace/catalog/compute and exact-bucket checks pass.

The release acceptance uses one real Banking participant. Require opaque folder/schema names; 20/200/320/4,000 Landing rows; four notebooks; four schemas; 15 catalog tables; `quality_issues > 0`; Bronze totals equal to Landing; Silver totals no greater than Bronze; a successful chained job; and Gold `banking_customer_value` plus `banking_branch_daily`. Run it twice and require identical counts. Promotion from pending to developers happens only after permissions complete. Use structured state and logs, not screenshots, and leave the participant active for follow-up.

## Suggested validation order
When changing multiple layers, validate in this order:
1. backend tests
2. frontend tests
3. Terraform validation/tests
4. local compose-based smoke test if the change spans the full stack

## Practical rule
If a change affects user identity, registration, session handling, or OCI permissions, there should be a test adjustment nearby. That repository pattern is already visible in the recent commit history and in the current test suite.
