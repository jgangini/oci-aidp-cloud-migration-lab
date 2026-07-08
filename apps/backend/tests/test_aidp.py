import asyncio
import hashlib
import json
import threading
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from app.aidp import (
    AidpClient,
    AidpProvisionConflict,
    AidpProvisionError,
    AidpProvisionPending,
    LocalAidpClient,
    UserMaterial,
    participant_key,
)
from app.config import Settings
from app.notebooks import LAYER_PREFIXES, schema_name, user_notebooks


USER_OCID = "ocid1.user.oc1..aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
EMAIL = "ada@example.com"


class FakeResponse:
    def __init__(self, body=None, *, headers=None, status_code=200, content=None) -> None:
        self._body = body
        self.headers = headers or {}
        self.status_code = status_code
        self.content = content if content is not None else (json.dumps(body).encode() if body is not None else b"")

    def json(self):
        return self._body


def bare_client() -> AidpClient:
    client = object.__new__(AidpClient)
    client.base = "https://aidp.example.invalid/20260430/aiDataPlatforms/platform"
    client.signer = object()
    client._session_lock = threading.Lock()
    client._locks = {}
    return client


def test_participant_key_is_exact_stable_opaque_ocid_hash() -> None:
    expected = "u_" + hashlib.sha256(USER_OCID.encode()).hexdigest()[:16]
    assert participant_key(USER_OCID) == expected
    assert participant_key(USER_OCID) == participant_key(USER_OCID)
    assert EMAIL not in expected
    assert len(expected) == 18
    with pytest.raises(ValueError, match="valid OCI user OCID"):
        participant_key(EMAIL)
    with pytest.raises(ValueError, match="valid OCI user OCID"):
        participant_key(f"{USER_OCID} invalid")


def test_notebooks_use_opaque_oci_uris_and_register_fifteen_tables() -> None:
    key = participant_key(USER_OCID)
    notebooks = user_notebooks("retail", key, "aidp-data-test", "namespace")
    assert list(notebooks) == [
        "01_landing_retail.ipynb",
        "02_bronze_retail.ipynb",
        "03_silver_retail.ipynb",
        "04_gold_retail.ipynb",
    ]
    rendered = json.dumps(notebooks)
    assert f"/Workspace/lab-users/{key}/retail" in rendered
    assert f"oci://aidp-data-test@namespace/01_landing/users/{key}/retail/" in rendered
    assert "CREATE EXTERNAL TABLE IF NOT EXISTS" in rendered
    assert "quality_issues" in rendered
    assert EMAIL not in rendered
    assert "/Volumes/" not in rendered
    assert rendered.count("CREATE EXTERNAL TABLE IF NOT EXISTS") == 15
    for notebook in notebooks.values():
        assert notebook["nbformat"] == 4
        assert all("cell_type" in cell for cell in notebook["cells"])
        assert len({cell["id"] for cell in notebook["cells"]}) == len(notebook["cells"])


def test_request_retry_token_covers_binary_content_and_identity_headers() -> None:
    client = bare_client()
    calls: list[dict] = []

    class Session:
        def request(self, _method, _url, **kwargs):
            calls.append(kwargs)
            return FakeResponse()

    client.session = Session()
    client._request("POST", "/objects", data=b"\x00binary", headers={"path": "/one", "type": "FILE"})
    client._request("POST", "/objects", data=b"\x00binary", headers={"path": "/one", "type": "FILE"})
    client._request("POST", "/objects", data=b"\x00changed", headers={"path": "/one", "type": "FILE"})
    client._request("POST", "/objects", data=b"\x00binary", headers={"path": "/two", "type": "FILE"})
    client._request("POST", "/notebooks", payload={"type": "notebook"}, retry_scope="/one.ipynb")
    client._request("POST", "/notebooks", payload={"type": "notebook"}, retry_scope="/two.ipynb")
    tokens = [call["headers"]["opc-retry-token"] for call in calls]
    assert tokens[0] == tokens[1]
    assert tokens[0] != tokens[2]
    assert tokens[0] != tokens[3]
    assert tokens[4] != tokens[5]


