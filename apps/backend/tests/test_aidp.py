import asyncio
import hashlib
import json
import threading
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from app.industry_kits import INDUSTRIES
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
from app.notebooks import (
    LAYER_PREFIXES,
    participant_folder,
    schema_name,
    table_name,
    table_names,
    user_notebooks,
    workspace_root,
)


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
    client.base = "https://aidp.example.invalid/20240831/dataLakes/platform"
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


def test_participant_folder_preserves_common_email_and_escapes_path_delimiters() -> None:
    assert participant_folder("JE.GG.GARCIA24@OUTLOOK.COM") == "je.gg.garcia24@outlook.com"
    assert participant_folder("student/lab@example.com") == "student%2Flab@example.com"


def test_notebooks_use_opaque_oci_uris_and_register_fifteen_tables() -> None:
    key = participant_key(USER_OCID)
    notebooks = user_notebooks("retail", key, EMAIL, "aidp-data-test", "namespace")
    assert list(notebooks) == [
        "01_landing_retail.ipynb",
        "02_bronze_retail.ipynb",
        "03_silver_retail.ipynb",
        "04_gold_retail.ipynb",
    ]
    rendered = json.dumps(notebooks)
    assert f"/Workspace/medallon/{EMAIL}/retail" in rendered
    assert f"oci://aidp-data-test@namespace/01_landing/users/{key}/retail/" in rendered
    assert "CREATE EXTERNAL TABLE IF NOT EXISTS" in rendered
    assert "quality_issues" in rendered
    assert EMAIL in rendered
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
        {"limit": "100", "catalogKey": "catalog"},
        {"limit": "100", "catalogKey": "catalog", "page": "next-token"},
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


def test_workspace_object_key_accepts_live_folder_path_header() -> None:
    client = bare_client()
    client._workspace_object = lambda _workspace, path, **_kwargs: (
        "",
        {"folder": path, "type": "FOLDER"},
    )

    assert client._workspace_object_key("workspace", "/Workspace/medallon") == "/Workspace/medallon"


def test_workspace_object_key_rejects_mismatched_folder_path() -> None:
    client = bare_client()
    client._workspace_object = lambda *_args, **_kwargs: (
        "",
        {"folder": "/Workspace/medallon", "type": "FOLDER"},
    )

    with pytest.raises(AidpProvisionError, match="mismatched workspace object path"):
        client._workspace_object_key(
            "workspace", "/Workspace/medallon/ada@example.com"
        )


def test_workspace_permissions_encode_live_folder_paths() -> None:
    client = bare_client()
    client._workspace_object = lambda _workspace, path, **_kwargs: (
        "",
        {"folder": path, "type": "FOLDER"},
    )
    resources: list[str] = []

    def ensure_permission(resource_path, *_args, **_kwargs):
        resources.append(resource_path)
        return False

    client._ensure_permission = ensure_permission
    client._ensure_permissions(
        "workspace", USER_OCID, "/Workspace/medallon/ada@example.com", "job"
    )

    assert resources == [
        "/workspaces/workspace/objects/%2FWorkspace%2Fmedallon",
        "/workspaces/workspace/objects/%2FWorkspace%2Fmedallon%2Fada%40example.com",
        "/workspaces/workspace/jobs/job",
    ]


def test_notebooks_are_compared_before_create_or_update() -> None:
    client = bare_client()
    expected = {"nbformat": 4, "cells": []}
    normalized = {**expected, "metadata": {"trusted": True}}
    notebooks = {
        "/Workspace/medallon/one@example.com/banking/exact.ipynb": normalized,
        "/Workspace/medallon/one@example.com/banking/drifted.ipynb": {"nbformat": 4, "cells": [1]},
    }
    writes: list[tuple[str, str]] = []
    reads: list[str] = []

    client._workspace_object = lambda _workspace, path, **_kwargs: (
        (b"", {"type": "NOTEBOOK"}) if path in notebooks else (None, {})
    )

    def request(method, path, *, payload=None, **_kwargs):
        target = path.rsplit("/", 1)[-1]
        decoded = unquote(target)
        if method == "POST" and "/actions/export/" in path:
            reads.append(decoded)
            content = notebooks.get(decoded)
            return None if content is None else {"content": content, "format": "ipynb"}
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
    exact_path = "/Workspace/medallon/one@example.com/banking/exact.ipynb"
    drifted_path = "/Workspace/medallon/one@example.com/banking/drifted.ipynb"
    missing_path = "/Workspace/medallon/one@example.com/banking/missing.ipynb"
    assert not client._upload_notebook("workspace", exact_path, expected)
    assert not client._upload_notebook(
        "workspace", drifted_path, expected, repair_drift=False
    )
    assert client._upload_notebook("workspace", drifted_path, expected)
    assert client._upload_notebook("workspace", missing_path, expected)
    assert [method for method, _ in writes] == ["PUT", "POST", "PATCH", "PUT"]
    assert reads.count(missing_path) == 1


