from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest


MODULE_PATH = Path(__file__).parents[1] / "hooks" / "post_apply.py"
SPEC = importlib.util.spec_from_file_location("post_apply", MODULE_PATH)
post_apply = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = post_apply
SPEC.loader.exec_module(post_apply)


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []
        self.list_calls: list[tuple[str, dict | None]] = []
        self.actions: dict[str, dict] = {}
        self.workspace_objects: dict[str, str] = {}
        self.resources = {
            "/workspaces": [
                {
                    "displayName": "ws",
                    "key": "ws-key",
                    "type": "DEFAULT",
                    "lifecycleState": "ACTIVE",
                }
            ],
            "/catalogs": [],
            "/schemas": [],
            "/volumes": [],
            "/roles": [],
            "/workspaces/ws-key/clusters": [],
        }

    def list_all(self, path: str, *, params=None) -> list[dict]:
        self.list_calls.append((path, params))
        if path.endswith("/permissions"):
            return list(self.actions.get(path, []))
        items = list(self.resources[path])
        if params and params.get("displayName"):
            items = [item for item in items if item.get("displayName") == params["displayName"]]
        return items

    def request(
        self,
        method: str,
        path: str,
        *,
        payload=None,
        params=None,
        data=None,
        headers=None,
    ):
        self.calls.append((method, path, payload, params))
        if method == "POST" and path in self.resources:
            item = dict(payload)
            name = payload["displayName"]
            if path == "/catalogs":
                item["key"] = f"{name}-key"
            elif path == "/schemas":
                item["key"] = f"{payload['catalogName']}.{name}"
            elif path == "/volumes":
                item["key"] = f"{payload['catalogName']}.{payload['schemaName']}.{name}"
            else:
                item["key"] = f"{name}-key"
            if path in {"/catalogs", "/schemas", "/volumes", "/workspaces/ws-key/clusters"}:
                item["lifecycleState"] = "ACTIVE"
            self.resources[path].append(item)
            return post_apply.ApiResponse(201, item, {})
        if method in {"POST", "PUT"} and "/actions/" in path:
            base_path = path.split("/actions/", 1)[0]
            if path.endswith("/addMember"):
                self.actions[base_path] = {"assignees": payload["assignees"]}
            else:
                inspect_path = f"{base_path}/permissions"
                details = next(iter(payload.values()))
                assignees = details["assignees"]
                targets = assignees["targets"]
                self.actions.setdefault(inspect_path, []).extend(
                    [
                    {
                        "grantee": target,
                        "granteeName": target,
                        "granteeType": assignees["type"],
                        "granteePermissions": details["permissions"],
                        "isPermissionsInheritable": details.get(
                            "isPermissionsInheritable"
                        ),
                    }
                    for target in targets
                    ]
                )
                if "/clusters/" in base_path:
                    resource_type, resource_key = "CLUSTER", base_path.rsplit("/", 1)[-1]
                elif "/objects/" in base_path:
                    resource_type, resource_key = "FOLDER", base_path.rsplit("/", 1)[-1]
                elif base_path.startswith("/workspaces/"):
                    resource_type, resource_key = "WORKSPACE", base_path.rsplit("/", 1)[-1]
                else:
                    resource_type, resource_key = "CATALOG", base_path.rsplit("/", 1)[-1]
                for target in targets:
                    role_key = f"{target}-key"
                    self.actions.setdefault(f"/roles/{role_key}/permissions", []).append(
                        {
                            "roleKey": role_key,
                            "permissionsWithResourceDetails": {
                                "permissions": details["permissions"],
                                "resourceType": resource_type,
                                "resourceKey": resource_key,
                            },
                        }
                    )
            return post_apply.ApiResponse(200, {}, {})
        if method == "POST" and path == "/workspaces/ws-key/objects":
            object_path = headers["path"]
            self.workspace_objects[object_path] = "lab-users-key"
            return post_apply.ApiResponse(201, None, {"object-key": "lab-users-key"})
        if method == "GET" and path.startswith("/workspaces/ws-key/objects/"):
            object_path = unquote(path.rsplit("/", 1)[-1])
            object_key = self.workspace_objects.get(object_path)
            if not object_key:
                raise post_apply.ApiRequestError(method, path, 404, "request-id")
            return post_apply.ApiResponse(200, None, {"object-key": object_key})
        if method == "GET" and path.startswith("/schemas/"):
            key = path.removeprefix("/schemas/")
            item = next(item for item in self.resources["/schemas"] if item["key"] == key)
            return post_apply.ApiResponse(200, item, {})
        if method == "GET" and path.startswith("/volumes/") and not path.endswith("/permissions"):
            key = path.removeprefix("/volumes/")
            item = next(item for item in self.resources["/volumes"] if item["key"] == key)
            return post_apply.ApiResponse(200, item, {})
        if method == "GET":
            return post_apply.ApiResponse(200, self.actions.get(path, {}), {})
        return post_apply.ApiResponse(200, {}, {})


