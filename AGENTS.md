# OCI AIDP Cloud Migration Lab

- Keep `deploy-studio.json` and `infra/terraform` compatible with Deploy Studio schema v1.
- Never commit OCI config files, PEM keys, OAuth client secrets, passwords, access codes, Terraform state, or generated certificates.
- Preserve the single-bucket medallion contract: `01_landing/`, `02_bronze/`, `03_silver/`, `04_gold/`.
- Treat the post-apply hook as idempotent. It may add missing AIDP resources, but it must never delete or replace a mismatched live resource.
- Run `./scripts/arch-preflight.ps1` before non-trivial edits and `./scripts/arch-postflight.ps1` after them.