@pytest.mark.parametrize("industry", INDUSTRIES)
def test_partial_notebooks_are_repaired_in_place_for_every_industry(industry: str) -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    expected = user_notebooks(industry, key, EMAIL, "bucket", "namespace")
    stored: dict[str, dict] = {}
    calls: dict[str, list[str]] = {name: [] for name in expected}
    client._workspace_object = lambda *_args, **_kwargs: (b"", {"type": "NOTEBOOK"})

    def request(method, path, *, payload=None, **_kwargs):
        target = unquote(path.rsplit("/", 1)[-1])
        name = target.rsplit("/", 1)[-1]
        calls[name].append(method)
        if method == "POST" and "/actions/export/" in path:
            if target not in stored:
                return None
            normalized = json.loads(json.dumps(stored[target]))
            for cell in normalized["cells"]:
                if isinstance(cell.get("source"), list):
                    cell["source"] = "".join(cell["source"])
            return {"content": normalized, "format": "ipynb"}
        if method == "PUT":
            stored[payload["path"]] = payload["content"]
            return None
        raise AssertionError(method)

    client._request = request
    root = workspace_root(EMAIL, industry)
    for name, notebook in expected.items():
        assert client._upload_notebook("workspace", f"{root}/{name}", notebook)
        assert calls[name] == ["POST", "PUT", "POST"]


def test_unreadable_notebook_is_not_overwritten_when_drift_repair_is_disabled() -> None:
    client = bare_client()
    calls: list[str] = []
    client._workspace_object = lambda *_args, **_kwargs: (b"", {"type": "NOTEBOOK"})

    def request(method, *_args, **_kwargs):
        calls.append(method)
        raise AidpProvisionPending("AIDP is still reconciling the requested material.")

    client._request = request
    with pytest.raises(AidpProvisionPending):
        client._upload_notebook(
            "workspace",
            "/Workspace/medallon/one@example.com/banking/exact.ipynb",
            {"nbformat": 4, "cells": []},
            repair_drift=False,
        )
    assert calls == ["POST"]


def test_transient_notebook_export_failure_never_triggers_a_write() -> None:
    client = bare_client()
    calls: list[str] = []
    client._workspace_object = lambda *_args, **_kwargs: (b"", {"type": "NOTEBOOK"})

    def request(method, *_args, **_kwargs):
        calls.append(method)
        raise AidpProvisionPending("AIDP is still reconciling the requested material.")

    client._request = request
    with pytest.raises(AidpProvisionPending):
        client._upload_notebook(
            "workspace",
            "/Workspace/medallon/one@example.com/banking/exact.ipynb",
            {"nbformat": 4, "cells": []},
        )
    assert calls == ["POST"]


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
    contract, changed = client._ensure_catalog_contract("catalog")
    assert changed
    assert set(contract) == set(LAYER_PREFIXES)
    assert len(posts) == 4
    assert client._ensure_catalog_contract("catalog") == (contract, False)
    assert len(posts) == 4


def test_job_is_returned_only_after_exact_four_stage_contract_is_visible() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    root = workspace_root(EMAIL, "banking")
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
        "workspace", "compute", key, "banking", root, notebooks
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

    for task in published["tasks"]:
        task["dependsOn"] = [
            {**dependency, "outcome": None} for dependency in task["dependsOn"]
        ]
        task["cluster"]["clusterName"] = None
    published["jobClusters"][0]["clusterName"] = None

    writes: list[str] = []
    client._request = lambda method, _path, **_kwargs: (
        writes.append(method) if method == "PUT" else published
    )
    assert client._ensure_job("workspace", "compute", key, "banking", root, notebooks) == (
        expected_job_name,
        "job-key",
        False,
    )
    assert writes == []

    client._request = lambda method, _path, **_kwargs: (
        {**published, "tasks": published["tasks"][:-1]} if method == "GET" else None
    )
    with pytest.raises(AidpProvisionPending, match="complete participant workflow") as pending:
        client._ensure_job("workspace", "compute", key, "banking", root, notebooks)
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
        root,
        notebooks,
        repair_drift=False,
    ) == (expected_job_name, "job-key", False)
    assert drift_writes == []
    with pytest.raises(AidpProvisionPending, match="complete participant workflow"):
        client._ensure_job("workspace", "compute", key, "banking", root, notebooks)
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


