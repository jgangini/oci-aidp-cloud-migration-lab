#!/usr/bin/env python3
"""Additively reconcile AIDP Master Catalog resources after Terraform APPLY."""

from __future__ import annotations

import base64
import configparser
import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from oci._vendor import requests


API_VERSION = "20260430"
CATALOG_NAME = "aidp_lab"
DEVELOPER_ROLE_NAME = "AIDP_LAB_DEVELOPER"
PENDING_ROLE_NAME = "AIDP_LAB_PENDING"
PROVISIONER_ROLE_NAME = "AIDP_LAB_PROVISIONER"
SHARED_COMPUTE_NAME = "aidp_lab_shared_compute"
LAYERS = ("landing", "bronze", "silver", "gold")
RESOURCE_WAIT_ATTEMPTS = 120
PUBLIC_KEY_SCRIPT = (
    "attempt=0; while [ \"$attempt\" -lt 120 ]; do "
    "if [ -x /usr/local/sbin/aidp-lab-public-key ]; then "
    "exec sudo /usr/local/sbin/aidp-lab-public-key; fi; "
    "attempt=$((attempt + 1)); sleep 5; done; exit 1"
)


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
        self.base = f"https://aidpprod.{region}.oci.oraclecloud.com/{API_VERSION}/aiDataPlatforms/{platform_id}"
        self.signer = signer
        self.deployment_id = deployment_id
        self.session = requests.Session()

    def _send(
        self,
        method: str,
        path: str,
        request_headers: dict[str, str],
        payload: dict[str, Any] | None,
        data: bytes | None,
        params: dict[str, Any] | None,
    ) -> Any:
        for attempt in range(5):
            try:
                response = self.session.request(
                    method,
                    f"{self.base}{path}",
                    auth=self.signer,
                    headers=request_headers,
                    params=params,
                    json=payload if data is None else None,
                    data=data,
                    timeout=(10, 60),
                )
            except requests.exceptions.RequestException as exc:
                if attempt == 4:
                    raise ReconcileError(f"AIDP {method} {path} failed after network retries") from exc
                time.sleep(min(2**attempt, 15))
                continue
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 4:
                return response
            retry_after = response.headers.get("retry-after")
            delay = min(30, int(retry_after)) if retry_after and retry_after.isdigit() else min(2**attempt, 15)
            time.sleep(delay)
        raise ReconcileError(f"AIDP {method} {path} exhausted retries")

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> ApiResponse:
        request_headers = {"Accept": "application/json", **(headers or {})}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        if method.upper() == "POST":
            content = data if data is not None else json.dumps(
                payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            payload_hash = hashlib.sha256(content).hexdigest()
            object_type = str(request_headers.get("type") or path.strip("/").split("/", 1)[0] or "root")
            request_headers["opc-retry-token"] = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{self.deployment_id}:{method.upper()}:{path}:{object_type}:{payload_hash}",
                )
            )
        response = self._send(method, path, request_headers, payload, data, params)
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = response.content
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
    if state == "ACTIVE" or (kind == "shared compute" and state == "STOPPED"):
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


def role_has_member(
    api: AidpApi,
    role_key: str,
    principal_type: str,
    principal_id: str,
) -> bool:
    body = api.request("GET", f"/roles/{role_key}").body
    assignees = (body.get("assignees") or []) if isinstance(body, dict) else []
    return any(
        isinstance(item, dict)
        and str(item.get("type", "")).upper() == principal_type.upper()
        and item.get("target") == principal_id
        for item in assignees
    )


def role_has_group(api: AidpApi, role_key: str, group_ocid: str) -> bool:
    return role_has_member(api, role_key, "GROUP", group_ocid)


def assert_role_members_exact(
    api: AidpApi,
    role_key: str,
    role_name: str,
    principal_type: str,
    principal_id: str,
) -> None:
    body = api.request("GET", f"/roles/{role_key}").body
    assignees = (body.get("assignees") or []) if isinstance(body, dict) else []
    actual = {
        (str(item.get("type", "")).upper(), str(item.get("target", "")))
        for item in assignees
        if isinstance(item, dict)
    }
    if actual != {(principal_type.upper(), principal_id)}:
        raise ReconcileError(
            f"Role {role_name} has unexpected members; remove the broader assignments before retrying"
        )


