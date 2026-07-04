#!/usr/bin/env python3
"""Additively reconcile AIDP Master Catalog resources after Terraform APPLY."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from oci._vendor import requests


API_VERSION = "20260430"
CATALOG_NAME = "aidp_lab"
ROLE_NAME = "AIDP_LAB_DEVELOPER"
LAYERS = ("landing", "bronze", "silver", "gold")
RESOURCE_WAIT_ATTEMPTS = 120


class ReconcileError(RuntimeError):
    pass


class ApiRequestError(ReconcileError):
    def __init__(self, method: str, path: str, status_code: int, request_id: str) -> None:
        super().__init__(f"AIDP {method} {path} failed with {status_code}; opc-request-id={request_id}")
        self.status_code = status_code


def read_json_env(name: str) -> dict[str, Any]:
    path = os.environ.get(name)
    if not path:
        raise ReconcileError(f"{name} is required")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_result(path: str, result: dict[str, Any]) -> None:
    target = Path(path)
    target.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass


@dataclass(slots=True)
class ApiResponse:
    status_code: int
    body: Any
    headers: dict[str, str]


class AidpApi:
    def __init__(self, region: str, platform_id: str, signer: Any, deployment_id: str) -> None:
        self.base = f"https://aidp.{region}.oci.oraclecloud.com/{API_VERSION}/aiDataPlatforms/{platform_id}"
        self.signer = signer
        self.deployment_id = deployment_id
        self.session = requests.Session()

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> ApiResponse:
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if method.upper() == "POST":
            canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            payload_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
            headers["opc-retry-token"] = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"{self.deployment_id}:{path}:{payload_hash}")
            )
        for attempt in range(5):
            try:
                response = self.session.request(
                    method,
                    f"{self.base}{path}",
                    auth=self.signer,
                    headers=headers,
                    params=params,
                    json=payload,
                    timeout=(10, 60),
                )
            except requests.exceptions.RequestException as exc:
                if attempt == 4:
                    raise ReconcileError(f"AIDP {method} {path} failed after network retries") from exc
                time.sleep(min(2**attempt, 15))
                continue
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 4:
                break
            retry_after = response.headers.get("retry-after")
            delay = min(30, int(retry_after)) if retry_after and retry_after.isdigit() else min(2**attempt, 15)
            time.sleep(delay)
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = None
        if response.status_code >= 400:
            raise ApiRequestError(method, path, response.status_code, response.headers.get("opc-request-id", "unavailable"))
        return ApiResponse(response.status_code, body, {key.lower(): value for key, value in response.headers.items()})

    def list_all(self, path: str, *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            query = dict(params or {})
            if page:
                query["page"] = page
            response = self.request("GET", path, params=query)
            body = response.body
            if isinstance(body, list):
                items.extend(body)
            elif isinstance(body, dict):
                items.extend(body.get("items") or body.get("Items") or [])
            page = response.headers.get("opc-next-page")
            if not page:
                return items


def exact_one(items: list[dict[str, Any]], name: str, kind: str) -> dict[str, Any] | None:
    matches = [item for item in items if item.get("displayName") == name]
    if len(matches) > 1:
        raise ReconcileError(f"Ambiguous {kind}: multiple resources named {name}")
    return matches[0] if matches else None


def assert_fields(resource: dict[str, Any], expected: dict[str, Any], kind: str) -> None:
    if not isinstance(resource, dict):
        raise ReconcileError(f"AIDP returned no {kind} details")
    mismatches = [key for key, value in expected.items() if resource.get(key) != value]
    if mismatches:
        raise ReconcileError(f"Existing {kind} has incompatible fields: {', '.join(mismatches)}")


def is_active_or_raise(resource: dict[str, Any], kind: str) -> bool:
    state = str(resource.get("lifecycleState") or resource.get("state") or "").upper()
    if state == "ACTIVE":
        return True
    if state in {"FAILED", "DELETING", "DELETED", "DELETE_FAILED", "CANCELED", "CANCELLED"} or state.endswith(
        "FAILED"
    ):
        raise ReconcileError(f"{kind} {resource.get('displayName', 'unknown')} entered terminal state {state}")
    return False


def ensure_resource(
    api: AidpApi,
    path: str,
    kind: str,
    name: str,
    create_payload: dict[str, Any],
    immutable_fields: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    wait_for_active: bool = False,
    attempts: int = RESOURCE_WAIT_ATTEMPTS,
) -> tuple[dict[str, Any], bool]:
    query = {"displayName": name, **(filters or {})}
    current = exact_one(api.list_all(path, params=query), name, kind)
    if current:
        if not wait_for_active or is_active_or_raise(current, kind):
            assert_fields(current, immutable_fields, kind)
            return current, False
        created = False
    else:
        created = True
        try:
            api.request("POST", path, payload=create_payload)
        except ApiRequestError as exc:
            if exc.status_code != 409:
                raise
            created = False
    for _ in range(attempts):
        current = exact_one(api.list_all(path, params=query), name, kind)
        if current:
            if not wait_for_active or is_active_or_raise(current, kind):
                assert_fields(current, immutable_fields, kind)
                return current, created
        time.sleep(5)
    target = "ACTIVE state" if wait_for_active else "visibility"
    raise ReconcileError(f"Timed out waiting for {kind} {name} {target}")


def wait_for_existing_active(
    api: AidpApi,
    path: str,
    kind: str,
    name: str,
    immutable_fields: dict[str, Any],
    *,
    attempts: int = RESOURCE_WAIT_ATTEMPTS,
) -> dict[str, Any]:
    query = {"displayName": name}
    for _ in range(attempts):
        current = exact_one(api.list_all(path, params=query), name, kind)
        if current and is_active_or_raise(current, kind):
            assert_fields(current, immutable_fields, kind)
            return current
        time.sleep(5)
    raise ReconcileError(f"Timed out waiting for existing {kind} {name} ACTIVE state")


def role_has_group(api: AidpApi, role_key: str, group_ocid: str) -> bool:
    body = api.request("GET", f"/roles/{role_key}").body
    assignees = body.get("assignees", []) if isinstance(body, dict) else []
    return any(
        isinstance(item, dict)
        and str(item.get("type", "")).upper() == "GROUP"
        and item.get("target") == group_ocid
        for item in assignees
    )


def permission_is_assigned(api: AidpApi, inspect_path: str, role_name: str, permission: str) -> bool:
    for item in api.list_all(inspect_path):
        if not isinstance(item, dict) or str(item.get("granteeType", "")).upper() != "ROLE":
            continue
        if role_name not in {item.get("grantee"), item.get("granteeName")}:
            continue
        permissions = item.get("granteePermissions", [])
        if isinstance(permissions, list) and permission in permissions:
            return True
    return False


def ensure_action(
    api: AidpApi,
    method: str,
    action_path: str,
    payload: dict[str, Any],
    is_applied: Callable[[], bool],
    *,
    attempts: int = 12,
) -> bool:
    if is_applied():
        return False
    try:
        api.request(method, action_path, payload=payload)
    except ApiRequestError as exc:
        if exc.status_code != 409:
            raise
    for _ in range(attempts):
        if is_applied():
            return True
        time.sleep(5)
    raise ReconcileError(f"AIDP action {action_path} did not converge to the requested values")


def build_signer(config_path: str, key_path: str) -> Any:
    import oci

    config = oci.config.from_file(config_path)
    required = ("tenancy", "user", "fingerprint")
    missing = [name for name in required if not config.get(name)]
    if missing:
        raise ReconcileError(f"OCI config is missing required fields: {', '.join(missing)}")
    return oci.signer.Signer(
        tenancy=config["tenancy"],
        user=config["user"],
        fingerprint=config["fingerprint"],
        private_key_file_location=key_path,
        pass_phrase=config.get("pass_phrase"),
    )


def reconcile(api: AidpApi, outputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    events: list[str] = []
    workspace_name = str(outputs["default_workspace_name"])
    workspace = wait_for_existing_active(
        api,
        "/workspaces",
        "workspace",
        workspace_name,
        {"type": "DEFAULT"},
    )

    catalog, created = ensure_resource(
        api,
        "/catalogs",
        "catalog",
        CATALOG_NAME,
        {"displayName": CATALOG_NAME, "description": "AIDP lab medallion catalog", "catalogType": "INTERNAL"},
        {"catalogType": "INTERNAL"},
        wait_for_active=True,
    )
    events.append(f"Catalog {CATALOG_NAME} {'created' if created else 'reused'}")
    catalog_key = str(catalog["key"])

    schema_resources: dict[str, dict[str, Any]] = {}
    volume_resources: dict[str, dict[str, Any]] = {}
    namespace = str(outputs["objectstorage_namespace"])
    bucket = str(outputs["bucket_name"])
    for index, layer in enumerate(LAYERS, start=1):
        schema, schema_created = ensure_resource(
            api,
            "/schemas",
            "schema",
            layer,
            {"displayName": layer, "description": f"{layer.title()} medallion schema", "catalogName": CATALOG_NAME},
            {},
            filters={"catalogKey": catalog_key},
            wait_for_active=True,
        )
        schema_key = str(schema["key"])
        schema_detail = api.request("GET", f"/schemas/{schema_key}").body
        assert_fields(schema_detail, {"displayName": layer, "catalogName": CATALOG_NAME}, "schema")
        schema_resources[layer] = schema
        events.append(f"Schema {layer} {'created' if schema_created else 'reused'}")
        volume_name = f"{layer}_data"
        location = f"oci://{bucket}@{namespace}/{index:02d}_{layer}/"
        volume, volume_created = ensure_resource(
            api,
            "/volumes",
            "volume",
            volume_name,
            {
                "displayName": volume_name,
                "description": f"External {layer} data volume",
                "catalogName": CATALOG_NAME,
                "schemaName": layer,
                "volumeType": "EXTERNAL",
                "storageLocation": location,
            },
            {"volumeType": "EXTERNAL"},
            filters={"catalogKey": catalog_key, "schemaKey": schema_key},
            wait_for_active=True,
        )
        volume_key = str(volume["key"])
        volume_detail = api.request("GET", f"/volumes/{volume_key}").body
        assert_fields(
            volume_detail,
            {
                "displayName": volume_name,
                "catalogName": CATALOG_NAME,
                "schemaName": layer,
                "volumeType": "EXTERNAL",
                "storageLocation": location,
            },
            "volume",
        )
        volume_resources[layer] = volume
        events.append(f"Volume {volume_name} {'created' if volume_created else 'reused'}")

    role, role_created = ensure_resource(
        api,
        "/roles",
        "role",
        ROLE_NAME,
        {"displayName": ROLE_NAME, "description": "AIDP migration lab developer"},
        {},
        filters={"displayName": ROLE_NAME},
    )
    events.append(f"Role {ROLE_NAME} {'created' if role_created else 'reused'}")
    role_key = str(role["key"])
    group_ocid = str(outputs["developer_group_ocid"])
    member_added = ensure_action(
        api,
        "POST",
        f"/roles/{role_key}/actions/addMember",
        {"assignees": [{"type": "GROUP", "target": group_ocid}]},
        lambda: role_has_group(api, role_key, group_ocid),
    )
    events.append(f"Developer group {'added to' if member_added else 'already in'} {ROLE_NAME}")

    permission_principal = {"type": "ROLE", "targets": [ROLE_NAME]}
    workspace_key = str(workspace["key"])
    ensure_action(
        api,
        "POST",
        f"/workspaces/{workspace_key}/actions/managePermission",
        {"assignWorkspacePermissionDetails": {"assignees": permission_principal, "permissions": ["USER"]}},
        lambda: permission_is_assigned(
            api, f"/workspaces/{workspace_key}/permissions", ROLE_NAME, "USER"
        ),
    )
    ensure_action(
        api,
        "POST",
        f"/catalogs/{catalog_key}/actions/managePermission",
        {"assignCatalogPermissionDetails": {"assignees": permission_principal, "permissions": ["SELECT"]}},
        lambda: permission_is_assigned(
            api, f"/catalogs/{catalog_key}/permissions", ROLE_NAME, "SELECT"
        ),
    )
    for layer, volume in volume_resources.items():
        volume_key = str(volume["key"])
        ensure_action(
            api,
            "PUT",
            f"/volumes/{volume_key}/actions/managePermission",
            {"assignVolumePermissionDetails": {"assignees": permission_principal, "permissions": ["WRITE"]}},
            lambda volume_key=volume_key: permission_is_assigned(
                api, f"/volumes/{volume_key}/permissions", ROLE_NAME, "WRITE"
            ),
        )
        events.append(f"Role permissions aligned for {layer}_data")

    return (
        {
            "workspace_key": workspace_key,
            "catalog_key": catalog_key,
            "schema_keys": {name: item.get("key") for name, item in schema_resources.items()},
            "volume_keys": {name: item.get("key") for name, item in volume_resources.items()},
            "role_key": role_key,
        },
        events,
    )


def wait_for_application(application_url: str, *, attempts: int = 60) -> None:
    if not application_url.startswith("https://"):
        raise ReconcileError("application_url must use HTTPS")
    health_url = f"{application_url.rstrip('/')}/api/health"
    session = requests.Session()
    for _ in range(attempts):
        try:
            response = session.get(health_url, timeout=(5, 10), verify=False)
            if response.status_code == 200 and response.json().get("status") == "ok":
                return
        except (requests.exceptions.RequestException, ValueError):
            pass
        time.sleep(5)
    raise ReconcileError("Registration application did not become healthy over HTTPS")


def build_success_result(context: dict[str, Any], reconciled: dict[str, Any], messages: list[str]) -> dict[str, Any]:
    summary = {
        "schema_version": 1,
        "deployment_id": context["deployment_id"],
        "source": context["source"],
        "resources": reconciled,
    }
    return {
        "events": [{"level": "info", "message": message} for message in messages],
        "artifacts": [
            {
                "name": "aidp_lab_summary.json",
                "content_type": "application/json",
                "content_b64": base64.b64encode((json.dumps(summary, indent=2, sort_keys=True) + "\n").encode()).decode(),
            }
        ],
        "outputs": {},
    }


def main() -> int:
    output_path = os.environ.get("DEPLOY_STUDIO_OUTPUT")
    if not output_path:
        print("DEPLOY_STUDIO_OUTPUT is required", file=sys.stderr)
        return 2
    try:
        context = read_json_env("DEPLOY_STUDIO_CONTEXT")
        read_json_env("DEPLOY_STUDIO_SECRETS")  # Validate the complete hook contract; values are intentionally unused.
        config_path = os.environ["DEPLOY_STUDIO_OCI_CONFIG"]
        key_path = os.environ["DEPLOY_STUDIO_OCI_KEY"]
        outputs = context["terraform_outputs"]
        signer = build_signer(config_path, key_path)
        api = AidpApi(context["region"], outputs["ai_data_platform_id"], signer, context["deployment_id"])
        reconciled, messages = reconcile(api, outputs)
        wait_for_application(str(outputs["application_url"]))
        messages.append("Registration application is healthy over HTTPS")
        write_result(output_path, build_success_result(context, reconciled, messages))
        return 0
    except (KeyError, OSError, ValueError, ReconcileError) as exc:
        write_result(output_path, {"events": [{"level": "error", "message": str(exc)}], "artifacts": [], "outputs": {}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
