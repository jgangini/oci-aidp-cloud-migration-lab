# Testing guide

This repository leans on tests to protect the lab’s security and workflow boundaries. The most useful thing to know is not just where tests live, but what each suite is defending.

## Backend tests
[`apps/backend/tests/test_api.py`](../apps/backend/tests/test_api.py) verifies the service contract around:
- registration code rejection before any identity calls
- strict health-check behavior
- SCIM filter safety in email validation
- active/pending/conflict registration results
- admin session handling and protected routes
- admin create/delete lifecycle
- local-development mode without OCI
- identity client singleton behavior and shutdown

## Frontend tests
[`apps/frontend/tests/security.test.mjs`](../apps/frontend/tests/security.test.mjs) is a source-based safety suite. It guards UI behavior that is easy to regress accidentally, such as:
- not storing secrets in browser storage
- keeping password inputs as password fields
- preserving the segmented registration code control
- using browser cryptography for password generation
- keeping admin routes and destructive actions behind protected UI flows

## Terraform tests
The Terraform tests under [`terraform/tests/`](../terraform/tests/) cover infrastructure shape and post-apply behavior. The recent git history shows many fixes in this area, so these tests are especially important when changing:
- VM bootstrapping or networking
- OCI capacity selection and public IP handling
- Identity Domains reconciliation
- AIDP workspace/catalog/volume permissions
- post-apply idempotency and conflict handling

## Suggested validation order
When changing multiple layers, validate in this order:
1. backend tests
2. frontend tests
3. Terraform validation/tests
4. local compose-based smoke test if the change spans the full stack

## Practical rule
If a change affects user identity, registration, session handling, or OCI permissions, there should be a test adjustment nearby. That repository pattern is already visible in the recent commit history and in the current test suite.