def test_list_follows_opc_next_page_and_preserves_filters() -> None:
    client = bare_client()
    calls: list[dict | None] = []

    def request(_method, _path, *, params=None, **_kwargs):
        calls.append(params)
        if len(calls) == 1:
            return {"items": [{"key": "one"}]}, {"opc-next-page": "next-token"}
        return [{"key": "two"}], {}

    client._request = request
    assert client._list("/schemas", params={"catalogKey": "catalog"}, phase="schemas") == [
        {"key": "one"},
        {"key": "two"},
    ]
    assert calls == [
        {"catalogKey": "catalog"},
        {"catalogKey": "catalog", "page": "next-token"},
    ]


def test_control_resources_require_operational_lifecycle_states() -> None:
    client = bare_client()
    client.settings = SimpleNamespace(aidp_workspace_name="workspace")
    resources = {
        "/workspaces": [{"displayName": "workspace", "key": "workspace", "lifecycleState": "ACTIVE"}],
        "/catalogs": [{"displayName": "aidp_lab", "key": "catalog", "lifecycleState": "ACTIVE"}],
        "/workspaces/workspace/clusters": [
            {"displayName": "aidp_lab_shared_compute", "key": "compute", "lifecycleState": "STOPPED"}
        ],
    }
    client._list = lambda path, **_kwargs: resources[path]

    assert client._workspace()["key"] == "workspace"
    assert client._catalog()["key"] == "catalog"
    assert client._shared_compute("workspace")["key"] == "compute"

    resources["/workspaces"][0]["lifecycleState"] = "CREATING"
    with pytest.raises(AidpProvisionPending, match="not operational") as pending:
        client._workspace()
    assert pending.value.phase == "workspace"

    resources["/workspaces"][0]["lifecycleState"] = "ACTIVE"
    resources["/catalogs"][0]["lifecycleState"] = "FAILED"
    with pytest.raises(AidpProvisionError, match="terminal state FAILED"):
        client._catalog()

    resources["/catalogs"][0]["lifecycleState"] = "ACTIVE"
    resources["/workspaces/workspace/clusters"][0]["lifecycleState"] = "DELETING"
    with pytest.raises(AidpProvisionError, match="terminal state DELETING"):
        client._shared_compute("workspace")


def test_workspace_folders_and_files_only_write_missing_or_drifted_content() -> None:
    client = bare_client()
    objects: dict[str, tuple[object, dict[str, str]]] = {
        "/existing": (None, {"object-key": "folder-key", "type": "FOLDER"}),
        "/exact.csv": (b"exact", {"object-key": "exact-key", "type": "FILE"}),
        "/drifted.csv": (b"old", {"object-key": "drifted-key", "type": "FILE"}),
        "/wrong": (None, {"object-key": "file-key", "type": "FILE"}),
    }
    writes: list[tuple[str, str]] = []
    client._workspace_object = lambda _workspace, path, **_kwargs: objects.get(path, (None, {}))

    def request(method, _path, *, data=None, headers=None, **_kwargs):
        assert method == "POST"
        assert headers["Accept"] == "*/*"
        path = headers["path"]
        writes.append((path, headers["is-overwrite"]))
        objects[path] = (
            (None if headers["type"] == "FOLDER" else data),
            {"object-key": f"key-{len(writes)}", "type": headers["type"]},
        )

    client._request = request
    assert not client._ensure_folder("workspace", "/existing")
    with pytest.raises(AidpProvisionError, match="not a folder"):
        client._ensure_folder("workspace", "/wrong")
    assert client._ensure_folder("workspace", "/missing")
    assert not client._upload_file("workspace", "/exact.csv", b"exact")
    assert not client._upload_file(
        "workspace", "/drifted.csv", b"new", repair_drift=False
    )
    assert client._upload_file("workspace", "/drifted.csv", b"new")
    assert client._upload_file("workspace", "/missing.csv", b"new")
    assert writes == [
        ("/missing", "false"),
        ("/drifted.csv", "true"),
        ("/missing.csv", "false"),
    ]


def test_workspace_object_get_negotiates_binary_content() -> None:
    client = bare_client()
    calls: list[dict] = []
    client._request = lambda _method, _path, **kwargs: (
        calls.append(kwargs) or (b"content", {"type": "FILE"})
    )
    assert client._workspace_object("workspace", "/Workspace/file.csv", phase="content") == (
        b"content",
        {"type": "FILE"},
    )
    assert calls[0]["headers"] == {"Accept": "*/*"}


