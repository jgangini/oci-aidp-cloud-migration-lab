# Backend API and identity flows

The backend is a FastAPI service in [`apps/backend/app/main.py`](../apps/backend/app/main.py). It owns the public registration API, the administrator session, and the server-side boundary to OCI Identity Domains.

## Configuration
[`apps/backend/app/config.py`](../apps/backend/app/config.py) loads settings from environment variables. The important groups are:
- admin credentials: `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`
- registration gate: `REGISTRATION_CODE_HASH`
- OCI identity: `IDENTITY_DOMAIN_URL`, `IDENTITY_DEVELOPER_GROUP_ID`, `IDENTITY_PENDING_GROUP_ID`, and the uploaded operator profile installed at `OCI_CONFIG_FILE`
- AIDP runtime: `AIDP_PLATFORM_ID`, `AIDP_WORKSPACE_NAME`, `AIDP_REGION`, `OCI_CONFIG_FILE`, `OBJECTSTORAGE_NAMESPACE`, `BUCKET_NAME`, and optional `AIDP_WORKBENCH_URL`
- runtime behavior: `SESSION_SECRET_FILE`, `COOKIE_SECURE`, `LOCAL_DEVELOPMENT_MODE`

`Settings.identity_ready()` allows local development mode to bypass the OCI identity requirements.

## Public routes
- `GET /api/health` — verifies a signed Identity Domains query and the same operator-profile path to the required AIDP workspace, catalog, shared compute, and exact data bucket before reporting healthy
- `GET /api/public/config` — exposes the lab name and registration code pattern
- `POST /api/register` — validates the user payload and registration code, then creates or reconciles a lab user

## Administrator routes
Administrator access is protected by the `__Host-aidp_lab_admin` cookie and the HMAC session helpers in [`apps/backend/app/security.py`](../apps/backend/app/security.py).
- `POST /api/admin/login` — verifies admin credentials and issues the session cookie
- `POST /api/admin/logout` — clears the cookie
- `GET /api/admin/session` — returns the logged-in admin username
- `GET /api/admin/settings` — exposes the AIDP console URL and whether a lab registration code is configured; it never returns the code
- `PUT /api/admin/settings` — updates the AIDP console URL and can rotate the registration code; the replacement is persisted only as a PBKDF2 hash
- `GET /api/admin/users` — lists lab users
- `POST /api/admin/users` — creates a lab user from the admin UI
- `POST /api/admin/users/{user_id}/reset` — idempotently removes and reinstalls only the participant's AIDP environment, optionally changing industry while preserving OCI Identity
- `DELETE /api/admin/users/{user_id}` — removes a lab-created user after validating ownership

## Identity flow
[`apps/backend/app/identity.py`](../apps/backend/app/identity.py) is the important integration layer. It:
1. loads the `DEFAULT` OCI profile from `OCI_CONFIG_FILE` and signs Identity Domains HTTP requests with the deployment operator's existing API key
2. finds an Identity Domains user by email/userName
3. rejects unmanaged matches that predate the lab
4. creates new users with `externalId == LAB_MARKER`
5. adds the user to pending before participant provisioning starts
6. promotes the user to developers and removes pending only after provisioning succeeds
7. deletes only users created by the lab

The client treats Identity and AIDP reconciliation as eventually consistent. `POST /api/register` is idempotent and may return `202 pending` while identity, workspace, schemas, content, or permissions are being reconciled. The public registration industry remains immutable. The administrator reset endpoint accepts a UUID operation ID, journals `cleanup -> provision -> complete`, and permits changing industry without deleting the managed Identity Domains user. Retrying the same operation resumes it; a different operation conflicts while one is active.

## AIDP participant flow
The Identity Domains and AIDP clients authenticate with the same operator profile at `OCI_CONFIG_FILE`; neither falls back to OAuth or an instance principal. The encrypted one-use bootstrap happens before request handling, so the application only reads the validated root-only runtime profile. The normalized email names `/Workspace/medallon/<email>/<industry>`, while an opaque participant key scopes the personal job, table names, and Object Storage paths. The operator-backed service creates four deterministic source CSVs and four notebooks, then aligns permissions before Identity promotion.

The authoritative participant state is `/Workspace/medallon/.control/<key>.json`, an operator-admin-only sibling of the personal folders. It stores the exact workspace path, active industry, and reset journal; user-editable manifests and bucket objects never scope deletion.

RBAC is exact: pending has workspace `USER`; developers have workspace `USER`, catalog `SELECT`, shared compute `USE`, and `ADMIN` on the four collaborative schemas; the deployment operator is verified as a direct member of built-in `AI_DATA_PLATFORM_ADMIN`; each participant has root `READ` without cascade, own-folder `ADMIN` with cascade, and own-job `MANAGE`. The lab creates no `AIDP_LAB_PROVISIONER` role.

## Security rules to keep intact
- Registration code and admin password are verified through PBKDF2 hashes, not plaintext values.
- The backend never exposes API-key material or upstream error details to clients.
- The operator config/key never enters API payloads, environment variables, or logs; runtime reads only the atomically installed `0600` files selected by `OCI_CONFIG_FILE`.
- Session cookies are `HttpOnly`, `SameSite=strict`, path-scoped, and use `__Host-` naming.
- Rate limiting exists for both registration and admin login.
- `GET /api/health` must fail when the signed Identity Domains query fails or the required workspace/catalog/compute/bucket cannot be reached. It must not expose upstream bodies or credentials.

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
