from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


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

    def request(self, method: str, path: str, *, payload=None, params=None):
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
                self.actions[inspect_path] = [
                    {
                        "grantee": target,
                        "granteeName": target,
                        "granteeType": assignees["type"],
                        "granteePermissions": details["permissions"],
                    }
                    for target in assignees["targets"]
                ]
            return post_apply.ApiResponse(200, {}, {})
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


def test_reconcile_builds_canonical_medallion_resources(monkeypatch) -> None:
    monkeypatch.setattr(post_apply.time, "sleep", lambda _: None)
    api = FakeApi()
    outputs = {
        "default_workspace_name": "ws",
        "objectstorage_namespace": "namespace",
        "bucket_name": "aidp-data-test",
        "developer_group_ocid": "ocid1.group.test",
    }
    reconciled, events = post_apply.reconcile(api, outputs)
    assert reconciled["catalog_key"] == "aidp_lab-key"
    assert set(reconciled["schema_keys"]) == {"landing", "bronze", "silver", "gold"}
    volume_payloads = [payload for method, path, payload, _ in api.calls if method == "POST" and path == "/volumes"]
    assert [item["storageLocation"] for item in volume_payloads] == [
        "oci://aidp-data-test@namespace/01_landing/",
        "oci://aidp-data-test@namespace/02_bronze/",
        "oci://aidp-data-test@namespace/03_silver/",
        "oci://aidp-data-test@namespace/04_gold/",
    ]
    schema_queries = [params for path, params in api.list_calls if path == "/schemas"]
    volume_queries = [params for path, params in api.list_calls if path == "/volumes"]
    assert all(query["catalogKey"] == "aidp_lab-key" for query in schema_queries)
    assert {query["schemaKey"] for query in volume_queries} == {
        "aidp_lab.landing",
        "aidp_lab.bronze",
        "aidp_lab.silver",
        "aidp_lab.gold",
    }
    role_queries = [params for path, params in api.list_calls if path == "/roles"]
    assert role_queries and all(query == {"displayName": "AIDP_LAB_DEVELOPER"} for query in role_queries)
    assert any("Developer group" in event for event in events)
    cluster_payloads = [payload for method, path, payload, _ in api.calls if method == "POST" and path.endswith("/clusters")]
    assert cluster_payloads[0]["displayName"] == "aidp_lab_shared_compute"
    assert cluster_payloads[0]["type"] == "USER"
    assert cluster_payloads[0]["driverConfig"]["driverShape"] == "amd.generic"
    assert cluster_payloads[0]["workerConfig"]["maxWorkerCount"] == 10
    assert reconciled["shared_compute_key"] == "aidp_lab_shared_compute-key"


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
    assert observed[0] == observed[1]
    assert observed[0] != observed[2]


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
    assert not post_apply.permission_is_assigned(
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
        "https://aidp.us-chicago-1.oci.oraclecloud.com/20260430/"
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
