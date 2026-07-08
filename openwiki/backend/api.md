# Backend API and identity flows

The backend is a FastAPI service in [`apps/backend/app/main.py`](../apps/backend/app/main.py). It owns the public registration API, the administrator session, and the server-side boundary to OCI Identity Domains.

## Configuration
[`apps/backend/app/config.py`](../apps/backend/app/config.py) loads settings from environment variables. The important groups are:
- admin credentials: `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`
- registration gate: `REGISTRATION_CODE_HASH`
- OCI identity: `IDENTITY_DOMAIN_URL`, `IDENTITY_OAUTH_CLIENT_ID`, `IDENTITY_OAUTH_CLIENT_SECRET` or `OAUTH_SECRET_OCID`, `IDENTITY_DEVELOPER_GROUP_ID`, `IDENTITY_PENDING_GROUP_ID`
- AIDP runtime: `AIDP_PLATFORM_ID`, `AIDP_WORKSPACE_NAME`, `AIDP_REGION`, `OCI_CONFIG_FILE`, `OBJECTSTORAGE_NAMESPACE`, `BUCKET_NAME`, and optional `AIDP_WORKBENCH_URL`
- runtime behavior: `SESSION_SECRET_FILE`, `COOKIE_SECURE`, `LOCAL_DEVELOPMENT_MODE`

`Settings.identity_ready()` allows local development mode to bypass the OCI identity requirements.

## Public routes
- `GET /api/health` — verifies OAuth/Identity and the service-key path to the required AIDP workspace, catalog, shared compute, and exact data bucket before reporting healthy
- `GET /api/public/config` — exposes the lab name and registration code pattern
- `POST /api/register` — validates the user payload and registration code, then creates or reconciles a lab user

## Administrator routes
Administrator access is protected by the `__Host-aidp_lab_admin` cookie and the HMAC session helpers in [`apps/backend/app/security.py`](../apps/backend/app/security.py).
- `POST /api/admin/login` — verifies admin credentials and issues the session cookie
- `POST /api/admin/logout` — clears the cookie
- `GET /api/admin/session` — returns the logged-in admin username
- `GET /api/admin/settings` — exposes the AIDP console URL for the UI
- `GET /api/admin/users` — lists lab users
- `POST /api/admin/users` — creates a lab user from the admin UI
- `DELETE /api/admin/users/{user_id}` — removes a lab-created user after validating ownership

## Identity flow
[`apps/backend/app/identity.py`](../apps/backend/app/identity.py) is the important integration layer. It:
1. finds an Identity Domains user by email/userName
2. rejects unmanaged matches that predate the lab
3. creates new users with `externalId == LAB_MARKER`
4. adds the user to pending before participant provisioning starts
5. promotes the user to developers and removes pending only after provisioning succeeds
6. deletes only users created by the lab

The client treats Identity and AIDP reconciliation as eventually consistent. `POST /api/register` is idempotent and may return `202 pending` while identity, workspace, schemas, content, or permissions are being reconciled. The industry recorded for an existing participant is immutable; a conflicting selection returns `409` with instructions to delete and recreate that participant.

## AIDP participant flow
The AIDP client authenticates with the API-key config at `OCI_CONFIG_FILE`; it does not fall back to an instance principal. An opaque participant key—not an email address—names `/Workspace/lab-users/<key>/<industry>`, the personal job, and four schemas. The provisioner creates four deterministic source CSVs and four notebooks, then aligns permissions before Identity promotion.

The authoritative participant state is `/Workspace/lab-users/.control/<key>.json`, a provisioner-only sibling of the personal folders. User-editable manifests and bucket objects are never trusted to choose an industry, decide whether content may be overwritten, or scope deletion.

RBAC is exact: pending has workspace `USER`; developers have workspace `USER`, catalog `SELECT`, and shared compute `USE`; the provisioner has workspace `USER`, catalog `ADMIN`, compute `USE`, and root-folder `ADMIN`; the participant has root `READ` without cascade, own-folder `ADMIN` with cascade, own-job `MANAGE`, and schema `ADMIN` on all four personal schemas.

## Security rules to keep intact
- Registration code and admin password are verified through PBKDF2 hashes, not plaintext values.
- The backend never exposes OAuth secrets or upstream error details to clients.
- Session cookies are `HttpOnly`, `SameSite=strict`, path-scoped, and use `__Host-` naming.
- Rate limiting exists for both registration and admin login.
- `GET /api/health` must fail when OAuth/Identity is unusable, technical request signing fails, or the required workspace/catalog/compute/bucket cannot be reached. It must not expose upstream bodies or credentials.

## Tests that matter
[`apps/backend/tests/test_api.py`](../apps/backend/tests/test_api.py) covers the critical behaviors:
- invalid registration codes never reach identity
- health checks fail when identity is not usable
- email validation resists SCIM filter injection
- registration returns active/pending/conflict states correctly
- admin login/session/user management works
- local development mode can exercise the lifecycle without OCI
- the identity client is cached and closed cleanly

When editing backend behavior, update these tests first or alongside the code change.