def permission_is_assigned(
    api: AidpApi,
    inspect_path: str,
    role_name: str,
    permission: str,
    inheritable: bool | None = None,
) -> bool:
    matches: list[dict[str, Any]] = []
    for item in api.list_all(inspect_path):
        if not isinstance(item, dict) or str(item.get("granteeType", "")).upper() != "ROLE":
            continue
        if role_name not in {item.get("grantee"), item.get("granteeName")}:
            continue
        matches.append(item)
    expected_permissions = {permission}
    if any(
        set(item.get("granteePermissions") or []) != expected_permissions
        or (inheritable is not None and item.get("isPermissionsInheritable") is not inheritable)
        for item in matches
    ) or len(matches) > 1:
        raise ReconcileError(
            f"Role {role_name} has a conflicting direct permission; remove the broader grant before retrying"
        )
    return len(matches) == 1


def assert_role_permissions_exact(
    api: AidpApi,
    role_key: str,
    role_name: str,
    expected: set[tuple[str, str, frozenset[str]]],
) -> None:
    actual: list[tuple[str, str, frozenset[str]]] = []
    for item in api.list_all(
        f"/roles/{role_key}/permissions", params={"permissionScope": "DIRECT"}
    ):
        details = item.get("permissionsWithResourceDetails") if isinstance(item, dict) else None
        if not isinstance(details, dict):
            raise ReconcileError(f"Role {role_name} returned an invalid permission record")
        actual.append(
            (
                str(details.get("resourceType") or "").upper(),
                str(details.get("resourceKey") or ""),
                frozenset(details.get("permissions") or []),
            )
        )
    if len(actual) != len(expected) or set(actual) != expected:
        raise ReconcileError(
            f"Role {role_name} has unexpected direct permissions; remove the broader grants before retrying"
        )


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


def load_oci_config(config_path: str, key_path: str) -> dict[str, Any]:
    import oci

    parser = configparser.ConfigParser()
    if not parser.read(config_path, encoding="utf-8") or "DEFAULT" not in parser:
        raise ReconcileError("OCI config is missing the DEFAULT profile")
    config = dict(parser["DEFAULT"])
    config["key_file"] = key_path
    required = ("tenancy", "user", "fingerprint")
    missing = [name for name in required if not config.get(name)]
    if missing:
        raise ReconcileError(f"OCI config is missing required fields: {', '.join(missing)}")
    try:
        oci.config.validate_config(config)
    except Exception as exc:
        raise ReconcileError("OCI config could not be validated with the supplied private key") from exc
    return config


def build_signer(config: dict[str, Any]) -> Any:
    import oci

    return oci.signer.Signer(
        tenancy=config["tenancy"],
        user=config["user"],
        fingerprint=config["fingerprint"],
        private_key_file_location=config["key_file"],
        pass_phrase=config.get("pass_phrase"),
    )


def ensure_object_prefixes(client: Any, namespace: str, bucket: str) -> list[str]:
    import oci

    events: list[str] = []
    for index, layer in enumerate(LAYERS, start=1):
        name = f"{index:02d}_{layer}/"
        try:
            client.head_object(namespace, bucket, name)
            created = False
        except oci.exceptions.ServiceError as exc:
            if exc.status != 404:
                raise ReconcileError(f"Object Storage prefix check failed with {exc.status}: {name}") from exc
            try:
                client.put_object(namespace, bucket, name, b"", content_type="application/x-directory")
            except oci.exceptions.ServiceError as put_exc:
                raise ReconcileError(f"Object Storage prefix creation failed with {put_exc.status}: {name}") from put_exc
            created = True
        events.append(f"Object Storage prefix {name} {'created' if created else 'reused'}")
    return events


def workspace_object(api: AidpApi, workspace_key: str, path: str) -> ApiResponse | None:
    try:
        return api.request(
            "GET",
            f"/workspaces/{workspace_key}/objects/{quote(path, safe='')}",
            headers={"Accept": "*/*"},
        )
    except ApiRequestError as exc:
        if exc.status_code == 404:
            return None
        raise


def workspace_object_key(response: ApiResponse | None, path: str) -> str:
    if response is None:
        return ""
    body = response.body if isinstance(response.body, dict) else {}
    key = response.headers.get("object-key") or body.get("key") or body.get("objectKey")
    if not key:
        raise ReconcileError(f"AIDP workspace object {path} has no object key")
    return str(key)