def test_control_manifest_is_workspace_scoped_idempotent_and_industry_is_immutable() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    control_path = f"/Workspace/medallon/.control/{key}.json"
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

    first = client._ensure_manifest("workspace", key, EMAIL, "banking")
    assert first == {
        "layout_version": 2,
        "participant_key": key,
        "industry": "banking",
        "workspace_path": f"/Workspace/medallon/{EMAIL}/banking",
        "phase": "workspace",
    }
    assert writes == [control_path]
    assert client._ensure_manifest("workspace", key, EMAIL, "banking") == first
    assert writes == [control_path]
    client._advance_manifest("workspace", first, "schemas")
    assert client._ensure_manifest("workspace", key, EMAIL, "banking")["phase"] == "schemas"
    assert writes == [control_path, control_path]
    client._advance_manifest("workspace", first, "schemas")
    assert writes == [control_path, control_path]
    with pytest.raises(AidpProvisionConflict, match="reset the participant environment"):
        client._ensure_manifest("workspace", key, EMAIL, "retail")

    objects[control_path] = (
        b"{invalid-json",
        {"object-key": "control-key", "object-type": "FILE"},
    )
    with pytest.raises(AidpProvisionError, match="control manifest is invalid"):
        client._manifest("workspace", key)


@pytest.mark.parametrize("industry", INDUSTRIES)
def test_provisioning_reconciles_real_inventory_and_repairs_active_drift(industry: str) -> None:
    client = bare_client()
    client.settings = SimpleNamespace(bucket_name="bucket", objectstorage_namespace="namespace")
    key = participant_key(USER_OCID)
    manifest = {
        "layout_version": 2,
        "participant_key": key,
        "industry": industry,
        "workspace_path": f"/Workspace/medallon/{EMAIL}/{industry}",
        "phase": "workspace",
    }
    root = f"/Workspace/medallon/{EMAIL}/{industry}"
    folders: set[str] = set()
    files: dict[str, bytes] = {}
    notebooks: dict[str, dict] = {}
    writes = {"folders": 0, "files": 0, "notebooks": 0, "schemas": 0, "job": 0, "permissions": 0}
    state = {"schemas": False, "job": False, "permissions": False}
    schemas = {layer: {"key": f"schema-{layer}"} for layer in LAYER_PREFIXES}
    job_name = f"wf_{key}_{industry}_medallion"

    client._workspace = lambda: {"key": "workspace"}
    client._shared_compute = lambda _workspace: {"key": "compute"}
    client._catalog = lambda: {"key": "catalog"}

    def ensure_folder(_workspace, path):
        changed = path not in folders
        if changed:
            folders.add(path)
            writes["folders"] += 1
        return changed

    def ensure_manifest(_workspace, _key, _email, _industry):
        assert "/Workspace/medallon/.control" in folders
        return manifest

    def ensure_schemas(_catalog):
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
            client._provision_user(USER_OCID, EMAIL, industry)
        phases.append(pending.value.phase)
    assert phases == ["schemas", "content", "permissions"]
    assert manifest["phase"] == "permissions"
    assert len(folders) == 5
    assert "/Workspace/medallon/.control" in folders
    assert len(files) == 5  # one participant manifest plus four source CSVs
    assert len(notebooks) == 4
    assert all(path.startswith(f"/Workspace/medallon/{EMAIL}/{industry}/") for path in notebooks)
    assert all("/notebooks/" not in path for path in notebooks)

    material = client._provision_user(USER_OCID, EMAIL, industry)
    assert material == UserMaterial(
        EMAIL,
        industry,
        key,
        f"/Workspace/medallon/{EMAIL}/{industry}",
        job_name,
    )
    assert manifest["phase"] == "active"
    completed_writes = writes.copy()
    assert client._provision_user(USER_OCID, EMAIL, industry) == material
    assert writes == completed_writes

    missing_csv = next(path for path in files if path.startswith(f"{root}/source/"))
    del files[missing_csv]
    with pytest.raises(AidpProvisionPending) as pending:
        client._provision_user(USER_OCID, EMAIL, industry)
    assert pending.value.phase == "permissions"
    assert writes["files"] == completed_writes["files"] + 1
    assert client._provision_user(USER_OCID, EMAIL, industry) == material

    folders.remove(f"{root}/source")
    with pytest.raises(AidpProvisionPending) as pending:
        client._provision_user(USER_OCID, EMAIL, industry)
    assert pending.value.phase == "schemas"
    assert client._provision_user(USER_OCID, EMAIL, industry) == material

    state["schemas"] = False
    with pytest.raises(AidpProvisionPending) as pending:
        client._provision_user(USER_OCID, EMAIL, industry)
    assert pending.value.phase == "content"
    assert client._provision_user(USER_OCID, EMAIL, industry) == material

    del notebooks[next(iter(notebooks))]
    with pytest.raises(AidpProvisionPending) as pending:
        client._provision_user(USER_OCID, EMAIL, industry)
    assert pending.value.phase == "permissions"
    assert client._provision_user(USER_OCID, EMAIL, industry) == material

    state["job"] = False
    with pytest.raises(AidpProvisionPending) as pending:
        client._provision_user(USER_OCID, EMAIL, industry)
    assert pending.value.phase == "permissions"
    assert client._provision_user(USER_OCID, EMAIL, industry) == material

    state["permissions"] = False
    with pytest.raises(AidpProvisionPending) as pending:
        client._provision_user(USER_OCID, EMAIL, industry)
    assert pending.value.phase == "permissions"
    assert manifest["phase"] == "active"
    assert client._provision_user(USER_OCID, EMAIL, industry) == material


