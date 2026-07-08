# Operations and local run modes

This repository has three operational modes that matter for development and validation.

## 1. Full OCI deployment
The top-level README describes the Deploy Studio package that provisions OCI resources, creates the VM, and wires Identity Domains and AIDP together. This is the production-like path and the one that matters most for lab operators.

## 2. Local-only development profile
[`docker/docker-compose.dev.yml`](../docker/docker-compose.dev.yml) runs the same image with a local `.env.dev` file and localhost-only ports. It uses in-memory users instead of OCI Identity Domains, so it is good for UI and backend iteration but cannot prove cloud permissions.

Typical flow from the README:
```powershell
Copy-Item .env.example .env.dev
docker compose -f docker/docker-compose.dev.yml up --build -d
```
Stop it with:
```powershell
docker compose -f docker/docker-compose.dev.yml down
```

## 3. OCI-connected local profile
[`docker/docker-compose.oci-local.yml`](../docker/docker-compose.oci-local.yml) runs the same image against live OCI resources on localhost-only ports. It is intentionally not a replacement for the VM.

The bootstrap helper [`scripts/bootstrap_local_oci_env.py`](../scripts/bootstrap_local_oci_env.py) discovers the live lab resources and writes a local `.env` without printing secrets. It can also self-check its env rendering and secret escaping behavior.

```powershell
.\.venv\Scripts\python.exe .\scripts\bootstrap_local_oci_env.py --config <oci-config> --key <oci-key.pem>
docker compose --env-file .env -f docker/docker-compose.oci-local.yml config --quiet
docker compose --env-file .env -f docker/docker-compose.oci-local.yml up --build --detach
```

The generated `.env` includes `OCI_CONFIG_FILE=/etc/aidp-lab/oci/config`, `OBJECTSTORAGE_NAMESPACE`, `BUCKET_NAME`, and two host paths used only for Compose binds. The helper also writes a sanitized, non-secret `.tmp/oci-local/<suffix>/config` whose `key_file` points inside the container. Compose mounts that config and the original operator key as read-only files; it never copies or prints the private key. The container authenticates with that config and has no instance-principal fallback. OCI-local rejects a config passphrase instead of copying it, so use a dedicated unencrypted API key.

## Operational scripts
- [`scripts/arch-preflight.ps1`](../scripts/arch-preflight.ps1) — preflight checks before substantial changes
- [`scripts/arch-postflight.ps1`](../scripts/arch-postflight.ps1) — follow-up checks after changes
- [`scripts/bootstrap_local_oci_env.py`](../scripts/bootstrap_local_oci_env.py) — generate an OCI-backed local env file

The repo root README says the architecture scripts should bracket non-trivial edits. The scripts themselves are part of the operational contract, so changes to runtime behavior should usually include a quick pass through those checks.

## What to watch out for
- Do not use the OCI-connected local profile as a substitute for the VM; it has no restart policy and binds only to `127.0.0.1`.
- Never commit generated `.env` files, keys, state, or certificates.
- `.tmp/` is ignored because it holds only the generated non-secret OCI-local config; the original private key remains at the operator-supplied path.
- The local development profile uses sample hashes and fake users; it does not validate real OCI IAM or Vault access.
- The HTTPS endpoints are self-signed in the lab environment, so browser trust warnings are expected.

## Useful checks
- Backend tests are the fastest way to validate registration/login/session behavior.
- Frontend security tests protect against accidental browser-side secret persistence.
- Terraform validation is the best first check for infrastructure edits.
- `python scripts/bootstrap_local_oci_env.py --self-check` validates env escaping and the sanitized container-key path without contacting OCI.