def ensure_workspace_folder(api: AidpApi, workspace_key: str, path: str) -> tuple[str, bool]:
    current = workspace_object(api, workspace_key, path)
    if current is not None:
        return workspace_object_key(current, path), False
    try:
        api.request(
            "POST",
            f"/workspaces/{workspace_key}/objects",
            data=b"",
            headers={
                "Accept": "*/*",
                "Content-Type": "application/octet-stream",
                "path": path,
                "type": "FOLDER",
                "is-overwrite": "false",
            },
        )
    except ApiRequestError as exc:
        if exc.status_code != 409:
            raise
    current = workspace_object(api, workspace_key, path)
    if current is None:
        raise ReconcileError(f"AIDP workspace folder {path} was not published")
    return workspace_object_key(current, path), True


def ensure_role(
    api: AidpApi,
    name: str,
    description: str,
    principal_type: str,
    principal_id: str,
) -> tuple[str, bool, bool]:
    role, created = ensure_resource(
        api,
        "/roles",
        "role",
        name,
        {"displayName": name, "description": description},
        {},
        filters={"displayName": name},
    )
    role_key = str(role["key"])
    member_added = ensure_action(
        api,
        "POST",
        f"/roles/{role_key}/actions/addMember",
        {"assignees": [{"type": principal_type, "target": principal_id}]},
        lambda: role_has_member(api, role_key, principal_type, principal_id),
    )
    return role_key, created, member_added


def ensure_role_permission(
    api: AidpApi,
    resource_path: str,
    assignment_key: str,
    role_name: str,
    permission: str,
    *,
    method: str = "POST",
    inheritable: bool | None = None,
) -> bool:
    assignment: dict[str, Any] = {
        "assignees": {"type": "ROLE", "targets": [role_name]},
        "permissions": [permission],
    }
    if inheritable is not None:
        assignment["isPermissionsInheritable"] = inheritable
    return ensure_action(
        api,
        method,
        f"{resource_path}/actions/managePermission",
        {assignment_key: assignment},
        lambda: permission_is_assigned(
            api,
            f"{resource_path}/permissions",
            role_name,
            permission,
            inheritable,
        ),
    )


def assert_fresh_catalog(
    api: AidpApi,
    catalog_key: str,
    namespace: str,
    bucket: str,
) -> tuple[int, int]:
    schemas = api.list_all("/schemas", params={"catalogKey": catalog_key})
    global_schemas = [
        item for item in schemas if item.get("displayName") in set(LAYERS)
    ]
    if global_schemas:
        names = ", ".join(sorted(str(item.get("displayName")) for item in global_schemas))
        raise ReconcileError(
            f"Fresh-only bootstrap found legacy global schemas: {names}; remove them explicitly before retrying"
        )
    volumes = api.list_all("/volumes", params={"catalogKey": catalog_key})
    external: list[dict[str, Any]] = []
    for volume in volumes:
        details = volume
        if volume.get("key") and not volume.get("storageLocation"):
            response = api.request("GET", f"/volumes/{volume['key']}").body
            details = response if isinstance(response, dict) else volume
        if str(details.get("volumeType") or "").upper() == "EXTERNAL":
            external.append(details)
    expected_locations = {
        f"oci://{bucket}@{namespace}/{index:02d}_{layer}/"
        for index, layer in enumerate(LAYERS, start=1)
    }
    overlapping = [
        item for item in external if item.get("storageLocation") in expected_locations
    ]
    if overlapping:
        names = ", ".join(sorted(str(item.get("displayName") or item.get("key")) for item in overlapping))
        raise ReconcileError(
            f"Fresh-only bootstrap found legacy external volumes overlapping medallion paths: {names}; no resources were deleted"
        )
    if external:
        raise ReconcileError(
            "Fresh-only bootstrap requires zero external volumes in aidp_lab; no resources were deleted"
        )
    return len(global_schemas), len(external)


def parse_public_key_output(text: str) -> tuple[str, str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    fingerprints = [line.split("=", 1)[1] for line in lines if line.startswith("FINGERPRINT=")]
    public_lines = [line for line in lines if not line.startswith("FINGERPRINT=")]
    if len(fingerprints) != 1:
        raise ReconcileError("Run Command returned an invalid public-key response")
    public_key = "\n".join(public_lines) + "\n"
    if "PRIVATE KEY" in public_key or not public_key.startswith("-----BEGIN PUBLIC KEY-----"):
        raise ReconcileError("Run Command did not return a public-only PEM")
    if not public_key.rstrip().endswith("-----END PUBLIC KEY-----"):
        raise ReconcileError("Run Command returned an incomplete public key")
    fingerprint = fingerprints[0].lower()
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){15}[0-9a-f]{2}", fingerprint):
        raise ReconcileError("Run Command returned an invalid API-key fingerprint")
    return public_key, fingerprint