def test_notebooks_are_compared_before_create_or_update() -> None:
    client = bare_client()
    expected = {"nbformat": 4, "cells": []}
    normalized = {**expected, "metadata": {"trusted": True}}
    notebooks = {
        "/Workspace/lab-users/u_one/banking/exact.ipynb": normalized,
        "/Workspace/lab-users/u_one/banking/drifted.ipynb": {"nbformat": 4, "cells": [1]},
    }
    writes: list[tuple[str, str]] = []

    def request(method, path, *, payload=None, **_kwargs):
        target = path.rsplit("/", 1)[-1]
        decoded = target.replace("%2F", "/")
        if method == "GET":
            content = notebooks.get(decoded)
            return None if content is None else {"content": content}
        if method == "POST":
            writes.append((method, path))
            return {"path": f"{decoded}/Untitled.ipynb"}
        if method == "PATCH":
            writes.append((method, path))
            return None
        if method == "PUT":
            writes.append((method, path))
            notebooks[payload["path"]] = payload["content"]
            return None
        raise AssertionError(method)

    client._request = request
    exact_path = "/Workspace/lab-users/u_one/banking/exact.ipynb"
    drifted_path = "/Workspace/lab-users/u_one/banking/drifted.ipynb"
    missing_path = "/Workspace/lab-users/u_one/banking/missing.ipynb"
    assert not client._upload_notebook("workspace", exact_path, expected)
    assert not client._upload_notebook(
        "workspace", drifted_path, expected, repair_drift=False
    )
    assert client._upload_notebook("workspace", drifted_path, expected)
    assert client._upload_notebook("workspace", missing_path, expected)
    assert [method for method, _ in writes] == ["PUT", "POST", "PATCH", "PUT"]


def test_schema_contract_lists_real_resources_and_creates_only_missing() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    schemas: dict[str, dict] = {}
    posts: list[str] = []
    client._list = lambda *_args, **_kwargs: list(schemas.values())

    def request(method, _path, *, payload=None, **_kwargs):
        assert method == "POST"
        posts.append(payload["displayName"])
        schemas[payload["displayName"]] = {
            "displayName": payload["displayName"],
            "key": f"schema-{len(posts)}",
        }

    client._request = request
    contract, changed = client._ensure_catalog_contract("catalog", key)
    assert changed
    assert set(contract) == set(LAYER_PREFIXES)
    assert len(posts) == 4
    assert client._ensure_catalog_contract("catalog", key) == (contract, False)
    assert len(posts) == 4


def test_job_is_returned_only_after_exact_four_stage_contract_is_visible() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    root = f"/Workspace/lab-users/{key}/banking"
    notebook_names = [
        "01_landing_banking.ipynb",
        "02_bronze_banking.ipynb",
        "03_silver_banking.ipynb",
        "04_gold_banking.ipynb",
    ]
    notebooks = {name: {} for name in notebook_names}
    published: dict = {}
    expected_job_name = f"wf_{key}_banking_medallion"

    client._list = lambda *_args, **_kwargs: [{"displayName": expected_job_name, "key": "job-key"}]

    def request(method, _path, *, payload=None, **_kwargs):
        if method == "PUT":
            published.update(payload)
        return published if method == "GET" else None

    client._request = request
    job_name, job_key, changed = client._ensure_job(
        "workspace", "compute", key, "banking", notebooks
    )
    assert job_key == "job-key"
    assert job_name == expected_job_name
    assert changed
    assert [task["notebookPath"] for task in published["tasks"]] == [
        f"{root}/{name}" for name in notebook_names
    ]
    assert [task["dependsOn"] for task in published["tasks"]] == [
        [],
        [{"taskKey": "stage_1"}],
        [{"taskKey": "stage_2"}],
        [{"taskKey": "stage_3"}],
    ]
    assert published["jobClusters"] == [{"clusterKey": "compute"}]

    writes: list[str] = []
    client._request = lambda method, _path, **_kwargs: (
        writes.append(method) if method == "PUT" else published
    )
    assert client._ensure_job("workspace", "compute", key, "banking", notebooks) == (
        expected_job_name,
        "job-key",
        False,
    )
    assert writes == []

    client._request = lambda method, _path, **_kwargs: (
        {**published, "tasks": published["tasks"][:-1]} if method == "GET" else None
    )
    with pytest.raises(AidpProvisionPending, match="complete participant workflow") as pending:
        client._ensure_job("workspace", "compute", key, "banking", notebooks)
    assert pending.value.phase == "content"

    wrong_type = [{**published["tasks"][0], "type": "PYTHON_TASK"}, *published["tasks"][1:]]
    drift_writes: list[str] = []

    def drift_request(method, _path, **_kwargs):
        if method == "PUT":
            drift_writes.append(method)
            return None
        return {**published, "tasks": wrong_type}

    client._request = drift_request
    assert client._ensure_job(
        "workspace",
        "compute",
        key,
        "banking",
        notebooks,
        repair_drift=False,
    ) == (expected_job_name, "job-key", False)
    assert drift_writes == []
    with pytest.raises(AidpProvisionPending, match="complete participant workflow"):
        client._ensure_job("workspace", "compute", key, "banking", notebooks)
    assert drift_writes == ["PUT"]