def test_reconcile_builds_fresh_only_rbac_without_global_schemas_or_volumes(monkeypatch) -> None:
    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)
    api = FakeApi()
    outputs = {
        "default_workspace_name": "ws",
        "objectstorage_namespace": "namespace",
        "bucket_name": "aidp-data-test",
        "developer_group_ocid": "ocid1.group.developer",
        "pending_group_ocid": "ocid1.group.pending",
        "provisioner_group_ocid": "ocid1.group.provisioner",
    }
    reconciled, events = post_apply.reconcile(api, outputs)
    assert reconciled["catalog_key"] == "aidp_lab-key"
    assert reconciled["catalog_name"] == "aidp_lab"
    assert reconciled["global_schema_count"] == 0
    assert reconciled["external_volume_count"] == 0
    assert not any(
        method == "POST" and path in {"/schemas", "/volumes"}
        for method, path, _, _ in api.calls
    )
    schema_queries = [params for path, params in api.list_calls if path == "/schemas"]
    volume_queries = [params for path, params in api.list_calls if path == "/volumes"]
    assert all(query["catalogKey"] == "aidp_lab-key" for query in schema_queries)
    assert volume_queries == [{"catalogKey": "aidp_lab-key"}]
    role_queries = [params for path, params in api.list_calls if path == "/roles"]
    assert {query["displayName"] for query in role_queries} == {
        "AIDP_LAB_DEVELOPER",
        "AIDP_LAB_PENDING",
        "AIDP_LAB_PROVISIONER",
    }
    assert any("zero global schemas and zero external volumes" in event for event in events)
    cluster_payloads = [payload for method, path, payload, _ in api.calls if method == "POST" and path.endswith("/clusters")]
    assert cluster_payloads[0]["displayName"] == "aidp_lab_shared_compute"
    assert cluster_payloads[0]["type"] == "USER"
    assert cluster_payloads[0]["driverConfig"]["driverShape"] == "amd.generic"
    assert cluster_payloads[0]["workerConfig"]["maxWorkerCount"] == 10
    assert reconciled["shared_compute_key"] == "aidp_lab_shared_compute-key"
    assert reconciled["root_object_key"] == "lab-users-key"
    workspace_permissions = api.actions["/workspaces/ws-key/permissions"]
    assert {
        (item["grantee"], tuple(item["granteePermissions"]))
        for item in workspace_permissions
    } == {
        ("AIDP_LAB_DEVELOPER", ("USER",)),
        ("AIDP_LAB_PENDING", ("USER",)),
        ("AIDP_LAB_PROVISIONER", ("USER",)),
    }
    catalog_permissions = api.actions["/catalogs/aidp_lab-key/permissions"]
    assert {
        (item["grantee"], tuple(item["granteePermissions"]))
        for item in catalog_permissions
    } == {
        ("AIDP_LAB_DEVELOPER", ("SELECT",)),
        ("AIDP_LAB_PROVISIONER", ("ADMIN",)),
    }
    compute_permissions = api.actions[
        "/workspaces/ws-key/clusters/aidp_lab_shared_compute-key/permissions"
    ]
    assert {item["grantee"] for item in compute_permissions} == {
        "AIDP_LAB_DEVELOPER",
        "AIDP_LAB_PROVISIONER",
    }
    root_permissions = api.actions[
        "/workspaces/ws-key/objects/lab-users-key/permissions"
    ]
    assert root_permissions == [
        {
            "grantee": "AIDP_LAB_PROVISIONER",
            "granteeName": "AIDP_LAB_PROVISIONER",
            "granteeType": "ROLE",
            "granteePermissions": ["ADMIN"],
            "isPermissionsInheritable": True,
        }
    ]