def fetch_provisioner_public_key(
    client: Any,
    oci_module: Any,
    compartment_id: str,
    instance_id: str,
    *,
    attempts: int = 420,
) -> tuple[str, str]:
    models = oci_module.compute_instance_agent.models
    details = models.CreateInstanceAgentCommandDetails(
        compartment_id=compartment_id,
        execution_time_out_in_seconds=660,
        display_name="aidp-lab-provisioner-public-key",
        target=models.InstanceAgentCommandTarget(instance_id=instance_id),
        content=models.InstanceAgentCommandContent(
            source=models.InstanceAgentCommandSourceViaTextDetails(text=PUBLIC_KEY_SCRIPT),
            output=models.InstanceAgentCommandOutputViaTextDetails(),
        ),
    )
    command = client.create_instance_agent_command(details).data
    command_id = str(getattr(command, "id", "") or "")
    if not command_id:
        raise ReconcileError("Compute Run Command did not return a command OCID")
    terminal = {"FAILED", "TIMED_OUT", "CANCELED"}
    for _ in range(attempts):
        try:
            execution = client.get_instance_agent_command_execution(command_id, instance_id).data
        except oci_module.exceptions.ServiceError as exc:
            if exc.status == 404:
                time.sleep(5)
                continue
            raise
        state = str(getattr(execution, "lifecycle_state", "") or "").upper()
        if state == "SUCCEEDED":
            content = getattr(execution, "content", None)
            return parse_public_key_output(str(getattr(content, "text", "") or ""))
        if state in terminal:
            raise ReconcileError(f"Compute Run Command failed with state {state}")
        time.sleep(5)
    raise ReconcileError("Timed out waiting for the provisioner public key Run Command")


