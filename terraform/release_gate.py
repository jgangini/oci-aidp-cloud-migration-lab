from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


EXPECTED_PROJECT = "oci-aidp-cloud-migration-lab"
EXPECTED_REPOSITORIES = {
    "https://github.com/jgangini/oci-aidp-cloud-migration-lab",
    "https://github.com/jgangini/oci-aidp-cloud-migration-lab.git",
}
EXPECTED_REF = "v2.0.0"
EXPECTED_REGION = "us-chicago-1"
EXPECTED_COMPARTMENT = "oci-aidp-cloud-migration-lab-4"

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SOURCE_PATTERNS = (
    ("lab-2/lab-3 reference", re.compile(r"oci-aidp-cloud-migration-lab-(?:2|3)(?![0-9])", re.IGNORECASE)),
    (
        "explicit OSCS/OpenSearch resource",
        re.compile(
            r"resource\s+\"[^\"]*(?:opensearch|oscs)[^\"]*\"|oci[._-]opensearch|/opensearch(?:/|\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "external AIDP volume",
        re.compile(
            r"volumeType\s*[\"']?\s*[:=]\s*[\"']EXTERNAL[\"']|"
            r"resource\s+\"oci_ai_data_platform[^\"]*volume[^\"]*\"",
            re.IGNORECASE,
        ),
    ),
    ("optional VNIC policy", re.compile(r"\bmanage\s+vnics\b", re.IGNORECASE)),
    ("optional subnet policy", re.compile(r"\buse\s+subnets\b", re.IGNORECASE)),
    ("optional NSG policy", re.compile(r"\buse\s+network-security-groups\b", re.IGNORECASE)),
    (
        "optional Object Storage deletion policy",
        re.compile(
            r"allow\s+service\s+objectstorage-[^\s]+\s+to\s+manage\s+object-family\b",
            re.IGNORECASE,
        ),
    ),
)


def validate_context(context: dict[str, Any]) -> None:
    source = context.get("source") or {}
    if context.get("project_id") != EXPECTED_PROJECT:
        raise ValueError(f"release requires project_id {EXPECTED_PROJECT}")
    if str(source.get("repository") or "").rstrip("/") not in EXPECTED_REPOSITORIES:
        raise ValueError("release requires the trusted GitHub repository")
    if source.get("ref") != EXPECTED_REF:
        raise ValueError(f"release requires source ref {EXPECTED_REF}")
    if not _SHA.fullmatch(str(source.get("commit_sha") or "")):
        raise ValueError("release requires a full lowercase 40-character source SHA")
    if context.get("region") != EXPECTED_REGION:
        raise ValueError(f"release requires region {EXPECTED_REGION}")
    if context.get("compartment") != EXPECTED_COMPARTMENT:
        raise ValueError(f"release requires a new compartment named {EXPECTED_COMPARTMENT}")


def _deployment_files(terraform_root: Path) -> list[Path]:
    files = list(terraform_root.glob("*.tf"))
    files.extend(terraform_root.glob("hooks/*.py"))
    files.extend(terraform_root.glob("templatefile/*.sh"))
    return sorted(path for path in files if path.is_file())


def _forbidden_finding(text: str) -> str | None:
    return next((label for label, pattern in _SOURCE_PATTERNS if pattern.search(text)), None)


def validate_source(terraform_root: Path) -> None:
    files = _deployment_files(terraform_root)
    if not files:
        raise ValueError("release source contains no deployable Terraform files")
    for path in files:
        text = path.read_text(encoding="utf-8")
        finding = _forbidden_finding(text)
        if finding:
            raise ValueError(f"release source contains {finding} in {path.relative_to(terraform_root)}")
        if 'resource "oci_objectstorage_bucket"' in text and re.search(r"\bkms_key_id\s*=", text):
            raise ValueError("the lab bucket must use its Oracle-managed encryption key")


def _has_nonempty_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return any(
            (item_key == key and item_value not in (None, "", False)) or _has_nonempty_key(item_value, key)
            for item_key, item_value in value.items()
        )
    if isinstance(value, list):
        return any(_has_nonempty_key(item, key) for item in value)
    return False


def validate_plan(plan: dict[str, Any]) -> None:
    managed = [change for change in plan.get("resource_changes") or [] if change.get("mode", "managed") == "managed"]
    if not managed:
        raise ValueError("release plan contains no managed resources")
    for resource in managed:
        address = str(resource.get("address") or resource.get("type") or "unknown resource")
        actions = (resource.get("change") or {}).get("actions") or []
        if actions != ["create"]:
            raise ValueError(f"fresh-only release rejects actions {actions!r} for {address}")
        serialized = json.dumps(resource, sort_keys=True, separators=(",", ":"))
        finding = _forbidden_finding(serialized)
        if finding:
            raise ValueError(f"release plan contains {finding} in {address}")
        resource_type = str(resource.get("type") or "").lower()
        if "ai_data_platform" in resource_type and "volume" in resource_type:
            raise ValueError(f"release plan contains an external AIDP volume in {address}")
        if resource_type == "oci_objectstorage_bucket" and _has_nonempty_key(resource.get("change"), "kms_key_id"):
            raise ValueError(f"release plan assigns a customer-managed key to {address}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the immutable v2 lab-4 release contract.")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).parent)
    parser.add_argument("--context-json", type=Path)
    parser.add_argument("--plan-json", type=Path)
    args = parser.parse_args(argv)
    try:
        validate_source(args.source_root)
        if args.context_json:
            validate_context(json.loads(args.context_json.read_text(encoding="utf-8")))
        if args.plan_json:
            validate_plan(json.loads(args.plan_json.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"release gate failed: {exc}", file=sys.stderr)
        return 1
    print("release gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