def test_permission_requires_user_and_exact_inheritance_then_rechecks() -> None:
    client = bare_client()
    wrong = {
        "grantee": USER_OCID,
        "granteeType": "GROUP",
        "granteePermissions": ["ADMIN"],
        "isPermissionsInheritable": True,
    }
    exact = {**wrong, "granteeType": "USER"}
    assert not client._permission_matches(wrong, USER_OCID, "ADMIN", True)
    assert not client._permission_matches({**exact, "isPermissionsInheritable": False}, USER_OCID, "ADMIN", True)
    assert client._permission_matches(exact, USER_OCID, "ADMIN", True)

    reads = iter(([wrong], [exact]))
    posts: list[dict] = []
    client._list = lambda *_args, **_kwargs: next(reads)
    client._request = lambda method, _path, **kwargs: posts.append(kwargs["payload"]) if method == "POST" else None
    assert client._ensure_permission(
        "/workspaces/workspace/objects/object",
        "assignWorkspaceObjectPermissionDetails",
        USER_OCID,
        "ADMIN",
        inheritable=True,
    )
    assert posts == [
        {
            "assignWorkspaceObjectPermissionDetails": {
                "assignees": {"type": "USER", "targets": [USER_OCID]},
                "permissions": ["ADMIN"],
                "isPermissionsInheritable": True,
            }
        }
    ]

    client._list = lambda *_args, **_kwargs: [wrong]
    with pytest.raises(AidpProvisionPending, match="has not applied") as pending:
        client._ensure_permission(
            "/workspaces/workspace/objects/object",
            "assignWorkspaceObjectPermissionDetails",
            USER_OCID,
            "ADMIN",
            inheritable=True,
        )
    assert pending.value.phase == "permissions"

    post_count = len(posts)
    for conflicting in (
        {**exact, "granteePermissions": ["ADMIN", "READ"]},
        {**exact, "isPermissionsInheritable": False},
    ):
        client._list = lambda *_args, item=conflicting, **_kwargs: [item]
        with pytest.raises(AidpProvisionError, match="conflicting direct permission"):
            client._ensure_permission(
                "/workspaces/workspace/objects/object",
                "assignWorkspaceObjectPermissionDetails",
                USER_OCID,
                "ADMIN",
                inheritable=True,
            )
    assert len(posts) == post_count