def rotate_provisioner_api_key(
    identity_client: Any,
    oci_module: Any,
    user_ocid: str,
    public_key: str,
    expected_fingerprint: str,
    *,
    attempts: int = 12,
) -> str:
    existing = oci_module.pagination.list_call_get_all_results(
        identity_client.list_api_keys, user_ocid
    ).data
    for item in existing:
        fingerprint = str(getattr(item, "fingerprint", "") or "")
        if fingerprint:
            identity_client.delete_api_key(user_ocid, fingerprint)
    uploaded = identity_client.upload_api_key(
        user_ocid,
        oci_module.identity.models.CreateApiKeyDetails(key=public_key),
    ).data
    uploaded_fingerprint = str(getattr(uploaded, "fingerprint", "") or "").lower()
    if uploaded_fingerprint != expected_fingerprint:
        raise ReconcileError("OCI returned a fingerprint that does not match the VM public key")
    for _ in range(attempts):
        current = oci_module.pagination.list_call_get_all_results(
            identity_client.list_api_keys, user_ocid
        ).data
        fingerprints = {
            str(getattr(item, "fingerprint", "") or "").lower() for item in current
        }
        if fingerprints == {expected_fingerprint}:
            return expected_fingerprint
        time.sleep(5)
    raise ReconcileError("Provisioner API-key rotation did not converge to exactly one key")


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
    namespace = str(outputs["objectstorage_namespace"])
    bucket = str(outputs["bucket_name"])
    global_schema_count, external_volume_count = assert_fresh_catalog(
        api, catalog_key, namespace, bucket
    )
    events.append("Fresh-only catalog verified: zero global schemas and zero external volumes")
    workspace_key = str(workspace["key"])
    shared_compute, compute_created = ensure_resource(
        api,
        f"/workspaces/{workspace_key}/clusters",
        "shared compute",
        SHARED_COMPUTE_NAME,
        {
            "type": "USER",
            "displayName": SHARED_COMPUTE_NAME,
            "description": "Quickstart-equivalent shared Spark compute for AIDP lab workflows",
            "driverConfig": {
                "driverShape": "amd.generic",
                "driverShapeConfig": {"ocpus": 2, "memoryInGBs": 32},
            },
            "workerConfig": {
                "workerShape": "amd.generic",
                "workerShapeConfig": {"ocpus": 2, "memoryInGBs": 32},
                "minWorkerCount": 1,
                "maxWorkerCount": 10,
            },
            "autoTerminationMinutes": 60,
            "clusterRuntimeConfig": {
                "type": "SPARK",
                "sparkVersion": "3.5.0",
                "sparkAdvancedConfigurations": {},
                "sparkEnvVariables": {},
                "initScripts": [],
            },
        },
        {},
        wait_for_active=True,
    )
    events.append(f"Shared compute {SHARED_COMPUTE_NAME} {'created' if compute_created else 'reused'}")
    compute_key = str(shared_compute["key"])
    root_object_key, root_created = ensure_workspace_folder(
        api, workspace_key, "/Workspace/lab-users"
    )
    events.append(
        f"Workspace root /Workspace/lab-users {'created' if root_created else 'reused'}"
    )

    role_specs = (
        (
            DEVELOPER_ROLE_NAME,
            "AIDP lab developer",
            str(outputs["developer_group_ocid"]),
        ),
        (
            PENDING_ROLE_NAME,
            "AIDP lab pending participant",
            str(outputs["pending_group_ocid"]),
        ),
        (
            PROVISIONER_ROLE_NAME,
            "AIDP lab technical provisioner",
            str(outputs["provisioner_group_ocid"]),
        ),
    )
    role_keys: dict[str, str] = {}
    for role_name, description, group_ocid in role_specs:
        role_key, role_created, member_added = ensure_role(
            api, role_name, description, "GROUP", group_ocid
        )
        role_keys[role_name] = role_key
        events.append(
            f"Role {role_name} {'created' if role_created else 'reused'}; "
            f"group {'added' if member_added else 'already assigned'}"
        )

    for role_name in (DEVELOPER_ROLE_NAME, PENDING_ROLE_NAME, PROVISIONER_ROLE_NAME):
        ensure_role_permission(
            api,
            f"/workspaces/{workspace_key}",
            "assignWorkspacePermissionDetails",
            role_name,
            "USER",
        )
    for role_name, permission in (
        (DEVELOPER_ROLE_NAME, "SELECT"),
        (PROVISIONER_ROLE_NAME, "ADMIN"),
    ):
        ensure_role_permission(
            api,
            f"/catalogs/{catalog_key}",
            "assignCatalogPermissionDetails",
            role_name,
            permission,
        )
    for role_name in (DEVELOPER_ROLE_NAME, PROVISIONER_ROLE_NAME):
        ensure_role_permission(
            api,
            f"/workspaces/{workspace_key}/clusters/{compute_key}",
            "assignClusterPermissionDetails",
            role_name,
            "USE",
        )
    ensure_role_permission(
        api,
        f"/workspaces/{workspace_key}/objects/{root_object_key}",
        "assignWorkspaceObjectPermissionDetails",
        PROVISIONER_ROLE_NAME,
        "ADMIN",
        inheritable=True,
    )
    expected_permissions = {
        DEVELOPER_ROLE_NAME: {
            ("WORKSPACE", workspace_key, frozenset({"USER"})),
            ("CATALOG", catalog_key, frozenset({"SELECT"})),
            ("CLUSTER", compute_key, frozenset({"USE"})),
        },
        PENDING_ROLE_NAME: {
            ("WORKSPACE", workspace_key, frozenset({"USER"})),
        },
        PROVISIONER_ROLE_NAME: {
            ("WORKSPACE", workspace_key, frozenset({"USER"})),
            ("CATALOG", catalog_key, frozenset({"ADMIN"})),
            ("CLUSTER", compute_key, frozenset({"USE"})),
            ("FOLDER", root_object_key, frozenset({"ADMIN"})),
        },
    }
    for role_name, _, group_ocid in role_specs:
        assert_role_members_exact(
            api, role_keys[role_name], role_name, "GROUP", group_ocid
        )
        assert_role_permissions_exact(
            api, role_keys[role_name], role_name, expected_permissions[role_name]
        )
    events.append("AIDP developer, pending, and provisioner RBAC verified")

    return (
        {
            "workspace_key": workspace_key,
            "shared_compute_key": compute_key,
            "shared_compute_name": SHARED_COMPUTE_NAME,
            "catalog_key": catalog_key,
            "catalog_name": CATALOG_NAME,
            "role_keys": role_keys,
            "root_object_key": root_object_key,
            "global_schema_count": global_schema_count,
            "external_volume_count": external_volume_count,
        },
        events,
    )