def test_local_client_is_idempotent_conflict_safe_and_cleanup_is_exact() -> None:
    async def run() -> None:
        client = LocalAidpClient(Settings(local_development_mode=True))
        first = await client.provision_user(USER_OCID, EMAIL, "healthcare")
        second = await client.provision_user(USER_OCID, EMAIL, "healthcare")
        assert first == second
        assert len(client.users) == 1
        with pytest.raises(AidpProvisionConflict, match="delete and recreate"):
            await client.provision_user(USER_OCID, EMAIL, "retail")
        operation_id = "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43"
        reset = await client.reset_user(USER_OCID, EMAIL, "retail", operation_id)
        assert reset.industry == "retail"
        assert await client.reset_user(USER_OCID, EMAIL, "retail", operation_id) == reset
        assert await client.list_user_industries([USER_OCID]) == {USER_OCID: "retail"}
        with pytest.raises(AidpProvisionConflict, match="another industry"):
            await client.reset_user(USER_OCID, EMAIL, "banking", operation_id)
        await client.cleanup_user(USER_OCID)
        assert client.users == {}
        assert client._reset_operations == {}

    asyncio.run(run())


@pytest.mark.parametrize("target_industry", INDUSTRIES)
def test_reset_journal_changes_industry_and_same_operation_never_cleans_twice(
    target_industry: str,
) -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    operation_id = "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43"
    manifest = {
        "layout_version": 2,
        "participant_key": key,
        "industry": "banking",
        "workspace_path": workspace_root(EMAIL, "banking"),
        "phase": "active",
    }
    cleanups: list[bool] = []
    deleted_paths: list[str] = []
    writes: list[str] = []
    client._workspace = lambda: {"key": "workspace"}
    client._ensure_workspace_layout = lambda *_args: False
    client._manifest = lambda *_args: manifest
    client._write_manifest = lambda _workspace, _key, value: writes.append(value["reset"]["phase"])
    client._cleanup_user = lambda _key, preserve_manifest=False: cleanups.append(preserve_manifest)
    client._delete_workspace_path = lambda _workspace, path, _message: deleted_paths.append(path)

    def provision(_ocid, email, industry):
        manifest["phase"] = "active"
        return UserMaterial(
            email,
            industry,
            key,
            workspace_root(email, industry),
            f"wf_{key}_{industry}_medallion",
        )

    client._provision_user = provision
    first = client._reset_user(USER_OCID, EMAIL, target_industry, operation_id)
    second = client._reset_user(USER_OCID, EMAIL, target_industry, operation_id)

    assert first == second
    assert cleanups == [True]
    assert deleted_paths == ["/Workspace/lab-users/ada@example.com"]
    assert writes == ["cleanup", "provision", "complete"]
    assert manifest["industry"] == target_industry
    assert manifest["reset"] == {
        "operation_id": operation_id,
        "target_industry": target_industry,
        "target_workspace_path": workspace_root(EMAIL, target_industry),
        "phase": "complete",
    }