def test_fresh_only_rejects_legacy_overlapping_volume_without_deleting() -> None:
    api = FakeApi()
    api.resources["/volumes"] = [
        {
            "displayName": "landing_data",
            "key": "legacy-volume",
            "volumeType": "EXTERNAL",
            "storageLocation": "oci://aidp-data-test@namespace/01_landing/",
        }
    ]
    with pytest.raises(post_apply.ReconcileError, match="overlapping medallion paths"):
        post_apply.assert_fresh_catalog(
            api, "aidp_lab-key", "namespace", "aidp-data-test"
        )
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


def test_fresh_only_rejects_global_medallion_schema_without_deleting() -> None:
    api = FakeApi()
    api.resources["/schemas"] = [
        {"displayName": "landing", "key": "legacy-schema"}
    ]
    with pytest.raises(post_apply.ReconcileError, match="legacy global schemas"):
        post_apply.assert_fresh_catalog(
            api, "aidp_lab-key", "namespace", "aidp-data-test"
        )
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


def test_object_prefixes_are_created_only_when_missing() -> None:
    class Client:
        def __init__(self) -> None:
            self.objects = {"02_bronze/"}

        def head_object(self, namespace, bucket, name):
            if name not in self.objects:
                import oci

                raise oci.exceptions.ServiceError(404, "NotFound", {}, "missing")

        def put_object(self, namespace, bucket, name, body, *, content_type):
            assert body == b""
            assert content_type == "application/x-directory"
            self.objects.add(name)

    client = Client()
    events = post_apply.ensure_object_prefixes(client, "namespace", "bucket")
    assert client.objects == {"01_landing/", "02_bronze/", "03_silver/", "04_gold/"}
    assert "Object Storage prefix 02_bronze/ reused" in events


def test_existing_incompatible_catalog_is_never_replaced() -> None:
    api = FakeApi()
    api.resources["/catalogs"] = [
        {"displayName": "aidp_lab", "catalogType": "EXTERNAL", "key": "bad", "lifecycleState": "ACTIVE"}
    ]
    try:
        post_apply.ensure_resource(
            api,
            "/catalogs",
            "catalog",
            "aidp_lab",
            {"displayName": "aidp_lab", "catalogType": "INTERNAL"},
            {"catalogType": "INTERNAL"},
            wait_for_active=True,
        )
    except post_apply.ReconcileError as exc:
        assert "incompatible" in str(exc)
    else:
        raise AssertionError("incompatible catalog must fail")
    assert not any(method == "DELETE" for method, _, _, _ in api.calls)


def test_async_resource_waits_for_active(monkeypatch) -> None:
    api = FakeApi()
    api.resources["/catalogs"] = [
        {"displayName": "aidp_lab", "catalogType": "INTERNAL", "key": "catalog", "lifecycleState": "CREATING"}
    ]
    sleeps: list[int] = []

    def activate(_: int) -> None:
        sleeps.append(1)
        if len(sleeps) == 25:
            api.resources["/catalogs"][0]["lifecycleState"] = "ACTIVE"

    monkeypatch.setattr(post_apply.time, "sleep", activate)
    resource, created = post_apply.ensure_resource(
        api,
        "/catalogs",
        "catalog",
        "aidp_lab",
        {"displayName": "aidp_lab", "catalogType": "INTERNAL"},
        {"catalogType": "INTERNAL"},
        wait_for_active=True,
    )
    assert resource["lifecycleState"] == "ACTIVE"
    assert created is False
    assert len(sleeps) == 25


def test_async_resource_terminal_state_fails() -> None:
    api = FakeApi()
    api.resources["/volumes"] = [{"displayName": "landing_data", "lifecycleState": "DELETED"}]
    try:
        post_apply.ensure_resource(
            api,
            "/volumes",
            "volume",
            "landing_data",
            {"displayName": "landing_data"},
            {},
            wait_for_active=True,
        )
    except post_apply.ReconcileError as exc:
        assert "terminal state DELETED" in str(exc)
    else:
        raise AssertionError("terminal lifecycle state must fail")


def test_permission_action_must_be_observable(monkeypatch) -> None:
    api = FakeApi()
    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)
    inspect_path = "/catalogs/key/permissions"
    changed = post_apply.ensure_action(
        api,
        "POST",
        "/catalogs/key/actions/managePermission",
        {
            "assignCatalogPermissionDetails": {
                "assignees": {"type": "ROLE", "targets": ["AIDP_LAB_DEVELOPER"]},
                "permissions": ["SELECT"],
            }
        },
        lambda: post_apply.permission_is_assigned(
            api, inspect_path, "AIDP_LAB_DEVELOPER", "SELECT"
        ),
        attempts=1,
    )
    assert changed is True


