"""Idempotent AIDP provisioning for isolated lab participants."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from oci._vendor import requests

from .config import Settings
from .industry_kits import INDUSTRIES, csv_samples as build_csv_samples
from .notebooks import (
    LAYER_PREFIXES,
    WORKSPACE_ROOT,
    layer_uri,
    participant_folder,
    participant_table_names,
    schema_name,
    user_notebooks as build_user_notebooks,
    workspace_participant_root,
    workspace_root,
)


API_VERSION = "20240831"
SHARED_COMPUTE_NAME = "aidp_lab_shared_compute"
CATALOG_NAME = "aidp_lab"
CONTROL_ROOT = f"{WORKSPACE_ROOT}/.control"
LEGACY_WORKSPACE_ROOT = "/Workspace/lab-users"


class AidpProvisionPending(Exception):
    def __init__(self, message: str, phase: str = "content") -> None:
        super().__init__(message)
        self.phase = phase


class AidpProvisionError(Exception):
    pass


class AidpProvisionConflict(Exception):
    pass


@dataclass(frozen=True, slots=True)
class UserMaterial:
    email: str
    industry: str
    participant_key: str
    workspace_path: str
    job_name: str


def participant_key(user_ocid: str) -> str:
    """Derive the stable, non-PII participant identifier from an OCI user OCID."""
    if not user_ocid.startswith("ocid1.user.") or any(character.isspace() for character in user_ocid):
        raise ValueError("A valid OCI user OCID is required")
    return f"u_{hashlib.sha256(user_ocid.encode('utf-8')).hexdigest()[:16]}"


class LocalAidpClient:
    """In-memory AIDP adapter for the Docker development and test profile."""

    def __init__(self, _: Settings) -> None:
        self.users: dict[str, UserMaterial] = {}
        self._reset_operations: dict[str, tuple[str, UserMaterial]] = {}
        # ponytail: process-local locks are sufficient for the single-process development adapter.
        self._locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        return None

    async def healthcheck(self) -> None:
        return None

    @staticmethod
    def _material(user_ocid: str, email: str, industry: str) -> UserMaterial:
        if industry not in INDUSTRIES:
            raise ValueError("Choose banking, telecommunications, retail, or healthcare")
        key = participant_key(user_ocid)
        build_csv_samples(industry, key)
        return UserMaterial(
            email,
            industry,
            key,
            workspace_root(email, industry),
            f"wf_{key}_{industry}_medallion",
        )

    async def provision_user(self, user_ocid: str, email: str, industry: str) -> UserMaterial:
        key = participant_key(user_ocid)
        async with self._locks.setdefault(key, asyncio.Lock()):
            existing = self.users.get(key)
            if existing is not None and existing.industry != industry:
                raise AidpProvisionConflict(
                    "This participant already selected another industry; delete and recreate the participant."
                )
            material = self._material(user_ocid, email, industry)
            self.users[key] = material
            return material

    async def reset_user(
        self,
        user_ocid: str,
        email: str,
        industry: str,
        operation_id: str,
    ) -> UserMaterial:
        key = participant_key(user_ocid)
        async with self._locks.setdefault(key, asyncio.Lock()):
            completed = self._reset_operations.get(key)
            if completed is not None and completed[0] == operation_id:
                if completed[1].industry != industry:
                    raise AidpProvisionConflict("This reset operation already targets another industry.")
                return completed[1]
            material = self._material(user_ocid, email, industry)
            self.users[key] = material
            self._reset_operations[key] = (operation_id, material)
            return material

    async def list_user_industries(self, user_ocids: list[str]) -> dict[str, str]:
        return {
            user_ocid: material.industry
            for user_ocid in user_ocids
            if (material := self.users.get(participant_key(user_ocid))) is not None
        }

    async def cleanup_user(self, user_ocid: str) -> None:
        key = participant_key(user_ocid)
        async with self._locks.setdefault(key, asyncio.Lock()):
            self.users.pop(key, None)
            self._reset_operations.pop(key, None)


class AidpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base = (
            f"https://datalake.{settings.aidp_region}.oci.oraclecloud.com/{API_VERSION}/"
            f"dataLakes/{settings.aidp_platform_id}"
        )
        import oci

        self._oci = oci
        config = oci.config.from_file(settings.oci_config_file, "DEFAULT")
        self.signer = oci.signer.Signer(
            tenancy=config["tenancy"],
            user=config["user"],
            fingerprint=config["fingerprint"],
            private_key_file_location=config["key_file"],
            pass_phrase=config.get("pass_phrase"),
        )
        self.object_storage = oci.object_storage.ObjectStorageClient(config)
        self.session = requests.Session()
        self._session_lock = threading.Lock()
        # ponytail: process-local locks serialize one participant; use a distributed lock if the API is replicated.
        self._locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        self.session.close()

    @staticmethod
    def _request_headers(
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        data: bytes | None,
        headers: dict[str, str] | None,
        retry_scope: str,
    ) -> dict[str, str]:
        request_headers = {"Accept": "application/json", **(headers or {})}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        if method.upper() != "POST":
            return request_headers
        content = (
            data
            if data is not None
            else json.dumps(payload or {}, sort_keys=True).encode("utf-8")
        )
        identity_headers = {
            key.casefold(): str(value)
            for key, value in request_headers.items()
            if key.casefold() != "opc-retry-token"
        }
        retry_identity = json.dumps(
            {
                "method": method.upper(),
                "path": path,
                "scope": retry_scope,
                "headers": identity_headers,
                "content_sha256": hashlib.sha256(content).hexdigest(),
            },
            sort_keys=True,
        )
        request_headers["opc-retry-token"] = str(
            uuid.uuid5(uuid.NAMESPACE_URL, retry_identity)
        )
        return request_headers

    @staticmethod
    def _response_body(response: Any) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.content

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        allow_not_found: bool = False,
        include_headers: bool = False,
        phase: str = "content",
        retry_scope: str = "",
    ) -> Any:
        request_headers = self._request_headers(
            method, path, payload, data, headers, retry_scope
        )
        try:
            with self._session_lock:
                response = self.session.request(
                    method,
                    f"{self.base}{path}",
                    auth=self.signer,
                    json=payload,
                    data=data,
                    headers=request_headers,
                    params=params,
                    timeout=(10, 60),
                )
        except requests.exceptions.RequestException as exc:
            raise AidpProvisionPending(
                "AIDP is still accepting the requested material. Retry shortly.", phase
            ) from exc
        if response.status_code in {408, 409, 429, 500, 502, 503, 504}:
            raise AidpProvisionPending("AIDP is still reconciling the requested material. Retry shortly.", phase)
        if response.status_code == 404 and allow_not_found:
            return (None, response.headers) if include_headers else None
        if response.status_code >= 400:
            raise AidpProvisionError(
                f"AIDP could not complete this operation ({response.status_code}). Check the AIDP policy and retry."
            )
        result = self._response_body(response)
        return (result, response.headers) if include_headers else result

    @staticmethod
    def _page_items(body: Any) -> list[dict[str, Any]]:
        if isinstance(body, list):
            return [item for item in body if isinstance(item, dict)]
        if isinstance(body, dict):
            values = body.get("items") or body.get("Items") or []
            return [item for item in values if isinstance(item, dict)]
        return []

    def _list(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        phase: str = "content",
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            query = {"limit": "100", **(params or {})}
            if page:
                query["page"] = page
            body, response_headers = self._request(
                "GET", path, params=query, include_headers=True, phase=phase
            )
            items.extend(self._page_items(body))
            page = response_headers.get("opc-next-page") or response_headers.get("Opc-Next-Page")
            if not page:
                return items

    def _workspace(self) -> dict[str, Any]:
        workspaces = [
            item
            for item in self._list("/workspaces", phase="workspace")
            if item.get("displayName") == self.settings.aidp_workspace_name
        ]
        if len(workspaces) != 1:
            raise AidpProvisionPending("The default AIDP workspace is not ready yet. Retry shortly.", "workspace")
        return self._require_operational_state(
            workspaces[0], {"ACTIVE"}, "workspace", "workspace"
        )

    def _catalog(self) -> dict[str, Any]:
        catalogs = [
            item
            for item in self._list("/catalogs", phase="schemas")
            if (item.get("displayName") or item.get("name")) == CATALOG_NAME
        ]
        if len(catalogs) != 1:
            raise AidpProvisionPending("The aidp_lab catalog is not ready yet. Retry shortly.", "schemas")
        return self._require_operational_state(
            catalogs[0], {"ACTIVE"}, "catalog", "schemas"
        )

    def _shared_compute(self, workspace_key: str) -> dict[str, Any]:
        clusters = [
            item
            for item in self._list(f"/workspaces/{workspace_key}/clusters", phase="workspace")
            if item.get("displayName") == SHARED_COMPUTE_NAME
        ]
        if len(clusters) != 1:
            raise AidpProvisionPending("The shared AIDP compute is not ready yet. Retry shortly.", "workspace")
        # AIDP auto-starts an idle-timeout STOPPED cluster when a notebook or workflow uses it.
        return self._require_operational_state(
            clusters[0], {"ACTIVE", "STOPPED"}, "compute", "workspace"
        )

    @staticmethod
    def _require_operational_state(
        resource: dict[str, Any],
        allowed: set[str],
        kind: str,
        phase: str,
    ) -> dict[str, Any]:
        state = str(resource.get("lifecycleState") or resource.get("state") or "").upper()
        if state in allowed:
            return resource
        if state in {"FAILED", "DELETING", "DELETED", "DELETE_FAILED", "CANCELED", "CANCELLED"} or state.endswith("FAILED"):
            raise AidpProvisionError(f"The AIDP {kind} is in terminal state {state or 'UNKNOWN'}.")
        raise AidpProvisionPending(f"The AIDP {kind} is not operational yet. Retry shortly.", phase)

    def _workspace_object(
        self,
        workspace_key: str,
        path: str,
        *,
        phase: str,
    ) -> tuple[Any, dict[str, Any]]:
        body, headers = self._request(
            "GET",
            f"/workspaces/{workspace_key}/objects/{quote(path, safe='')}",
            headers={"Accept": "*/*"},
            allow_not_found=True,
            include_headers=True,
            phase=phase,
        )
        return body, headers

    @staticmethod
    def _workspace_object_exists(body: Any, headers: dict[str, Any]) -> bool:
        return body is not None or any(
            str(value)
            for name, value in headers.items()
            if name.casefold() in {"object-key", "object-type", "type"}
        )

    @staticmethod
    def _workspace_object_type(body: Any, headers: dict[str, Any]) -> str:
        for name, value in headers.items():
            if name.casefold() in {"object-type", "type"}:
                return str(value).upper()
        if isinstance(body, dict) and body.get("type") in {"FILE", "FOLDER"}:
            return str(body["type"])
        return ""

    @staticmethod
    def _content_matches(body: Any, expected: bytes) -> bool:
        if isinstance(body, bytes):
            actual = body
        elif isinstance(body, str):
            actual = body.encode("utf-8")
        elif isinstance(body, (dict, list)):
            actual = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        else:
            return False
        return actual == expected

    @classmethod
    def _notebook_matches(cls, actual: Any, expected: Any) -> bool:
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(
                key in actual and cls._notebook_matches(actual[key], value)
                for key, value in expected.items()
            )
        if isinstance(expected, list):
            if isinstance(actual, str) and all(
                isinstance(item, str) for item in expected
            ):
                return "".join(expected) == actual
            return (
                isinstance(actual, list)
                and len(actual) == len(expected)
                and all(
                    cls._notebook_matches(actual_item, expected_item)
                    for actual_item, expected_item in zip(actual, expected, strict=True)
                )
            )
        return actual == expected

    def _export_notebook(
        self,
        workspace_key: str,
        path: str,
    ) -> dict[str, Any] | None:
        exported = self._request(
            "POST",
            f"/workspaces/{workspace_key}/notebook/api/actions/export/contents/{quote(path, safe='')}",
            payload={"format": "ipynb"},
            allow_not_found=True,
            phase="content",
            retry_scope=path,
        )
        if exported is None:
            return None
        content = exported.get("content") if isinstance(exported, dict) else None
        if not isinstance(content, dict):
            raise AidpProvisionPending(
                f"AIDP has not exported notebook {path} yet.", "content"
            )
        return content

    def _notebook_state(
        self,
        workspace_key: str,
        path: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        object_body, object_headers = self._workspace_object(
            workspace_key, path, phase="content"
        )
        exists = self._workspace_object_exists(object_body, object_headers)
        if not exists:
            return False, None
        if self._workspace_object_type(object_body, object_headers) not in {
            "",
            "NOTEBOOK",
        }:
            raise AidpProvisionError(f"Workspace path {path} exists but is not a notebook.")
        return True, self._export_notebook(workspace_key, path)

    def _ensure_folder(self, workspace_key: str, path: str) -> bool:
        body, headers = self._workspace_object(workspace_key, path, phase="workspace")
        if self._workspace_object_exists(body, headers):
            if self._workspace_object_type(body, headers) not in {"", "FOLDER"}:
                raise AidpProvisionError(f"Workspace path {path} exists but is not a folder.")
            return False
        self._request(
            "POST",
            f"/workspaces/{workspace_key}/objects",
            data=b"",
            headers={
                "Accept": "*/*",
                "path": path,
                "type": "FOLDER",
                "is-overwrite": "false",
                "Content-Type": "application/octet-stream",
            },
            phase="workspace",
        )
        body, headers = self._workspace_object(workspace_key, path, phase="workspace")
        if not self._workspace_object_exists(body, headers):
            raise AidpProvisionPending(f"AIDP has not published workspace folder {path} yet.", "workspace")
        return True

    def _upload_file(
        self,
        workspace_key: str,
        path: str,
        content: bytes,
        *,
        repair_drift: bool = True,
    ) -> bool:
        body, headers = self._workspace_object(workspace_key, path, phase="content")
        exists = self._workspace_object_exists(body, headers)
        if exists and self._workspace_object_type(body, headers) == "FOLDER":
            raise AidpProvisionError(f"Workspace path {path} exists but is not a file.")
        if exists and (self._content_matches(body, content) or not repair_drift):
            return False
        self._request(
            "POST",
            f"/workspaces/{workspace_key}/objects",
            data=content,
            headers={
                "Accept": "*/*",
                "path": path,
                "type": "FILE",
                "is-overwrite": str(exists).lower(),
                "Content-Type": "application/octet-stream",
            },
            phase="content",
        )
        body, _ = self._workspace_object(workspace_key, path, phase="content")
        if not self._content_matches(body, content):
            raise AidpProvisionPending(f"AIDP has not published workspace file {path} yet.", "content")
        return True

    def _workspace_object_key(self, workspace_key: str, path: str) -> str:
        body, headers = self._workspace_object(workspace_key, path, phase="permissions")
        normalized = {str(name).casefold(): value for name, value in headers.items()}
        object_key = str(normalized.get("object-key") or "")
        if object_key:
            return object_key
        object_path = str(
            normalized.get("folder")
            or normalized.get("path")
            or (body.get("path") if isinstance(body, dict) else "")
            or ""
        )
        if object_path and object_path != path:
            raise AidpProvisionError(
                f"AIDP returned a mismatched workspace object path for {path}."
            )
        if not object_path:
            raise AidpProvisionPending(f"AIDP has not published workspace object {path} yet.", "permissions")
        return object_path

    def _upload_notebook(
        self,
        workspace_key: str,
        path: str,
        notebook: dict[str, Any],
        *,
        repair_drift: bool = True,
    ) -> bool:
        content_path = f"/workspaces/{workspace_key}/notebook/api/contents/{quote(path, safe='')}"
        exists, current_content = self._notebook_state(workspace_key, path)
        if current_content is not None and (
            self._notebook_matches(current_content, notebook) or not repair_drift
        ):
            return False
        if exists and current_content is None and not repair_drift:
            raise AidpProvisionPending(
                f"AIDP has not published readable notebook {path} yet.", "content"
            )
        if not exists:
            parent = path.rsplit("/", 1)[0]
            created = self._request(
                "POST",
                f"/workspaces/{workspace_key}/notebook/api/contents/{quote(parent, safe='')}",
                payload={"copy_from": None, "ext": ".ipynb", "type": "notebook"},
                phase="content",
                retry_scope=path,
            )
            created_path = str((created or {}).get("path") or "")
            if not created_path:
                raise AidpProvisionPending("AIDP has not published the notebook path yet.", "content")
            self._request(
                "PATCH",
                f"/workspaces/{workspace_key}/notebook/api/contents/{quote(created_path, safe='')}",
                payload={"path": path},
                phase="content",
            )
        self._request(
            "PUT",
            content_path,
            payload={
                "name": path.rsplit("/", 1)[-1],
                "path": path,
                "type": "notebook",
                "content": notebook,
                "format": "json",
            },
            phase="content",
        )
        current_content = self._export_notebook(workspace_key, path)
        if current_content is None or not self._notebook_matches(current_content, notebook):
            raise AidpProvisionPending(f"AIDP has not published notebook {path} yet.", "content")
        return True

    @staticmethod
    def _control_manifest_path(key: str) -> str:
        return f"{CONTROL_ROOT}/{key}.json"

    @staticmethod
    def _legacy_control_manifest_path(key: str) -> str:
        return f"{LEGACY_WORKSPACE_ROOT}/.control/{key}.json"

    def _workspace_json(
        self,
        workspace_key: str,
        path: str,
        invalid_message: str,
    ) -> dict[str, Any] | None:
        body, headers = self._workspace_object(workspace_key, path, phase="workspace")
        if not self._workspace_object_exists(body, headers):
            return None
        if self._workspace_object_type(body, headers) == "FOLDER":
            raise AidpProvisionError(invalid_message)
        try:
            if isinstance(body, dict):
                payload = body
            elif isinstance(body, bytes):
                payload = json.loads(body.decode("utf-8"))
            elif isinstance(body, str):
                payload = json.loads(body)
            else:
                raise TypeError("unsupported control manifest body")
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AidpProvisionError(invalid_message) from exc
        if not isinstance(payload, dict):
            raise AidpProvisionError(invalid_message)
        return payload

    def _manifest(self, workspace_key: str, key: str) -> dict[str, Any] | None:
        return self._workspace_json(
            workspace_key,
            self._control_manifest_path(key),
            "The participant control manifest is invalid; delete the participant before retrying.",
        )

    @staticmethod
    def _manifest_workspace_path(manifest: dict[str, Any], key: str) -> str:
        workspace_path = str(manifest.get("workspace_path") or "")
        relative_path = workspace_path.removeprefix(f"{WORKSPACE_ROOT}/")
        parts = relative_path.split("/")
        if (
            manifest.get("layout_version") != 2
            or manifest.get("participant_key") != key
            or not workspace_path.startswith(f"{WORKSPACE_ROOT}/")
            or len(parts) != 2
            or parts[0] in {"", ".control"}
            or parts[1] not in INDUSTRIES
            or manifest.get("industry") != parts[1]
        ):
            raise AidpProvisionError(
                "The participant control manifest does not contain an exact workspace path; cleanup stopped."
            )
        return workspace_path

    def _write_manifest(
        self,
        workspace_key: str,
        key: str,
        manifest: dict[str, Any],
    ) -> None:
        content = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            self._upload_file(
                workspace_key,
                self._control_manifest_path(key),
                content,
                repair_drift=True,
            )
        except AidpProvisionPending as exc:
            reset = manifest.get("reset")
            reset_phase = str(reset.get("phase") or "") if isinstance(reset, dict) else ""
            phase = "cleanup" if reset_phase == "cleanup" else str(manifest.get("phase") or "content")
            if phase not in {"cleanup", "workspace", "schemas", "content", "permissions"}:
                phase = "permissions"
            raise AidpProvisionPending(
                "AIDP is still accepting the participant control manifest.", phase
            ) from exc

    def _ensure_manifest(
        self,
        workspace_key: str,
        key: str,
        email: str,
        industry: str,
    ) -> dict[str, Any]:
        root = workspace_root(email, industry)
        existing = self._manifest(workspace_key, key)
        if existing is not None:
            self._manifest_workspace_path(existing, key)
            if existing.get("industry") != industry or existing.get("workspace_path") != root:
                raise AidpProvisionConflict(
                    "This participant already has a different lab layout or industry; reset the participant environment."
                )
        if existing is None:
            existing = {
                "layout_version": 2,
                "participant_key": key,
                "industry": industry,
                "workspace_path": root,
                "phase": "workspace",
            }
            self._write_manifest(workspace_key, key, existing)
        return existing

    def _advance_manifest(
        self,
        workspace_key: str,
        manifest: dict[str, Any],
        phase: str,
    ) -> None:
        if manifest.get("phase") == phase:
            return
        manifest["phase"] = phase
        self._write_manifest(
            workspace_key,
            str(manifest["participant_key"]),
            manifest,
        )

    @staticmethod
    def _resource_name(resource: dict[str, Any]) -> str:
        return str(resource.get("displayName") or resource.get("name") or "")

    def _ensure_schema(
        self,
        catalog_key: str,
        layer: str,
    ) -> tuple[dict[str, Any], bool]:
        name = schema_name(layer)

        def matches() -> list[dict[str, Any]]:
            return [
                item
                for item in self._list(
                    "/schemas", params={"catalogKey": catalog_key}, phase="schemas"
                )
                if self._resource_name(item) == name
            ]

        existing = matches()
        if len(existing) > 1:
            raise AidpProvisionError(f"AIDP has duplicate schemas named {name}.")
        if existing:
            state = str(existing[0].get("lifecycleState") or "ACTIVE").upper()
            if state != "ACTIVE":
                raise AidpProvisionPending(
                    f"AIDP schema {name} is still {state.lower()}.", "schemas"
                )
            return existing[0], False
        self._request(
            "POST",
            "/schemas",
            payload={
                "displayName": name,
                "description": f"Shared collaborative {layer.title()} schema for the AIDP lab",
                "catalogName": CATALOG_NAME,
            },
            phase="schemas",
        )
        published = matches()
        if len(published) > 1:
            raise AidpProvisionError(f"AIDP has duplicate schemas named {name}.")
        if len(published) != 1 or not published[0].get("key"):
            raise AidpProvisionPending(f"AIDP has not published schema {name} yet.", "schemas")
        state = str(published[0].get("lifecycleState") or "ACTIVE").upper()
        if state != "ACTIVE":
            raise AidpProvisionPending(
                f"AIDP schema {name} is still {state.lower()}.", "schemas"
            )
        return published[0], True

    def _ensure_catalog_contract(
        self,
        catalog_key: str,
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        schemas: dict[str, dict[str, Any]] = {}
        changed = False
        for layer in LAYER_PREFIXES:
            schemas[layer], created = self._ensure_schema(catalog_key, layer)
            changed = changed or created
        return schemas, changed

    @staticmethod
    def _job_tasks(
        root: str,
        compute_key: str,
        notebook_names: list[str],
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "NOTEBOOK_TASK",
                "taskKey": f"stage_{index}",
                "dependsOn": (
                    [] if index == 1 else [{"taskKey": f"stage_{index - 1}"}]
                ),
                "runIf": "ALL_SUCCESS",
                "maxRetries": 0,
                "isRetryOnTimeout": False,
                "notebookPath": f"{root}/{notebook_name}",
                "cluster": {"clusterKey": compute_key},
                "parameters": [],
            }
            for index, notebook_name in enumerate(notebook_names, start=1)
        ]

    @staticmethod
    def _job_task_matches(
        actual: Any,
        expected: dict[str, Any],
        compute_key: str,
    ) -> bool:
        return bool(
            isinstance(actual, dict)
            and actual.get("type") == "NOTEBOOK_TASK"
            and actual.get("taskKey") == expected["taskKey"]
            and isinstance(actual.get("dependsOn"), list)
            and [
                dependency.get("taskKey")
                if isinstance(dependency, dict)
                else None
                for dependency in actual["dependsOn"]
            ]
            == [dependency["taskKey"] for dependency in expected["dependsOn"]]
            and actual.get("runIf") == "ALL_SUCCESS"
            and actual.get("notebookPath") == expected["notebookPath"]
            and (actual.get("cluster") or {}).get("clusterKey") == compute_key
        )

    @classmethod
    def _job_tasks_match(
        cls,
        actual_tasks: Any,
        expected_tasks: list[dict[str, Any]],
        compute_key: str,
    ) -> bool:
        if not isinstance(actual_tasks, list) or len(actual_tasks) != len(expected_tasks):
            return False
        return len(actual_tasks) == 4 and all(
            cls._job_task_matches(actual, expected, compute_key)
            for actual, expected in zip(actual_tasks, expected_tasks, strict=True)
        )

    @staticmethod
    def _job_compute_matches(clusters: Any, compute_key: str) -> bool:
        if not isinstance(clusters, list) or len(clusters) != 1:
            return False
        cluster = clusters[0]
        return isinstance(cluster, dict) and cluster.get("clusterKey") == compute_key

    def _job_contract_is_visible(
        self,
        details: Any,
        payload: dict[str, Any],
        compute_key: str,
    ) -> bool:
        if not isinstance(details, dict):
            return False
        return bool(
            self._resource_name(details) == payload["name"]
            and details.get("path") == payload["path"]
            and self._job_tasks_match(details.get("tasks"), payload["tasks"], compute_key)
            and self._job_compute_matches(details.get("jobClusters"), compute_key)
        )

    def _job_key(self, workspace_key: str, job_name: str) -> str:
        jobs = [
            item
            for item in self._list(
                f"/workspaces/{workspace_key}/jobs", phase="content"
            )
            if self._resource_name(item) == job_name
        ]
        if len(jobs) > 1:
            raise AidpProvisionError(f"AIDP has duplicate jobs named {job_name}.")
        return str((jobs[0] if jobs else {}).get("key") or "")

    def _create_job(self, workspace_key: str, payload: dict[str, Any]) -> str:
        created = self._request(
            "POST",
            f"/workspaces/{workspace_key}/jobs",
            payload={
                name: payload[name]
                for name in ("name", "path", "description", "maxConcurrentRuns")
            },
            phase="content",
        )
        job_key = str((created or {}).get("key") or "")
        if not job_key:
            raise AidpProvisionPending(
                "AIDP has not published the participant workflow yet.", "content"
            )
        return job_key

    def _publish_job(
        self,
        workspace_key: str,
        job_key: str,
        payload: dict[str, Any],
        compute_key: str,
    ) -> None:
        self._request(
            "PUT",
            f"/workspaces/{workspace_key}/jobs/{job_key}",
            payload=payload,
            phase="content",
        )
        details = self._request(
            "GET",
            f"/workspaces/{workspace_key}/jobs/{job_key}",
            allow_not_found=True,
            phase="content",
        )
        if not self._job_contract_is_visible(details, payload, compute_key):
            raise AidpProvisionPending(
                "AIDP has not published the complete participant workflow yet.", "content"
            )

    def _ensure_job(
        self,
        workspace_key: str,
        compute_key: str,
        key: str,
        industry: str,
        root: str,
        notebooks: dict[str, dict[str, Any]],
        *,
        repair_drift: bool = True,
    ) -> tuple[str, str, bool]:
        job_name = f"wf_{key}_{industry}_medallion"
        tasks = self._job_tasks(root, compute_key, list(notebooks))
        payload = {
            "name": job_name,
            "path": root,
            "description": f"{industry.title()} medallion tutorial for {key}",
            "maxConcurrentRuns": 1,
            "jobClusters": [{"clusterKey": compute_key}],
            "tasks": tasks,
        }
        job_key = self._job_key(workspace_key, job_name)
        if job_key:
            details = self._request(
                "GET",
                f"/workspaces/{workspace_key}/jobs/{job_key}",
                allow_not_found=True,
                phase="content",
            )
            if self._job_contract_is_visible(details, payload, compute_key):
                return job_name, job_key, False
            if details is None:
                raise AidpProvisionPending(
                    "AIDP has not published the participant workflow yet.", "content"
                )
            if not repair_drift:
                return job_name, job_key, False
        if not job_key:
            job_key = self._create_job(workspace_key, payload)
        self._publish_job(workspace_key, job_key, payload, compute_key)
        return job_name, job_key, True

    @staticmethod
    def _permission_grantee(item: dict[str, Any]) -> tuple[str, str]:
        grantee = item.get("grantee")
        if isinstance(grantee, dict):
            target = grantee.get("target")
            grantee_type = grantee.get("type")
        else:
            target = grantee
            grantee_type = None
        return (
            str(target or item.get("granteeName") or ""),
            str(grantee_type or item.get("granteeType") or "").upper(),
        )

    @staticmethod
    def _permission_values(item: dict[str, Any]) -> set[str]:
        return set(item.get("granteePermissions") or item.get("permissions") or [])

    @classmethod
    def _permission_matches(
        cls,
        item: dict[str, Any],
        user_ocid: str,
        permission: str,
        inheritable: bool | None = None,
    ) -> bool:
        grantee_value, grantee_type = cls._permission_grantee(item)
        return (
            grantee_value == user_ocid
            and grantee_type == "USER"
            and permission in cls._permission_values(item)
            and (inheritable is None or item.get("isPermissionsInheritable") is inheritable)
        )

    @classmethod
    def _permission_is_exact(
        cls,
        item: dict[str, Any],
        user_ocid: str,
        permission: str,
        inheritable: bool | None,
    ) -> bool:
        return cls._permission_matches(
            item, user_ocid, permission, inheritable
        ) and cls._permission_values(item) == {permission}

    @classmethod
    def _assert_no_permission_conflict(
        cls,
        items: list[dict[str, Any]],
        user_ocid: str,
        permission: str,
        inheritable: bool | None,
    ) -> None:
        if any(
            cls._permission_grantee(item) == (user_ocid, "USER")
            and not cls._permission_is_exact(
                item, user_ocid, permission, inheritable
            )
            for item in items
        ):
            raise AidpProvisionError(
                "AIDP found a conflicting direct permission for this participant; "
                "an administrator must remove the broader grant before retrying."
            )

    def _ensure_permission(
        self,
        resource_path: str,
        assignment_key: str,
        user_ocid: str,
        permission: str,
        *,
        inheritable: bool | None = None,
    ) -> bool:
        permissions_path = f"{resource_path}/permissions"
        current = self._list(permissions_path, phase="permissions")
        self._assert_no_permission_conflict(
            current, user_ocid, permission, inheritable
        )
        if any(
            self._permission_is_exact(item, user_ocid, permission, inheritable)
            for item in current
        ):
            return False
        assignment: dict[str, Any] = {
            "assignees": {"type": "USER", "targets": [user_ocid]},
            "permissions": [permission],
        }
        if inheritable is not None:
            assignment["isPermissionsInheritable"] = inheritable
        self._request(
            "POST",
            f"{resource_path}/actions/managePermission",
            payload={assignment_key: assignment},
            phase="permissions",
        )
        current = self._list(permissions_path, phase="permissions")
        self._assert_no_permission_conflict(
            current, user_ocid, permission, inheritable
        )
        if not any(
            self._permission_is_exact(item, user_ocid, permission, inheritable)
            for item in current
        ):
            raise AidpProvisionPending("AIDP has not applied the participant permission yet.", "permissions")
        return True

    def _ensure_permissions(
        self,
        workspace_key: str,
        user_ocid: str,
        participant_root: str,
        job_key: str,
    ) -> bool:
        root_key = self._workspace_object_key(workspace_key, WORKSPACE_ROOT)
        participant_object_key = self._workspace_object_key(workspace_key, participant_root)
        changed = self._ensure_permission(
            f"/workspaces/{workspace_key}/objects/{quote(root_key, safe='')}",
            "assignWorkspaceObjectPermissionDetails",
            user_ocid,
            "READ",
            inheritable=False,
        )
        changed = self._ensure_permission(
            f"/workspaces/{workspace_key}/objects/{quote(participant_object_key, safe='')}",
            "assignWorkspaceObjectPermissionDetails",
            user_ocid,
            "ADMIN",
            inheritable=True,
        ) or changed
        changed = self._ensure_permission(
            f"/workspaces/{workspace_key}/jobs/{job_key}",
            "assignJobPermissionDetails",
            user_ocid,
            "MANAGE",
        ) or changed
        return changed

    def _ensure_workspace_layout(
        self,
        workspace_key: str,
        paths: tuple[str, ...],
    ) -> bool:
        changed = False
        for path in paths:
            changed = self._ensure_folder(workspace_key, path) or changed
        return changed

    def _pending_after_change(
        self,
        changed: bool,
        was_active: bool,
        workspace_key: str,
        manifest: dict[str, Any],
        next_phase: str,
        message: str,
    ) -> None:
        if not changed:
            return
        if not was_active:
            self._advance_manifest(workspace_key, manifest, next_phase)
        raise AidpProvisionPending(message, next_phase)

    def _ensure_participant_content(
        self,
        workspace_key: str,
        compute_key: str,
        key: str,
        email: str,
        industry: str,
        root: str,
        repair_drift: bool,
    ) -> tuple[str, str, bool]:
        content_changed = self._upload_file(
            workspace_key,
            f"{root}/lab-manifest.json",
            json.dumps(
                {"participant_key": key, "industry": industry},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            repair_drift=repair_drift,
        )
        for name, content in build_csv_samples(industry, key).items():
            content_changed = self._upload_file(
                workspace_key,
                f"{root}/source/{name}",
                content.encode("utf-8"),
                repair_drift=repair_drift,
            ) or content_changed
        notebooks = build_user_notebooks(
            industry,
            key,
            email,
            self.settings.bucket_name,
            self.settings.objectstorage_namespace,
        )
        for name, notebook in notebooks.items():
            content_changed = self._upload_notebook(
                workspace_key,
                f"{root}/{name}",
                notebook,
                repair_drift=repair_drift,
            ) or content_changed
        job_name, job_key, job_changed = self._ensure_job(
            workspace_key,
            compute_key,
            key,
            industry,
            root,
            notebooks,
            repair_drift=repair_drift,
        )
        return job_name, job_key, content_changed or job_changed

    def _provision_user(self, user_ocid: str, email: str, industry: str) -> UserMaterial:
        if industry not in INDUSTRIES:
            raise ValueError("Choose banking, telecommunications, retail, or healthcare")
        key = participant_key(user_ocid)
        participant_folder(email)
        workspace_key = str(self._workspace()["key"])
        workspace_changed = self._ensure_workspace_layout(
            workspace_key,
            (WORKSPACE_ROOT, CONTROL_ROOT),
        )
        manifest = self._ensure_manifest(workspace_key, key, email, industry)
        previous_phase = str(manifest.get("phase") or "workspace")
        was_active = previous_phase == "active"
        repair_drift = not was_active
        participant_root = workspace_participant_root(email)
        root = workspace_root(email, industry)
        job_name = f"wf_{key}_{industry}_medallion"

        compute_key = str(self._shared_compute(workspace_key)["key"])
        workspace_changed = self._ensure_workspace_layout(
            workspace_key,
            (participant_root, root, f"{root}/source"),
        ) or workspace_changed
        self._pending_after_change(
            workspace_changed,
            was_active,
            workspace_key,
            manifest,
            "schemas",
            "Participant workspace is ready; schemas are next.",
        )

        catalog_key = str(self._catalog()["key"])
        _schemas, schemas_changed = self._ensure_catalog_contract(catalog_key)
        self._pending_after_change(
            schemas_changed,
            was_active,
            workspace_key,
            manifest,
            "content",
            "Participant schemas are ready; content is next.",
        )

        job_name, job_key, content_changed = self._ensure_participant_content(
            workspace_key, compute_key, key, email, industry, root, repair_drift
        )
        self._pending_after_change(
            content_changed,
            was_active,
            workspace_key,
            manifest,
            "permissions",
            "Participant content is ready; permissions are next.",
        )

        permissions_changed = self._ensure_permissions(
            workspace_key,
            user_ocid,
            participant_root,
            job_key,
        )
        if permissions_changed and was_active:
            raise AidpProvisionPending(
                "Participant permissions were repaired; final verification is next.",
                "permissions",
            )
        self._advance_manifest(workspace_key, manifest, "active")
        return UserMaterial(email, industry, key, root, job_name)


    async def provision_user(self, user_ocid: str, email: str, industry: str) -> UserMaterial:
        key = participant_key(user_ocid)
        async with self._locks.setdefault(key, asyncio.Lock()):
            return await asyncio.to_thread(self._provision_user, user_ocid, email, industry)

    @staticmethod
    def _reset_record(
        manifest: dict[str, Any],
        operation_id: str,
        industry: str,
        target_root: str,
    ) -> dict[str, Any]:
        reset = manifest.get("reset")
        if not isinstance(reset, dict):
            raise AidpProvisionError("The participant reset journal is invalid; reset stopped.")
        if reset.get("operation_id") != operation_id:
            raise AidpProvisionConflict("Another AIDP reset is already in progress for this participant.")
        if reset.get("target_industry") != industry or reset.get("target_workspace_path") != target_root:
            raise AidpProvisionConflict("This reset operation already targets another industry.")
        return reset

    def _ensure_reset_manifest(
        self,
        workspace_key: str,
        key: str,
        email: str,
        industry: str,
        operation_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        target_root = workspace_root(email, industry)
        manifest = self._manifest(workspace_key, key)
        if manifest is None:
            manifest = {
                "layout_version": 2,
                "participant_key": key,
                "industry": industry,
                "workspace_path": target_root,
                "phase": "workspace",
            }
        else:
            self._manifest_workspace_path(manifest, key)
        existing = manifest.get("reset")
        if isinstance(existing, dict) and existing.get("operation_id") == operation_id:
            return manifest, self._reset_record(manifest, operation_id, industry, target_root)
        if isinstance(existing, dict) and existing.get("phase") in {"cleanup", "provision"}:
            raise AidpProvisionConflict("Another AIDP reset is already in progress for this participant.")
        if existing is not None and not isinstance(existing, dict):
            raise AidpProvisionError("The participant reset journal is invalid; reset stopped.")
        reset = {
            "operation_id": operation_id,
            "target_industry": industry,
            "target_workspace_path": target_root,
            "phase": "cleanup",
        }
        manifest["reset"] = reset
        self._write_manifest(workspace_key, key, manifest)
        return manifest, reset

    def _reset_user(
        self,
        user_ocid: str,
        email: str,
        industry: str,
        operation_id: str,
    ) -> UserMaterial:
        if industry not in INDUSTRIES:
            raise ValueError("Choose banking, telecommunications, retail, or healthcare")
        key = participant_key(user_ocid)
        participant_folder(email)
        target_root = workspace_root(email, industry)
        workspace_key = str(self._workspace()["key"])
        self._ensure_workspace_layout(workspace_key, (WORKSPACE_ROOT, CONTROL_ROOT))
        manifest, reset = self._ensure_reset_manifest(
            workspace_key, key, email, industry, operation_id
        )
        phase = str(reset.get("phase") or "")
        if phase == "cleanup":
            try:
                self._cleanup_user(key, preserve_manifest=True)
                self._delete_workspace_path(
                    workspace_key,
                    f"{LEGACY_WORKSPACE_ROOT}/{participant_folder(email)}",
                    "Legacy email-named workspace deletion is still in progress.",
                )
            except AidpProvisionPending as exc:
                raise AidpProvisionPending(str(exc), "cleanup") from exc
            manifest = self._manifest(workspace_key, key)
            if manifest is None:
                raise AidpProvisionError("The participant reset journal disappeared during cleanup.")
            reset = self._reset_record(manifest, operation_id, industry, target_root)
            manifest.update(
                industry=industry,
                workspace_path=target_root,
                phase="workspace",
            )
            reset["phase"] = "provision"
            self._write_manifest(workspace_key, key, manifest)
        elif phase not in {"provision", "complete"}:
            raise AidpProvisionError("The participant reset journal has an unsupported phase.")

        material = self._provision_user(user_ocid, email, industry)
        if phase != "complete":
            manifest = self._manifest(workspace_key, key)
            if manifest is None:
                raise AidpProvisionError("The participant reset journal disappeared during provisioning.")
            reset = self._reset_record(manifest, operation_id, industry, target_root)
            reset["phase"] = "complete"
            self._write_manifest(workspace_key, key, manifest)
        return material

    async def reset_user(
        self,
        user_ocid: str,
        email: str,
        industry: str,
        operation_id: str,
    ) -> UserMaterial:
        key = participant_key(user_ocid)
        async with self._locks.setdefault(key, asyncio.Lock()):
            return await asyncio.to_thread(
                self._reset_user, user_ocid, email, industry, operation_id
            )

    def _user_industries(self, user_ocids: list[str]) -> dict[str, str]:
        if not user_ocids:
            return {}
        workspace_key = str(self._workspace()["key"])
        keys = {participant_key(user_ocid): user_ocid for user_ocid in user_ocids}
        expected_jobs = {
            f"wf_{key}_{industry}_medallion": (user_ocid, industry)
            for key, user_ocid in keys.items()
            for industry in INDUSTRIES
        }
        matches: dict[str, set[str]] = {user_ocid: set() for user_ocid in user_ocids}
        for job in self._list(f"/workspaces/{workspace_key}/jobs", phase="content"):
            match = expected_jobs.get(self._resource_name(job))
            if match is not None:
                matches[match[0]].add(match[1])
        industries = {
            user_ocid: next(iter(values))
            for user_ocid, values in matches.items()
            if len(values) == 1
        }
        for key, user_ocid in keys.items():
            if user_ocid in industries:
                continue
            manifest = self._manifest(workspace_key, key)
            if manifest is not None:
                self._manifest_workspace_path(manifest, key)
                reset = manifest.get("reset")
                candidate = (
                    reset.get("target_industry")
                    if isinstance(reset, dict) and reset.get("phase") in {"cleanup", "provision"}
                    else manifest.get("industry")
                )
            else:
                legacy = self._workspace_json(
                    workspace_key,
                    self._legacy_control_manifest_path(key),
                    "The legacy participant control manifest is invalid.",
                )
                candidate = legacy.get("industry") if legacy and legacy.get("participant_key") == key else None
            if candidate in INDUSTRIES:
                industries[user_ocid] = str(candidate)
        return industries

    async def list_user_industries(self, user_ocids: list[str]) -> dict[str, str]:
        return await asyncio.to_thread(self._user_industries, user_ocids)

    def _delete_object_storage_prefix(self, prefix: str) -> None:
        start: str | None = None
        while True:
            response = self.object_storage.list_objects(
                self.settings.objectstorage_namespace,
                self.settings.bucket_name,
                prefix=prefix,
                start=start,
            )
            for item in response.data.objects:
                self.object_storage.delete_object(
                    self.settings.objectstorage_namespace,
                    self.settings.bucket_name,
                    item.name,
                )
            start = response.data.next_start_with
            if not start:
                return

    def _object_storage_prefix_exists(self, prefix: str) -> bool:
        response = self.object_storage.list_objects(
            self.settings.objectstorage_namespace,
            self.settings.bucket_name,
            prefix=prefix,
            start=None,
        )
        return bool(response.data.objects)

    def _participant_jobs(
        self,
        workspace_key: str,
        key: str,
    ) -> list[dict[str, Any]]:
        expected_jobs = {
            f"wf_{key}_{industry}_medallion" for industry in INDUSTRIES
        }
        return [
            job
            for job in self._list(
                f"/workspaces/{workspace_key}/jobs", phase="content"
            )
            if self._resource_name(job) in expected_jobs
        ]

    def _cleanup_jobs(self, workspace_key: str, key: str) -> None:
        for job in self._participant_jobs(workspace_key, key):
            job_key = job.get("key") or job.get("id")
            state = str(job.get("lifecycleState") or job.get("state") or "").upper()
            if job_key and state != "DELETING":
                self._request(
                    "DELETE",
                    f"/workspaces/{workspace_key}/jobs/{job_key}",
                    allow_not_found=True,
                    phase="content",
                )
        if self._participant_jobs(workspace_key, key):
            raise AidpProvisionPending(
                "Participant workflow deletion is still in progress.", "content"
            )

    def _shared_schemas(
        self,
        catalog_key: str,
    ) -> dict[str, dict[str, Any]]:
        schemas = self._list(
            "/schemas",
            params={"catalogKey": catalog_key},
            phase="schemas",
        )
        result: dict[str, dict[str, Any]] = {}
        for layer in LAYER_PREFIXES:
            name = schema_name(layer)
            matches = [schema for schema in schemas if self._resource_name(schema) == name]
            if len(matches) > 1:
                raise AidpProvisionError(f"AIDP has duplicate schemas named {name}.")
            if matches:
                result[layer] = matches[0]
        return result

    def _legacy_participant_schemas(
        self,
        catalog_key: str,
        key: str,
    ) -> list[dict[str, Any]]:
        expected = {f"{key}_{layer}" for layer in LAYER_PREFIXES}
        return [
            schema
            for schema in self._list(
                "/schemas", params={"catalogKey": catalog_key}, phase="schemas"
            )
            if self._resource_name(schema) in expected
        ]

    def _schema_tables(
        self,
        catalog_key: str,
        schema_key: str,
    ) -> list[dict[str, Any]]:
        return self._list(
            "/tables",
            params={"catalogKey": catalog_key, "schemaKey": schema_key},
            phase="schemas",
        )

    def _cleanup_tables(self, catalog_key: str, key: str) -> None:
        schemas = self._shared_schemas(catalog_key)
        for layer, schema in schemas.items():
            if not schema.get("key"):
                continue
            schema_key = str(schema["key"])
            expected = participant_table_names(key, layer)
            for table in self._schema_tables(catalog_key, schema_key):
                table_key = table.get("key")
                if table_key and self._resource_name(table) in expected:
                    self._request(
                        "DELETE",
                        f"/tables/{table_key}",
                        allow_not_found=True,
                        phase="schemas",
                    )
        for layer, schema in self._shared_schemas(catalog_key).items():
            schema_key = str(schema.get("key") or "")
            expected = participant_table_names(key, layer)
            if schema_key and any(
                self._resource_name(table) in expected
                for table in self._schema_tables(catalog_key, schema_key)
            ):
                raise AidpProvisionPending(
                    "Participant table deletion is still in progress.", "schemas"
                )

    def _cleanup_legacy_tables(self, catalog_key: str, key: str) -> None:
        for schema in self._legacy_participant_schemas(catalog_key, key):
            schema_key = str(schema.get("key") or "")
            if schema_key:
                for table in self._schema_tables(catalog_key, schema_key):
                    table_key = str(table.get("key") or "")
                    if table_key:
                        self._request(
                            "DELETE",
                            f"/tables/{table_key}",
                            allow_not_found=True,
                            phase="schemas",
                        )
                if self._schema_tables(catalog_key, schema_key):
                    raise AidpProvisionPending(
                        "Legacy participant table deletion is still in progress.", "schemas"
                    )

    def _cleanup_legacy_schemas(self, catalog_key: str, key: str) -> None:
        for schema in self._legacy_participant_schemas(catalog_key, key):
            schema_key = str(schema.get("key") or "")
            state = str(schema.get("lifecycleState") or "").upper()
            if not schema_key or state == "DELETING":
                continue
            self._request(
                "DELETE",
                f"/schemas/{schema_key}",
                allow_not_found=True,
                phase="schemas",
            )
        if self._legacy_participant_schemas(catalog_key, key):
            raise AidpProvisionPending(
                "Legacy participant schema deletion is still in progress.", "schemas"
            )

    def _cleanup_object_storage(self, key: str) -> None:
        for prefix in LAYER_PREFIXES.values():
            self._delete_object_storage_prefix(f"{prefix}/users/{key}/")
        if any(
            self._object_storage_prefix_exists(f"{prefix}/users/{key}/")
            for prefix in LAYER_PREFIXES.values()
        ):
            raise AidpProvisionPending(
                "Participant Object Storage cleanup is still in progress.", "content"
            )

    def _delete_workspace_path(
        self,
        workspace_key: str,
        path: str,
        pending_message: str,
    ) -> None:
        body, headers = self._workspace_object(
            workspace_key, path, phase="content"
        )
        if not self._workspace_object_exists(body, headers):
            return
        self._request(
            "DELETE",
            f"/workspaces/{workspace_key}/objects/{quote(path, safe='')}",
            headers={"Accept": "*/*"},
            allow_not_found=True,
            phase="content",
        )
        body, headers = self._workspace_object(
            workspace_key, path, phase="content"
        )
        if self._workspace_object_exists(body, headers):
            raise AidpProvisionPending(pending_message, "content")

    def _cleanup_user(self, key: str, preserve_manifest: bool = False) -> None:
        workspace_key = str(self._workspace()["key"])
        manifest = self._manifest(workspace_key, key)
        participant_root = ""
        if manifest is not None:
            workspace_path = self._manifest_workspace_path(manifest, key)
            participant_root = workspace_path.rsplit("/", 1)[0]
        self._cleanup_jobs(workspace_key, key)
        catalog_key = str(self._catalog()["key"])
        self._cleanup_tables(catalog_key, key)
        self._cleanup_legacy_tables(catalog_key, key)
        self._cleanup_legacy_schemas(catalog_key, key)
        self._cleanup_object_storage(key)
        if participant_root:
            self._delete_workspace_path(
                workspace_key,
                participant_root,
                "Participant workspace deletion is still in progress.",
            )
        self._delete_workspace_path(
            workspace_key,
            f"{LEGACY_WORKSPACE_ROOT}/{key}",
            "Legacy participant workspace deletion is still in progress.",
        )
        if not preserve_manifest:
            self._delete_workspace_path(
                workspace_key,
                self._control_manifest_path(key),
                "Participant control manifest deletion is still in progress.",
            )
        self._delete_workspace_path(
            workspace_key,
            self._legacy_control_manifest_path(key),
            "Legacy participant control manifest deletion is still in progress.",
        )


    async def cleanup_user(self, user_ocid: str) -> None:
        key = participant_key(user_ocid)
        async with self._locks.setdefault(key, asyncio.Lock()):
            await asyncio.to_thread(self._cleanup_user, key)

    def _healthcheck(self) -> None:
        workspace = self._workspace()
        self._shared_compute(str(workspace["key"]))
        self._catalog()
        self.object_storage.head_bucket(
            self.settings.objectstorage_namespace,
            self.settings.bucket_name,
        )

    async def healthcheck(self) -> None:
        await asyncio.to_thread(self._healthcheck)