def workbench_url(outputs: dict[str, Any]) -> str:
    endpoint = str(outputs.get("aidp_web_socket_endpoint") or "").strip()
    tenancy = str(outputs.get("tenancy_name") or "").strip()
    domain = str(outputs.get("identity_domain_name") or "Default").strip()
    if not endpoint or not tenancy:
        return ""
    host = endpoint.split("://", 1)[-1].split("/", 1)[0]
    if not host:
        return ""
    if not host.endswith(".datalake.oci.oraclecloud.com"):
        host = f"{host}.datalake.oci.oraclecloud.com"
    return f"https://{host}#?tenant={tenancy}&domain={domain}"


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


def resolve_workbench_url(outputs: dict[str, Any], config: dict[str, Any], signer: Any) -> str:
    direct_url = workbench_url(outputs)
    if direct_url:
        return direct_url
    try:
        import oci

        platform = oci.ai_data_platform.AiDataPlatformClient(config, signer=signer).get_ai_data_platform(
            str(outputs["ai_data_platform_id"])
        ).data
        enriched_outputs = {**outputs, "aidp_web_socket_endpoint": getattr(platform, "web_socket_endpoint", "")}
        return workbench_url(enriched_outputs)
    except Exception:
        # ponytail: an endpoint can appear after the Workbench is active; Settings remains the admin fallback.
        return ""


def register_provisioner_api_key(
    oci_module: Any,
    config: dict[str, Any],
    signer: Any,
    outputs: dict[str, Any],
    region: str,
) -> str:
    run_config = {**config, "region": region}
    run_client = oci_module.compute_instance_agent.ComputeInstanceAgentClient(
        run_config, signer=signer
    )
    public_key, fingerprint = fetch_provisioner_public_key(
        run_client,
        oci_module,
        str(outputs["compartment_ocid"]),
        str(outputs["instance_id"]),
    )
    identity_config = {**config, "region": str(outputs["home_region"])}
    identity_client = oci_module.identity.IdentityClient(
        identity_config, signer=signer
    )
    return rotate_provisioner_api_key(
        identity_client,
        oci_module,
        str(outputs["provisioner_user_ocid"]),
        public_key,
        fingerprint,
    )


def build_success_result(
    context: dict[str, Any], reconciled: dict[str, Any], messages: list[str], aidp_url: str = ""
) -> dict[str, Any]:
    resources = {**reconciled, "aidp_workbench_url": aidp_url}
    summary = {
        "schema_version": 2,
        "deployment_id": context["deployment_id"],
        "source": context["source"],
        "resources": resources,
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
        "outputs": {
            "aidp_workbench_url": aidp_url,
            "aidp_catalog_name": str(reconciled.get("catalog_name") or CATALOG_NAME),
            "aidp_shared_compute_name": str(
                reconciled.get("shared_compute_name") or SHARED_COMPUTE_NAME
            ),
            "aidp_provisioner_ready": bool(reconciled.get("provisioner_ready")),
            "aidp_external_volume_count": int(
                reconciled.get("external_volume_count") or 0
            ),
        },
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
        oci_config = load_oci_config(config_path, key_path)
        signer = build_signer(oci_config)
        import oci

        object_storage = oci.object_storage.ObjectStorageClient(oci_config, signer=signer)
        messages = ensure_object_prefixes(
            object_storage, str(outputs["objectstorage_namespace"]), str(outputs["bucket_name"])
        )
        fingerprint = register_provisioner_api_key(
            oci, oci_config, signer, outputs, str(context["region"])
        )
        messages.append(
            f"Provisioner API key rotated and verified: {fingerprint}"
        )
        api = AidpApi(context["region"], outputs["ai_data_platform_id"], signer, context["deployment_id"])
        reconciled, reconcile_messages = reconcile(api, outputs)
        messages.extend(reconcile_messages)
        reconciled["provisioner_ready"] = True
        reconciled["provisioner_api_key_fingerprint"] = fingerprint
        aidp_url = resolve_workbench_url(outputs, oci_config, signer)
        if not aidp_url:
            raise ReconcileError("AIDP Workbench direct URL is not published yet")
        wait_for_application(str(outputs["application_url"]))
        messages.append("Registration application is healthy over HTTPS")
        write_result(output_path, build_success_result(context, reconciled, messages, aidp_url))
        return 0
    except (KeyError, OSError, ValueError, ReconcileError) as exc:
        write_result(output_path, {"events": [{"level": "error", "message": str(exc)}], "artifacts": [], "outputs": {}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