def test_new_role_with_null_assignees_has_no_group() -> None:
    class Api:
        @staticmethod
        def request(method, path):
            return post_apply.ApiResponse(200, {"assignees": None}, {})

    assert not post_apply.role_has_group(Api(), "role-key", "group-id")


def test_role_readiness_rejects_extra_member() -> None:
    class Api:
        @staticmethod
        def request(method, path):
            return post_apply.ApiResponse(
                200,
                {
                    "assignees": [
                        {"type": "GROUP", "target": "expected"},
                        {"type": "USER", "target": "unexpected"},
                    ]
                },
                {},
            )

    with pytest.raises(post_apply.ReconcileError, match="unexpected members"):
        post_apply.assert_role_members_exact(
            Api(), "role-key", "AIDP_LAB_PROVISIONER", "GROUP", "expected"
        )


def test_role_readiness_rejects_master_catalog_or_broader_permissions() -> None:
    class Api:
        @staticmethod
        def list_all(path, *, params=None):
            assert params == {"permissionScope": "DIRECT"}
            return [
                {
                    "roleKey": "role-key",
                    "permissionsWithResourceDetails": {
                        "permissions": ["ADMIN"],
                        "resourceType": "MASTER_CATALOG",
                        "resourceKey": "master",
                    },
                }
            ]

    with pytest.raises(post_apply.ReconcileError, match="unexpected direct permissions"):
        post_apply.assert_role_permissions_exact(
            Api(),
            "role-key",
            "AIDP_LAB_PROVISIONER",
            {("WORKSPACE", "workspace-key", frozenset({"USER"}))},
        )


def test_resource_readiness_rejects_broader_direct_permission() -> None:
    class Api:
        @staticmethod
        def list_all(path):
            return [
                {
                    "grantee": "AIDP_LAB_PROVISIONER",
                    "granteeType": "ROLE",
                    "granteePermissions": ["USE", "ADMIN"],
                }
            ]

    with pytest.raises(post_apply.ReconcileError, match="conflicting direct permission"):
        post_apply.permission_is_assigned(
            Api(), "/clusters/key/permissions", "AIDP_LAB_PROVISIONER", "USE"
        )


def test_permission_conflict_is_not_treated_as_success(monkeypatch) -> None:
    class ConflictApi:
        @staticmethod
        def request(method, path, *, payload=None):
            raise post_apply.ApiRequestError(method, path, 409, "request-id")

    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)
    try:
        post_apply.ensure_action(
            ConflictApi(),
            "POST",
            "/catalogs/key/actions/managePermission",
            {"permissions": ["SELECT"]},
            lambda: False,
            attempts=1,
        )
    except post_apply.ReconcileError as exc:
        assert "did not converge" in str(exc)
    else:
        raise AssertionError("unverified 409 must fail")


def test_api_retries_429(monkeypatch) -> None:
    class Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.headers = {}
            self.content = b"{}"

        @staticmethod
        def json() -> dict:
            return {}

    class Session:
        def __init__(self) -> None:
            self.statuses = [429, 200]

        def request(self, *args, **kwargs):
            return Response(self.statuses.pop(0))

    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)
    api = post_apply.AidpApi("us-chicago-1", "platform", object(), "deployment")
    api.session = Session()
    response = api.request("GET", "/catalogs")
    assert response.status_code == 200


def test_post_retry_token_uses_canonical_payload_hash() -> None:
    observed: list[str] = []

    class Response:
        status_code = 200
        headers = {}
        content = b"{}"

        @staticmethod
        def json() -> dict:
            return {}

    class Session:
        @staticmethod
        def request(*args, **kwargs):
            observed.append(kwargs["headers"]["opc-retry-token"])
            return Response()

    api = post_apply.AidpApi("us-chicago-1", "platform", object(), "deployment")
    api.session = Session()
    api.request("POST", "/schemas", payload={"displayName": "landing", "catalogName": "aidp_lab"})
    api.request("POST", "/schemas", payload={"catalogName": "aidp_lab", "displayName": "landing"})
    api.request("POST", "/schemas", payload={"displayName": "bronze", "catalogName": "aidp_lab"})
    api.request(
        "POST",
        "/schemas",
        payload={"displayName": "bronze", "catalogName": "aidp_lab"},
        headers={"type": "FOLDER"},
    )
    assert observed[0] == observed[1]
    assert observed[0] != observed[2]
    assert observed[2] != observed[3]