def test_reset_reports_destructive_reconciliation_as_cleanup() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    manifest = {
        "layout_version": 2,
        "participant_key": key,
        "industry": "banking",
        "workspace_path": workspace_root(EMAIL, "banking"),
        "phase": "active",
    }
    client._workspace = lambda: {"key": "workspace"}
    client._ensure_workspace_layout = lambda *_args: False
    client._manifest = lambda *_args: manifest
    client._write_manifest = lambda *_args: None

    def pending_cleanup(*_args, **_kwargs):
        raise AidpProvisionPending("workflow deletion is still running", "content")

    client._cleanup_user = pending_cleanup
    with pytest.raises(AidpProvisionPending, match="workflow deletion") as pending:
        client._reset_user(
            USER_OCID,
            EMAIL,
            "retail",
            "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43",
        )
    assert pending.value.phase == "cleanup"


def test_reset_rejects_a_different_operation_while_cleanup_is_running() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    manifest = {
        "layout_version": 2,
        "participant_key": key,
        "industry": "banking",
        "workspace_path": workspace_root(EMAIL, "banking"),
        "phase": "active",
        "reset": {
            "operation_id": "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43",
            "target_industry": "retail",
            "target_workspace_path": workspace_root(EMAIL, "retail"),
            "phase": "cleanup",
        },
    }
    client._workspace = lambda: {"key": "workspace"}
    client._ensure_workspace_layout = lambda *_args: False
    client._manifest = lambda *_args: manifest
    with pytest.raises(AidpProvisionConflict, match="already in progress"):
        client._reset_user(
            USER_OCID,
            EMAIL,
            "retail",
            "2ea8cabb-77b5-4d42-80a2-514503510ce8",
        )


def test_user_industries_list_jobs_once_and_fall_back_to_pending_manifest() -> None:
    client = bare_client()
    second_ocid = "ocid1.user.oc1..bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    first_key = participant_key(USER_OCID)
    second_key = participant_key(second_ocid)
    calls: list[str] = []
    client._workspace = lambda: {"key": "workspace"}

    def listed(path, **_kwargs):
        calls.append(path)
        return [{"displayName": f"wf_{first_key}_banking_medallion", "key": "job"}]

    client._list = listed
    client._manifest = lambda _workspace, key: (
        {
            "layout_version": 2,
            "participant_key": second_key,
            "industry": "healthcare",
            "workspace_path": workspace_root("grace@example.com", "healthcare"),
            "phase": "content",
        }
        if key == second_key
        else None
    )
    client._workspace_json = lambda *_args: None

    assert client._user_industries([USER_OCID, second_ocid]) == {
        USER_OCID: "banking",
        second_ocid: "healthcare",
    }
    assert calls == ["/workspaces/workspace/jobs"]


def test_shared_schema_cleanup_deletes_only_exact_participant_tables() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    schemas = {
        layer: {"displayName": schema_name(layer), "key": f"schema-{layer}"}
        for layer in LAYER_PREFIXES
    }
    tables = {
        schema["key"]: [
            {
                "displayName": table_names(key, "banking", layer)[0],
                "key": f"{schema['key']}-participant",
            },
            {
                "displayName": table_name("u_ffffffffffffffff", "banking", "branches"),
                "key": f"{schema['key']}-other",
            },
        ]
            for layer, schema in schemas.items()
    }
    deleted: list[str] = []
    client._list = lambda path, *, params=None, **_kwargs: (
        list(schemas.values()) if path == "/schemas" else tables[params["schemaKey"]]
    )
    client._request = lambda method, path, **_kwargs: deleted.append(path)

    with pytest.raises(AidpProvisionPending, match="table deletion"):
        client._cleanup_tables("catalog", key)
    assert set(deleted) == {
        f"/tables/{schema['key']}-participant" for schema in schemas.values()
    }
    assert not any(path.startswith("/schemas/") for path in deleted)

    for values in tables.values():
        values[:] = [table for table in values if table["key"].endswith("-other")]
    client._cleanup_tables("catalog", key)


