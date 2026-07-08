# Improvement Log - codex-oci-aidp-cloud-migration-lab

Use this file for evidence-backed harness improvements in this repo.

Keep entries short. Record real friction, recurring overhead, or meaningful improvements only.

## Promotion Thresholds

- 2 recurrences in this repo -> local `.codex/AGENTS.md` candidate
- 3 recurrences or safety-critical repetition -> script or skill candidate
- Cross-repo or clearly universal pattern -> global `~/.codex/AGENTS.md` candidate

## Entry Template

| Date | Task or Incident | Friction Observed | Evidence | Action Taken or Proposed | Promotion Target | Status |
| --- | --- | --- | --- | --- | --- | --- |
| YYYY-MM-DD |  |  |  |  | local AGENTS / script / skill / global AGENTS / none | captured |
| 2026-07-03 | Initial AIDP lab | OCI Provider 8.21 exposes the Identity Domains App resource but no App Templates data source | `terraform validate` rejected `oci_identity_domains_app_templates`; provider schema confirmed it absent | Use the documented `CustomWebAppTemplateId` well-known identifier directly and keep a mock-provider contract test | local AGENTS | captured |
| 2026-07-03 | Initial AIDP lab | AIDP catalog resources are REST-only and their request shapes are easy to guess incorrectly | Official 20260430 Swagger distinguishes POST permissions from PUT volume permissions | Treat the published Swagger as the hook contract and cover canonical payloads with fakes | local AGENTS | captured |
| 2026-07-03 | Deploy Studio hook integration | Repository-relative hook paths and the worker's Python dependency boundary differ from the Terraform package root | Runner validation found a missing entrypoint and no top-level `requests` module | Test the manifest path from repo root and import the OCI SDK's vendored requests in hooks | local AGENTS | captured |
| 2026-07-03 | AIDP Quick Start alignment | Generic OCI endpoint conventions differ from the AIDP Workbench production endpoint | Official Quick Start uses `aidpprod.<region>` with the API version at the root | Keep an exact endpoint assertion in the hook tests | local AGENTS | captured |
| 2026-07-03 | Final deployment audit | Serialized substring checks could combine role and permission values from different paginated rows | AIDP permission responses expose `grantee`, `granteeName`, `granteeType`, and `granteePermissions` per item | Keep same-item permission predicates and pagination tests at the REST boundary | local AGENTS | resolved |
| 2026-07-03 | Compute fallback audit | Shape listing proves compatibility, not launch capacity | OCI capacity reports expose `AVAILABLE` plus `available_count` for the requested OCPU/memory shape | Run a repository preflight capacity report and document the residual report-to-launch race | local AGENTS | resolved |
| 2026-07-07 | Live AIDP test2/test3/test4 | A closed Identity Domain JWK caused repeated OSCS failures, and a manual platform reused a required policy later deleted by another stack | Audit showed `Any Client` denied `SigningCert/jwk`; enabling public signing-certificate access fixed new OSCS setup, while restoring the 9 core statements recovered test4 Logging | Gate JWK access in preflight, keep required policy ownership stable, and separate optional network/object-deletion grants | local AGENTS | resolved |
| 2026-07-07 | Lab v2 participant reconciliation | Participant-writable storage cannot be the authority for immutable industry or exact cleanup | A participant with bucket object management could alter the public manifest before a retry or delete | Keep authoritative participant state in provisioner-only `/Workspace/lab-users/.control`; treat the visible manifest as descriptive | local AGENTS | resolved |
| 2026-07-07 | Lab v2 frontend validation | Regex-only source assertions did not exercise polling deadlines, `Retry-After`, aborts, or users behind shared NAT | Executable polling tests exposed behavior that static checks could not prove; an nginx per-IP limiter would throttle unrelated students | Keep the small pure polling helper and executable tests; rate-limit invalid codes by IP and valid reconciliations by opaque email key | local AGENTS | resolved |
| 2026-07-07 | Lab v2 architecture preflight | A second concurrent preflight overwrote the saved Sentrux baseline | The baseline timestamp and quality signal changed before postflight despite no intentional baseline reset | Make baseline capture single-owner or write-once per task and require explicit approval before restoring a user baseline | script | proposed |