def test_stopped_shared_compute_is_reusable_after_auto_termination() -> None:
    assert post_apply.is_active_or_raise({"lifecycleState": "STOPPED"}, "shared compute")


def test_run_command_returns_only_public_key_and_fingerprint() -> None:
    fingerprint = ":".join(f"{index:02x}" for index in range(16))
    public_key = "-----BEGIN PUBLIC KEY-----\nQUJD\n-----END PUBLIC KEY-----\n"

    class Model:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    models = SimpleNamespace(
        CreateInstanceAgentCommandDetails=Model,
        InstanceAgentCommandTarget=Model,
        InstanceAgentCommandContent=Model,
        InstanceAgentCommandSourceViaTextDetails=Model,
        InstanceAgentCommandOutputViaTextDetails=Model,
    )
    oci_module = SimpleNamespace(
        compute_instance_agent=SimpleNamespace(models=models),
        exceptions=SimpleNamespace(ServiceError=RuntimeError),
    )

    class Client:
        details = None

        def create_instance_agent_command(self, details):
            self.details = details
            return SimpleNamespace(data=SimpleNamespace(id="command-id"))

        @staticmethod
        def get_instance_agent_command_execution(command_id, instance_id):
            assert (command_id, instance_id) == ("command-id", "instance-id")
            return SimpleNamespace(
                data=SimpleNamespace(
                    lifecycle_state="SUCCEEDED",
                    content=SimpleNamespace(
                        text=f"{public_key}FINGERPRINT={fingerprint}\n"
                    ),
                )
            )

    client = Client()
    assert post_apply.fetch_provisioner_public_key(
        client, oci_module, "compartment-id", "instance-id"
    ) == (public_key, fingerprint)
    assert "exec sudo /usr/local/sbin/aidp-lab-public-key" in client.details.content.source.text
    assert client.details.execution_time_out_in_seconds == 660
    assert "PRIVATE" not in client.details.content.source.text


def test_run_command_waits_for_instance_agent_policy_propagation(monkeypatch) -> None:
    fingerprint = ":".join(f"{index:02x}" for index in range(16))

    class ServiceError(Exception):
        status = 404

    class Model:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    models = SimpleNamespace(
        CreateInstanceAgentCommandDetails=Model,
        InstanceAgentCommandTarget=Model,
        InstanceAgentCommandContent=Model,
        InstanceAgentCommandSourceViaTextDetails=Model,
        InstanceAgentCommandOutputViaTextDetails=Model,
    )
    oci_module = SimpleNamespace(
        compute_instance_agent=SimpleNamespace(models=models),
        exceptions=SimpleNamespace(ServiceError=ServiceError),
    )

    class Client:
        polls = 0

        @staticmethod
        def create_instance_agent_command(details):
            return SimpleNamespace(data=SimpleNamespace(id="command-id"))

        def get_instance_agent_command_execution(self, command_id, instance_id):
            self.polls += 1
            if self.polls < 3:
                raise ServiceError()
            return SimpleNamespace(
                data=SimpleNamespace(
                    lifecycle_state="SUCCEEDED",
                    content=SimpleNamespace(
                        text=(
                            "-----BEGIN PUBLIC KEY-----\nQUJD\n-----END PUBLIC KEY-----\n"
                            f"FINGERPRINT={fingerprint}\n"
                        )
                    ),
                )
            )

    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)
    client = Client()
    post_apply.fetch_provisioner_public_key(
        client, oci_module, "compartment-id", "instance-id", attempts=3
    )
    assert client.polls == 3