def test_cleanup_uses_manifest_workspace_path_and_deletes_control_last() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    workspace_path = workspace_root(EMAIL, "banking")
    calls: list[str] = []
    client._workspace = lambda: {"key": "workspace"}
    client._catalog = lambda: {"key": "catalog"}
    client._manifest = lambda _workspace, _key: {
        "layout_version": 2,
        "participant_key": key,
        "industry": "banking",
        "workspace_path": workspace_path,
    }
    client._cleanup_jobs = lambda *_args: calls.append("jobs")
    client._cleanup_tables = lambda *_args: calls.append("tables")
    client._cleanup_legacy_tables = lambda *_args: calls.append("legacy_tables")
    client._cleanup_legacy_schemas = lambda *_args: calls.append("legacy")
    client._cleanup_object_storage = lambda *_args: calls.append("storage")
    client._delete_workspace_path = lambda _workspace, path, _message: calls.append(path)

    client._cleanup_user(key)

    assert calls[:5] == ["jobs", "tables", "legacy_tables", "legacy", "storage"]
    assert calls[5] == workspace_path.rsplit("/", 1)[0]
    assert calls[-2:] == [
        client._control_manifest_path(key),
        client._legacy_control_manifest_path(key),
    ]


def test_reset_cleanup_preserves_only_the_current_control_manifest() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    calls: list[str] = []
    client._workspace = lambda: {"key": "workspace"}
    client._catalog = lambda: {"key": "catalog"}
    client._manifest = lambda *_args: {
        "layout_version": 2,
        "participant_key": key,
        "industry": "banking",
        "workspace_path": workspace_root(EMAIL, "banking"),
    }
    client._cleanup_jobs = lambda *_args: None
    client._cleanup_tables = lambda *_args: None
    client._cleanup_legacy_tables = lambda *_args: None
    client._cleanup_legacy_schemas = lambda *_args: None
    client._cleanup_object_storage = lambda *_args: None
    client._delete_workspace_path = lambda _workspace, path, _message: calls.append(path)

    client._cleanup_user(key, preserve_manifest=True)

    assert client._control_manifest_path(key) not in calls
    assert client._legacy_control_manifest_path(key) in calls


def test_cleanup_rejects_untrusted_manifest_workspace_path() -> None:
    client = bare_client()
    key = participant_key(USER_OCID)
    client._workspace = lambda: {"key": "workspace"}
    client._manifest = lambda _workspace, _key: {
        "layout_version": 2,
        "participant_key": key,
        "industry": "banking",
        "workspace_path": "/Workspace/Shared/not-ours/banking",
    }
    with pytest.raises(AidpProvisionError, match="exact workspace path"):
        client._cleanup_user(key)


def test_async_entrypoints_use_participant_lock_and_to_thread(monkeypatch) -> None:
    client = bare_client()
    calls: list[tuple[str, tuple]] = []

    def provision(*args):
        return args

    def cleanup(*args):
        calls.append(("cleanup_impl", args))

    def reset(*args):
        return args

    def industries(*args):
        return {args[0][0]: "banking"}

    def health():
        calls.append(("health_impl", ()))

    client._provision_user = provision
    client._cleanup_user = cleanup
    client._reset_user = reset
    client._user_industries = industries
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
        assert await client.reset_user(
            USER_OCID,
            EMAIL,
            "retail",
            "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43",
        ) == (
            USER_OCID,
            EMAIL,
            "retail",
            "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43",
        )
        assert await client.list_user_industries([USER_OCID]) == {USER_OCID: "banking"}
        await client.healthcheck()

    asyncio.run(run())
    assert participant_key(USER_OCID) in client._locks
    assert ("provision", (USER_OCID, EMAIL, "banking")) in calls
    assert ("cleanup", (participant_key(USER_OCID),)) in calls
    assert (
        "reset",
        (
            USER_OCID,
            EMAIL,
            "retail",
            "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43",
        ),
    ) in calls
    assert ("industries", ([USER_OCID],)) in calls
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
        "https://datalake.us-chicago-1.oci.oraclecloud.com/20240831/"
        "dataLakes/platform"
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