class FakeServiceError(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


def test_control_manifest_is_workspace_scoped_idempotent_and_industry_is_immutable() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    control_path = f"/Workspace/lab-users/.control/{key}.json"
    objects: dict[str, tuple[object, dict[str, str]]] = {}
    writes: list[str] = []
    client._workspace_object = lambda _workspace, path, **_kwargs: objects.get(
        path, (None, {})
    )

    def request(method, _path, *, data=None, headers=None, **_kwargs):
        assert method == "POST"
        writes.append(headers["path"])
        objects[headers["path"]] = (
            data,
            {"object-key": "control-key", "object-type": "FILE"},
        )

    client._request = request

    first = client._ensure_manifest("workspace", key, "banking")
    assert first == {"participant_key": key, "industry": "banking", "phase": "workspace"}
    assert writes == [control_path]
    assert client._ensure_manifest("workspace", key, "banking") == first
    assert writes == [control_path]
    client._advance_manifest("workspace", first, "schemas")
    assert client._ensure_manifest("workspace", key, "banking")["phase"] == "schemas"
    assert writes == [control_path, control_path]
    client._advance_manifest("workspace", first, "schemas")
    assert writes == [control_path, control_path]
    with pytest.raises(AidpProvisionConflict, match="delete and recreate"):
        client._ensure_manifest("workspace", key, "retail")

    objects[control_path] = (
        b"{invalid-json",
        {"object-key": "control-key", "object-type": "FILE"},
    )
    with pytest.raises(AidpProvisionError, match="control manifest is invalid"):
        client._manifest("workspace", key)


def test_provisioning_reconciles_real_inventory_and_repairs_active_drift() -> None:
    client = bare_client()
    client.settings = SimpleNamespace(bucket_name="bucket", objectstorage_namespace="namespace")
    key = participant_key(USER_OCID)
    manifest = {"participant_key": key, "industry": "banking", "phase": "workspace"}
    root = f"/Workspace/lab-users/{key}/banking"
    folders: set[str] = set()
    files: dict[str, bytes] = {}
    notebooks: dict[str, dict] = {}
    writes = {"folders": 0, "files": 0, "notebooks": 0, "schemas": 0, "job": 0, "permissions": 0}
    state = {"schemas": False, "job": False, "permissions": False}
    schemas = {layer: {"key": f"schema-{layer}"} for layer in LAYER_PREFIXES}
    job_name = f"wf_{key}_banking_medallion"

    client._workspace = lambda: {"key": "workspace"}
    client._shared_compute = lambda _workspace: {"key": "compute"}
    client._catalog = lambda: {"key": "catalog"}

    def ensure_folder(_workspace, path):
        changed = path not in folders
        if changed:
            folders.add(path)
            writes["folders"] += 1
        return changed

    def ensure_manifest(_workspace, _key, _industry):
        assert "/Workspace/lab-users/.control" in folders
        return manifest

    def ensure_schemas(_catalog, _key):
        changed = not state["schemas"]
        state["schemas"] = True
        writes["schemas"] += int(changed)
        return schemas, changed

    def upload_file(_workspace, path, content, **_kwargs):
        changed = files.get(path) != content
        if changed:
            files[path] = content
            writes["files"] += 1
        return changed

    def upload_notebook(_workspace, path, notebook, **_kwargs):
        changed = notebooks.get(path) != notebook
        if changed:
            notebooks[path] = notebook
            writes["notebooks"] += 1
        return changed

    def ensure_job(*_args, **_kwargs):
        changed = not state["job"]
        state["job"] = True
        writes["job"] += int(changed)
        return job_name, "job-key", changed

    def ensure_permissions(*_args):
        changed = not state["permissions"]
        state["permissions"] = True
        writes["permissions"] += int(changed)
        return changed

    client._ensure_folder = ensure_folder
    client._ensure_manifest = ensure_manifest
    client._advance_manifest = lambda _workspace, value, phase: value.update(phase=phase)
    client._ensure_catalog_contract = ensure_schemas
    client._upload_file = upload_file
    client._upload_notebook = upload_notebook
    client._ensure_job = ensure_job
    client._ensure_permissions = ensure_permissions

    phases = []
    for _ in range(3):
        with pytest.raises(AidpProvisionPending) as pending:
            client._provision_user(USER_OCID, EMAIL, "banking")
        phases.append(pending.value.phase)
    assert phases == ["schemas", "content", "permissions"]
    assert manifest["phase"] == "permissions"
    assert len(folders) == 5
    assert "/Workspace/lab-users/.control" in folders
    assert len(files) == 5  # one participant manifest plus four source CSVs
    assert len(notebooks) == 4
    assert all(path.startswith(f"/Workspace/lab-users/{key}/banking/") for path in notebooks)
    assert all("/notebooks/" not in path for path in notebooks)

    material = client._provision_user(USER_OCID, EMAIL, "banking")
    assert material == UserMaterial(
        EMAIL,
        "banking",
        key,
        f"/Workspace/lab-users/{key}/banking",
        job_name,
    )
    assert manifest["phase"] == "active"
    completed_writes = writes.copy()
    assert client._provision_user(USER_OCID, EMAIL, "banking") == material
    assert writes == completed_writes

    missing_csv = next(path for path in files if path.startswith(f"{root}/source/"))
    del files[missing_csv]
    with pytest.raises(AidpProvisionPending) as pending:
        client._provision_user(USER_OCID, EMAIL, "banking")
    assert pending.value.phase == "permissions"
    assert writes["files"] == completed_writes["files"] + 1
    assert client._provision_user(USER_OCID, EMAIL, "banking") == material

    folders.remove(f"{root}/source")
    with pytest.raises(AidpProvisionPending) as pending:
        client._provision_user(USER_OCID, EMAIL, "banking")
    assert pending.value.phase == "schemas"
    assert client._provision_user(USER_OCID, EMAIL, "banking") == material

    state["schemas"] = False
    with pytest.raises(AidpProvisionPending) as pending:
        client._provision_user(USER_OCID, EMAIL, "banking")
    assert pending.value.phase == "content"
    assert client._provision_user(USER_OCID, EMAIL, "banking") == material

    del notebooks[next(iter(notebooks))]
    with pytest.raises(AidpProvisionPending) as pending:
        client._provision_user(USER_OCID, EMAIL, "banking")
    assert pending.value.phase == "permissions"
    assert client._provision_user(USER_OCID, EMAIL, "banking") == material

    state["job"] = False
    with pytest.raises(AidpProvisionPending) as pending:
        client._provision_user(USER_OCID, EMAIL, "banking")
    assert pending.value.phase == "permissions"
    assert client._provision_user(USER_OCID, EMAIL, "banking") == material

    state["permissions"] = False
    with pytest.raises(AidpProvisionPending) as pending:
        client._provision_user(USER_OCID, EMAIL, "banking")
    assert pending.value.phase == "permissions"
    assert manifest["phase"] == "active"
    assert client._provision_user(USER_OCID, EMAIL, "banking") == material


def test_local_client_is_idempotent_conflict_safe_and_cleanup_is_exact() -> None:
    async def run() -> None:
        client = LocalAidpClient(Settings(local_development_mode=True))
        first = await client.provision_user(USER_OCID, EMAIL, "healthcare")
        second = await client.provision_user(USER_OCID, EMAIL, "healthcare")
        assert first == second
        assert len(client.users) == 1
        with pytest.raises(AidpProvisionConflict, match="delete and recreate"):
            await client.provision_user(USER_OCID, EMAIL, "retail")
        await client.cleanup_user(USER_OCID)
        assert client.users == {}

    asyncio.run(run())


class CleanupStorage:
    def __init__(self, prefixes: list[str]) -> None:
        self.objects = {f"{prefix}part-000.csv": b"data" for prefix in prefixes}
        self.deleted: list[str] = []
        self.pages: list[tuple[str, str | None]] = []

    def list_objects(self, _namespace, _bucket, *, prefix, start):
        self.pages.append((prefix, start))
        return SimpleNamespace(
            data=SimpleNamespace(
                objects=[
                    SimpleNamespace(name=name)
                    for name in sorted(self.objects)
                    if name.startswith(prefix)
                ],
                next_start_with=None,
            )
        )

    def delete_object(self, _namespace, _bucket, name):
        if name not in self.objects:
            raise FakeServiceError(404)
        self.deleted.append(name)


class CleanupScenario:
    industries = ("banking", "telecommunications", "retail", "healthcare")

    def __init__(self) -> None:
        self.client = bare_client()
        self.key = participant_key(USER_OCID)
        self.client.settings = SimpleNamespace(
            objectstorage_namespace="namespace", bucket_name="bucket"
        )
        self.client._oci = SimpleNamespace(
            exceptions=SimpleNamespace(ServiceError=FakeServiceError)
        )
        prefixes = [
            f"{prefix}/users/{self.key}/" for prefix in LAYER_PREFIXES.values()
        ]
        self.client.object_storage = CleanupStorage(prefixes)
        self.client._workspace = lambda: {"key": "workspace"}
        self.client._catalog = lambda: {"key": "catalog"}
        self.schema_keys = {
            layer: f"schema-{layer}" for layer in LAYER_PREFIXES
        }
        self.exact_schemas = [
            {
                "displayName": schema_name(self.key, layer),
                "key": schema_key,
                "lifecycleState": "ACTIVE",
            }
            for layer, schema_key in self.schema_keys.items()
        ]
        self.near_schema = {
            "displayName": f"{schema_name(self.key, 'landing')}_archive",
            "key": "near-schema",
        }
        self.exact_jobs = [
            {
                "displayName": f"wf_{self.key}_{industry}_medallion",
                "key": f"job-{industry}",
                "lifecycleState": "ACTIVE",
            }
            for industry in self.industries
        ]
        self.near_job = {
            "displayName": f"wf_{self.key}_banking_medallion_archive",
            "key": "near-job",
        }
        self.resources = {
            "jobs": [*self.exact_jobs, self.near_job],
            "schemas": [*self.exact_schemas, self.near_schema],
        }
        self.tables = {
            schema_key: [
                {"displayName": "arbitrary_table_name", "key": f"{schema_key}-one"},
                {"displayName": "quality_issues", "key": f"{schema_key}-two"},
            ]
            for schema_key in self.schema_keys.values()
        }
        self.table_queries: list[str] = []
        self.participant_path = f"/Workspace/lab-users/{self.key}"
        self.control_path = self.client._control_manifest_path(self.key)
        self.workspace_objects = {
            self.participant_path: (None, {"object-key": "participant-folder"}),
            self.control_path: (
                b"{corrupt-json",
                {"object-key": "control-file", "object-type": "FILE"},
            ),
        }
        self.requests: list[tuple[str, str, dict]] = []
        self.client._list = self.list_resources
        self.client._request = self.request
        self.client._workspace_object = self.workspace_object

    def list_resources(self, path, *, params=None, **_kwargs):
        if path.endswith("/jobs"):
            return self.resources["jobs"]
        if path == "/schemas":
            return self.resources["schemas"]
        if path == "/tables":
            self.table_queries.append(params["schemaKey"])
            return self.tables[params["schemaKey"]]
        return []

    def workspace_object(self, _workspace, path, **_kwargs):
        return self.workspace_objects.get(path, (None, {}))

    def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        for job in self.exact_jobs:
            if path == f"/workspaces/workspace/jobs/{job['key']}":
                job["lifecycleState"] = "DELETING"
        if not path.startswith("/schemas/"):
            return
        schema_key = path.rsplit("/", 1)[-1]
        for schema in self.exact_schemas:
            if schema["key"] == schema_key:
                schema["lifecycleState"] = "DELETING"

    def expect_pending(self, message: str, phase: str) -> None:
        with pytest.raises(AidpProvisionPending, match=message) as pending:
            self.client._cleanup_user(self.key)
        assert pending.value.phase == phase

    def deleted_paths(self) -> set[str]:
        return {
            path for method, path, _ in self.requests if method == "DELETE"
        }

    def run_jobs_phase(self) -> None:
        self.expect_pending("workflow deletion", "content")
        assert all(job["lifecycleState"] == "DELETING" for job in self.exact_jobs)
        assert self.deleted_paths() == {
            f"/workspaces/workspace/jobs/job-{industry}"
            for industry in self.industries
        }
        self.resources["jobs"] = [self.near_job]

    def run_tables_phase(self) -> None:
        self.expect_pending("table deletion", "schemas")
        expected_tables = {
            f"/tables/{schema_key}-{suffix}"
            for schema_key in self.schema_keys.values()
            for suffix in ("one", "two")
        }
        assert expected_tables.issubset(self.deleted_paths())
        assert not any(
            path.startswith("/schemas/") for path in self.deleted_paths()
        )
        for schema_tables in self.tables.values():
            schema_tables.clear()

    def run_schemas_phase(self) -> None:
        self.expect_pending("schema deletion", "schemas")
        assert all(
            schema["lifecycleState"] == "DELETING"
            for schema in self.exact_schemas
        )
        self.resources["schemas"] = [self.near_schema]

    def run_storage_phase(self) -> None:
        self.expect_pending("Object Storage cleanup", "content")
        data_objects = set(self.client.object_storage.objects)
        assert data_objects.issubset(set(self.client.object_storage.deleted))
        self.client.object_storage.objects.clear()

    def run_workspace_phase(self) -> str:
        self.expect_pending("workspace deletion", "content")
        participant_delete = (
            f"/workspaces/workspace/objects/{quote(self.participant_path, safe='')}"
        )
        assert any(
            path == participant_delete and kwargs["headers"] == {"Accept": "*/*"}
            for _, path, kwargs in self.requests
        )
        self.workspace_objects.pop(self.participant_path)
        return participant_delete

    def run_control_phase(self, participant_delete: str) -> str:
        self.expect_pending("control manifest deletion", "content")
        control_delete = (
            f"/workspaces/workspace/objects/{quote(self.control_path, safe='')}"
        )
        delete_order = [
            path for method, path, _ in self.requests if method == "DELETE"
        ]
        assert delete_order.index(control_delete) > delete_order.index(participant_delete)
        self.workspace_objects.pop(self.control_path)
        return control_delete

    def finish(self, participant_delete: str, control_delete: str) -> None:
        self.client._cleanup_user(self.key)
        deleted_paths = self.deleted_paths()
        assert set(self.schema_keys.values()).issubset(set(self.table_queries))
        assert {
            f"/schemas/{schema_key}" for schema_key in self.schema_keys.values()
        }.issubset(deleted_paths)
        assert all(
            "near-schema" not in path and "near-job" not in path
            for path in deleted_paths
        )
        assert participant_delete in deleted_paths
        assert control_delete in deleted_paths


def test_cleanup_is_staged_exact_and_deletes_corrupt_control_manifest_last() -> None:
    scenario = CleanupScenario()
    scenario.run_jobs_phase()
    scenario.run_tables_phase()
    scenario.run_schemas_phase()
    scenario.run_storage_phase()
    participant_delete = scenario.run_workspace_phase()
    control_delete = scenario.run_control_phase(participant_delete)
    scenario.finish(participant_delete, control_delete)


def test_async_entrypoints_use_participant_lock_and_to_thread(monkeypatch) -> None:
    client = bare_client()
    calls: list[tuple[str, tuple]] = []

    def provision(*args):
        return args

    def cleanup(*args):
        calls.append(("cleanup_impl", args))

    def health():
        calls.append(("health_impl", ()))

    client._provision_user = provision
    client._cleanup_user = cleanup
    client._healthcheck = health

    async def to_thread(function, *args):
        calls.append((function.__name__, args))
        return function(*args)

    monkeypatch.setattr(asyncio, "to_thread", to_thread)

    async def run() -> None:
        assert await client.provision_user(USER_OCID, EMAIL, "banking") == (
            USER_OCID,
            EMAIL,
            "banking",
        )
        await client.cleanup_user(USER_OCID)
        await client.healthcheck()

    asyncio.run(run())
    assert participant_key(USER_OCID) in client._locks
    assert ("provision", (USER_OCID, EMAIL, "banking")) in calls
    assert ("cleanup", (participant_key(USER_OCID),)) in calls
    assert ("health", ()) in calls


def test_service_key_initialization_and_strict_healthcheck(monkeypatch) -> None:
    import oci

    loaded: list[tuple[str, str]] = []
    signer_args: list[dict] = []
    storage = SimpleNamespace(head_bucket=lambda namespace, bucket: calls.append((namespace, bucket)))
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        oci.config,
        "from_file",
        lambda path, profile: (
            loaded.append((path, profile))
            or {
                "tenancy": "tenancy",
                "user": "user",
                "fingerprint": "fingerprint",
                "key_file": "/etc/aidp-lab/oci/key.pem",
                "region": "us-chicago-1",
            }
        ),
    )
    monkeypatch.setattr(oci.signer, "Signer", lambda **kwargs: signer_args.append(kwargs) or object())
    monkeypatch.setattr(oci.object_storage, "ObjectStorageClient", lambda _config: storage)
    settings = Settings(
        aidp_platform_id="platform",
        aidp_workspace_name="workspace",
        aidp_region="us-chicago-1",
        oci_config_file="/etc/aidp-lab/oci/config",
        objectstorage_namespace="namespace",
        bucket_name="bucket",
    )
    client = AidpClient(settings)
    assert client.base == (
        "https://aidpprod.us-chicago-1.oci.oraclecloud.com/20260430/"
        "aiDataPlatforms/platform"
    )
    assert loaded == [("/etc/aidp-lab/oci/config", "DEFAULT")]
    assert signer_args[0]["private_key_file_location"] == "/etc/aidp-lab/oci/key.pem"

    client._workspace = lambda: {"key": "workspace-key"}
    client._shared_compute = lambda key: calls.append(("compute", key)) or {"key": "compute-key"}
    client._catalog = lambda: calls.append(("catalog", "aidp_lab")) or {"key": "catalog-key"}
    client._healthcheck()
    assert calls == [
        ("compute", "workspace-key"),
        ("catalog", "aidp_lab"),
        ("namespace", "bucket"),
    ]
    asyncio.run(client.close())