def test_api_key_rotation_deletes_previous_keys_and_verifies_exact_fingerprint() -> None:
    expected = ":".join(f"{index:02x}" for index in range(16))

    class IdentityClient:
        def __init__(self) -> None:
            self.keys = [SimpleNamespace(fingerprint="old:fingerprint")]
            self.deleted: list[str] = []
            self.uploaded = ""

        def list_api_keys(self, user_ocid):
            assert user_ocid == "ocid1.user.provisioner"
            return SimpleNamespace(data=list(self.keys))

        def delete_api_key(self, user_ocid, fingerprint):
            assert user_ocid == "ocid1.user.provisioner"
            self.deleted.append(fingerprint)
            self.keys = [item for item in self.keys if item.fingerprint != fingerprint]

        def upload_api_key(self, user_ocid, details):
            assert user_ocid == "ocid1.user.provisioner"
            self.uploaded = details.key
            item = SimpleNamespace(fingerprint=expected)
            self.keys = [item]
            return SimpleNamespace(data=item)

    class CreateApiKeyDetails:
        def __init__(self, *, key) -> None:
            self.key = key

    pagination = SimpleNamespace(
        list_call_get_all_results=lambda function, *args: function(*args)
    )
    oci_module = SimpleNamespace(
        pagination=pagination,
        identity=SimpleNamespace(
            models=SimpleNamespace(CreateApiKeyDetails=CreateApiKeyDetails)
        ),
    )
    identity = IdentityClient()
    public_key = "-----BEGIN PUBLIC KEY-----\nQUJD\n-----END PUBLIC KEY-----\n"
    assert post_apply.rotate_provisioner_api_key(
        identity,
        oci_module,
        "ocid1.user.provisioner",
        public_key,
        expected,
    ) == expected
    assert identity.deleted == ["old:fingerprint"]
    assert identity.uploaded == public_key
    assert "PRIVATE" not in identity.uploaded


def test_permission_verification_paginates_and_correlates_one_item() -> None:
    class Response:
        status_code = 200
        content = b"{}"

        def __init__(self, items: list[dict], next_page: str | None = None) -> None:
            self._items = items
            self.headers = {"opc-next-page": next_page} if next_page else {}

        def json(self) -> dict:
            return {"items": self._items}

    class Session:
        def request(self, *args, **kwargs):
            if kwargs["params"].get("page") == "second":
                return Response(
                    [
                        {
                            "grantee": "ANOTHER_ROLE",
                            "granteeName": "ANOTHER_ROLE",
                            "granteeType": "ROLE",
                            "granteePermissions": ["SELECT"],
                        }
                    ]
                )
            return Response(
                [
                    {
                        "grantee": "AIDP_LAB_DEVELOPER",
                        "granteeName": "AIDP_LAB_DEVELOPER",
                        "granteeType": "ROLE",
                        "granteePermissions": ["READ"],
                    }
                ],
                "second",
            )

    api = post_apply.AidpApi("us-chicago-1", "platform", object(), "deployment")
    api.session = Session()
    with pytest.raises(post_apply.ReconcileError, match="conflicting direct permission"):
        post_apply.permission_is_assigned(
            api, "/catalogs/key/permissions", "AIDP_LAB_DEVELOPER", "SELECT"
        )


def test_workspace_waits_until_active(monkeypatch) -> None:
    api = FakeApi()
    api.resources["/workspaces"][0]["lifecycleState"] = "CREATING"
    sleeps: list[int] = []

    def activate(_: int) -> None:
        sleeps.append(1)
        if len(sleeps) == 25:
            api.resources["/workspaces"][0]["lifecycleState"] = "ACTIVE"

    monkeypatch.setattr(post_apply.time, "sleep", activate)
    workspace = post_apply.wait_for_existing_active(
        api,
        "/workspaces",
        "workspace",
        "ws",
        {"type": "DEFAULT"},
    )
    assert workspace["lifecycleState"] == "ACTIVE"
    assert len(sleeps) == 25


def test_aidp_api_uses_quick_start_production_endpoint() -> None:
    api = post_apply.AidpApi("us-chicago-1", "ocid1.aidataplatform.test", object(), "deployment")
    assert api.base == (
        "https://aidpprod.us-chicago-1.oci.oraclecloud.com/20260430/"
        "aiDataPlatforms/ocid1.aidataplatform.test"
    )


def test_application_health_uses_self_signed_https(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, str]:
            return {"status": "ok"}

    class Session:
        @staticmethod
        def get(url, *, timeout, verify):
            observed.update(url=url, timeout=timeout, verify=verify)
            return Response()

    monkeypatch.setattr(post_apply.requests, "Session", Session)
    post_apply.wait_for_application("https://192.0.2.10")
    assert observed["url"] == "https://192.0.2.10/api/health"
    assert observed["verify"] is False


def test_workbench_url_uses_oci_web_socket_endpoint() -> None:
    assert post_apply.workbench_url(
        {
            "aidp_web_socket_endpoint": "1yjfbzshsbc4glmdavcord",
            "tenancy_name": "oci-deploy-1",
            "identity_domain_name": "Default",
        }
    ) == "https://1yjfbzshsbc4glmdavcord.datalake.oci.oraclecloud.com#?tenant=oci-deploy-1&domain=Default"
