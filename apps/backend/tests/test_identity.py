import asyncio
import json
import time

import httpx

from app.config import Settings
from app.identity import IdentityClient, IdentityConflict, IdentityPending, IdentityRejected, LocalIdentityClient


def test_group_user_listing_paginates() -> None:
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert "ocid" in request.url.params["attributes"].split(",")
        start = int(request.url.params["startIndex"])
        starts.append(start)
        count = 100 if start == 1 else 1
        users = [
            {
                "id": f"user-{start + index}",
                "userName": f"user-{start + index}@example.com",
                "externalId": "lab",
                "active": True,
            }
            for index in range(count)
        ]
        return httpx.Response(200, json={"Resources": users, "totalResults": 101})

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport)
        client = IdentityClient(
            Settings(identity_domain_url="https://identity.example.test", identity_oauth_client_id="client"),
            client=http,
        )
        client._token = "test-token"
        client._token_refresh_at = float("inf")
        users = await client._users_in_group("group-id")
        assert len(users) == 101
        await client.close()

    asyncio.run(run())
    assert starts == [1, 101]


def test_identity_domain_rejection_is_safe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert json.loads(request.content)["name"] == {
            "formatted": "Ada",
            "givenName": "Ada",
            "familyName": "Ada",
        }
        assert "password" not in json.loads(request.content)
        return httpx.Response(400, json={"detail": "Identity Domains rejected the user"})

    async def run() -> None:
        client = IdentityClient(
            Settings(identity_domain_url="https://identity.example.test"),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._token = "test-token"
        client._token_refresh_at = float("inf")
        try:
            await client.create_user("Ada", "ada@example.com")
        except IdentityRejected as exc:
            assert "Identity Domains" in str(exc)
        else:
            raise AssertionError("Identity Domains 400 must become IdentityRejected")
        await client.close()

    asyncio.run(run())


def test_prepare_registration_initiates_identity_activation_without_a_password() -> None:
    requests: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        requests.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.params.get("filter", "").startswith("userName"):
            return httpx.Response(200, json={"Resources": [], "totalResults": 0})
        if request.method == "POST":
            return httpx.Response(
                201,
                json={"id": "managed-user", "ocid": "ocid1.user.oc1..managed"},
            )
        if request.method == "GET":
            return httpx.Response(200, json={"Resources": []})
        return httpx.Response(200)

    async def run() -> None:
        client = IdentityClient(
            Settings(identity_domain_url="https://identity.example.test", lab_marker="lab"),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._token = "test-token"
        client._token_refresh_at = float("inf")
        result = await client.prepare_registration("Ada Lovelace", "ada@example.com")
        assert result.user_id == "managed-user"
        await client.close()

    asyncio.run(run())
    create_request = next(request for request in requests if request[:2] == ("POST", "/admin/v1/Users"))
    activation_request = next(request for request in requests if "UserActivationInitiator" in request[1])
    assert "password" not in create_request[2]
    assert activation_request == (
        "PUT",
        "/admin/v1/UserActivationInitiator/managed-user",
        {"schemas": ["urn:ietf:params:scim:schemas:oracle:idcs:UserActivationInitiator"]},
    )


def test_existing_managed_user_retries_activation_before_group_changes() -> None:
    events: list[str] = []

    class ExistingIdentity(IdentityClient):
        async def find_user(self, email: str):
            return {
                "id": "managed-user",
                "ocid": "ocid1.user.oc1..managed",
                "userName": email,
                "externalId": "lab",
            }

        async def ensure_activation_email(self, user_id: str) -> None:
            events.append(f"activate:{user_id}")

        async def _is_member(self, group_id: str, user_id: str) -> bool:
            return False

        async def add_member(self, group_id: str, user_id: str) -> None:
            events.append(f"add:{group_id}")

        async def remove_member(self, group_id: str, user_id: str) -> None:
            events.append(f"remove:{group_id}")

    async def run() -> None:
        client = ExistingIdentity(
            Settings(
                identity_domain_url="https://identity.example.test",
                developer_group_id="developers",
                pending_group_id="pending",
                lab_marker="lab",
            ),
            client=httpx.AsyncClient(),
        )
        await client.prepare_registration("Ada", "ada@example.com")
        await client.close()

    asyncio.run(run())
    assert events == ["activate:managed-user", "add:pending", "remove:developers"]


def test_pending_user_does_not_receive_duplicate_activation_email() -> None:
    events: list[str] = []

    class PendingIdentity(IdentityClient):
        async def find_user(self, email: str):
            return {
                "id": "managed-user",
                "ocid": "ocid1.user.oc1..managed",
                "userName": email,
                "externalId": "lab",
            }

        async def _is_member(self, group_id: str, user_id: str) -> bool:
            return group_id == "pending"

        async def ensure_activation_email(self, user_id: str) -> None:
            raise AssertionError("pending users must not receive another activation email")

        async def add_member(self, group_id: str, user_id: str) -> None:
            events.append(f"add:{group_id}")

        async def remove_member(self, group_id: str, user_id: str) -> None:
            events.append(f"remove:{group_id}")

    async def run() -> None:
        client = PendingIdentity(
            Settings(
                identity_domain_url="https://identity.example.test",
                developer_group_id="developers",
                pending_group_id="pending",
                lab_marker="lab",
            ),
            client=httpx.AsyncClient(),
        )
        await client.prepare_registration("Ada", "ada@example.com")
        await client.close()

    asyncio.run(run())
    assert events == ["add:pending", "remove:developers"]


def test_activation_transient_is_identity_pending_and_retryable() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path.endswith("/UserActivationInitiator/managed-user"):
            attempts += 1
            return httpx.Response(503 if attempts == 1 else 200)
        if request.url.params.get("filter", "").startswith("userName"):
            user = {
                "id": "managed-user",
                "ocid": "ocid1.user.oc1..managed",
                "userName": "ada@example.com",
                "emails": [{"value": "ada@example.com"}],
                "externalId": "lab",
            }
            return httpx.Response(200, json={"Resources": [user], "totalResults": 1})
        return httpx.Response(200, json={"Resources": []})

    async def run() -> None:
        client = IdentityClient(
            Settings(
                identity_domain_url="https://identity.example.test",
                developer_group_id="developers",
                pending_group_id="pending",
                lab_marker="lab",
            ),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._token = "test-token"
        client._token_refresh_at = float("inf")
        try:
            await client.prepare_registration("Ada", "ada@example.com")
        except IdentityPending:
            pass
        else:
            raise AssertionError("transient activation failure must remain in identity phase")
        result = await client.prepare_registration("Ada", "ada@example.com")
        assert result.status == "reconciled"
        await client.close()

    asyncio.run(run())
    assert attempts == 2


def test_unmanaged_existing_account_is_never_modified() -> None:
    methods: list[str] = []
    filters: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        filters.append(request.url.params["filter"])
        return httpx.Response(
            200,
            json={
                "Resources": [
                    {
                        "id": "foreign",
                        "userName": "foreign-login@example.com",
                        "emails": [{"value": "ada@example.com"}],
                        "externalId": "another-app",
                    }
                ]
            },
        )

    async def run() -> None:
        client = IdentityClient(
            Settings(identity_domain_url="https://identity.example.test", lab_marker="lab"),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._token = "test-token"
        client._token_refresh_at = float("inf")
        try:
            await client.prepare_registration("Ada", "ada@example.com")
        except IdentityConflict:
            pass
        else:
            raise AssertionError("foreign account must conflict")
        await client.close()

    asyncio.run(run())
    assert methods == ["GET"]
    literal = json.dumps("ada@example.com")
    assert filters == [f"userName eq {literal} or emails.value eq {literal}"]


def test_partial_group_failure_retries_idempotently() -> None:
    class PartialIdentity(IdentityClient):
        def __init__(self) -> None:
            super().__init__(
                Settings(
                    identity_domain_url="https://identity.example.test",
                    developer_group_id="developers",
                    pending_group_id="pending",
                    lab_marker="lab",
                ),
                client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
            )
            self.pending = False
            self.developer = False
            self.fail_developer_once = True

        async def find_user(self, email: str):
            return {
                "id": "managed-user",
                "ocid": "ocid1.user.oc1..managed",
                "userName": email,
                "externalId": "lab",
            }

        async def _is_member(self, group_id: str, user_id: str) -> bool:
            return self.developer if group_id == "developers" else self.pending

        async def ensure_activation_email(self, user_id: str) -> None:
            return None

        async def add_member(self, group_id: str, user_id: str) -> None:
            if group_id == "pending":
                self.pending = True
                return
            if self.fail_developer_once:
                self.fail_developer_once = False
                raise httpx.ConnectError("temporary group failure")
            self.developer = True

        async def remove_member(self, group_id: str, user_id: str) -> None:
            if group_id == "pending":
                self.pending = False
            else:
                self.developer = False

    async def run() -> None:
        client = PartialIdentity()
        result = await client.prepare_registration("Ada", "ada@example.com")
        assert result.status == "reconciled"
        assert not result.was_developer
        assert client.pending and not client.developer
        try:
            await client.activate_registration(result.user_id)
        except IdentityPending:
            pass
        else:
            raise AssertionError("first partial failure must return pending")
        assert client.pending and not client.developer
        await client.activate_registration(result.user_id)
        assert client.developer and not client.pending
        await client.close()

    asyncio.run(run())


def test_existing_developer_is_pending_until_activation_in_strict_group_order() -> None:
    class ExistingDeveloperIdentity(IdentityClient):
        def __init__(self) -> None:
            super().__init__(
                Settings(
                    identity_domain_url="https://identity.example.test",
                    developer_group_id="developers",
                    pending_group_id="pending",
                    lab_marker="lab",
                ),
                client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
            )
            self.members = {"developers": True, "pending": False}
            self.events: list[str] = []

        async def find_user(self, email: str):
            return {
                "id": "managed-user",
                "ocid": "ocid1.user.oc1..managed",
                "userName": email,
                "externalId": "lab",
            }

        async def _is_member(self, group_id: str, user_id: str) -> bool:
            return self.members[group_id]

        async def add_member(self, group_id: str, user_id: str) -> None:
            self.events.append(f"add:{group_id}")
            self.members[group_id] = True

        async def remove_member(self, group_id: str, user_id: str) -> None:
            self.events.append(f"remove:{group_id}")
            self.members[group_id] = False

    async def run() -> None:
        client = ExistingDeveloperIdentity()
        result = await client.prepare_registration("Ada", "ada@example.com")
        assert result.status == "reconciled"
        assert result.was_developer
        assert client.members == {"developers": False, "pending": True}
        assert client.events == ["add:pending", "remove:developers"]

        await client.activate_registration(result.user_id)
        assert client.members == {"developers": True, "pending": False}
        assert client.events == [
            "add:pending",
            "remove:developers",
            "add:developers",
            "remove:pending",
        ]
        await client.close()

    asyncio.run(run())


def test_local_identity_demotes_existing_active_user_during_preparation() -> None:
    async def run() -> None:
        client = LocalIdentityClient(Settings())
        created = await client.prepare_registration("Ada", "ada@example.com")
        await client.activate_registration(created.user_id)
        assert (await client.list_lab_users())[0]["status"] == "active"

        reconciled = await client.prepare_registration("Ada", "ADA@example.com")
        assert reconciled.status == "reconciled"
        assert reconciled.was_developer
        assert (await client.list_lab_users())[0]["status"] == "pending"

        await client.activate_registration(reconciled.user_id)
        assert (await client.list_lab_users())[0]["status"] == "active"

    asyncio.run(run())


def test_delete_lab_user_revokes_aidp_groups_before_deleting_identity() -> None:
    events: list[tuple[str, str]] = []

    class RevokingIdentity(IdentityClient):
        async def _request(self, method: str, path: str, **_kwargs):
            events.append((method, path))
            if method == "GET":
                return httpx.Response(
                    200,
                    json={"id": "managed-user", "externalId": "lab"},
                    request=httpx.Request(method, "https://identity.example.test" + path),
                )
            return httpx.Response(204, request=httpx.Request(method, "https://identity.example.test" + path))

        async def remove_member(self, group_id: str, user_id: str) -> None:
            events.append(("REMOVE", f"{group_id}/{user_id}"))

    async def run() -> None:
        client = RevokingIdentity(
            Settings(
                identity_domain_url="https://identity.example.test",
                developer_group_id="developers",
                pending_group_id="pending",
                lab_marker="lab",
            ),
            client=httpx.AsyncClient(),
        )
        try:
            assert await client.delete_lab_user("managed-user")
        finally:
            await client.close()

    asyncio.run(run())
    assert events == [
        ("GET", "/admin/v1/Users/managed-user"),
        ("REMOVE", "developers/managed-user"),
        ("REMOVE", "pending/managed-user"),
        ("DELETE", "/admin/v1/Users/managed-user"),
    ]


def test_admin_listing_includes_managed_user_without_group() -> None:
    class ListingIdentity(IdentityClient):
        async def _users_matching(self, filter_expression: str):
            if filter_expression.startswith("externalId"):
                return [
                    {"id": "orphan", "userName": "orphan@example.com", "externalId": "lab", "active": True},
                    {"id": "active", "userName": "active@example.com", "externalId": "lab", "active": True},
                ]
            if "developers" in filter_expression:
                return [
                    {"id": "active"},
                    {"id": "group-active", "userName": "group-active@example.com", "active": True},
                ]
            return [{"id": "group-pending", "userName": "group-pending@example.com", "active": True}]

    async def run() -> None:
        client = ListingIdentity(
            Settings(
                identity_domain_url="https://identity.example.test",
                developer_group_id="developers",
                pending_group_id="pending",
                lab_marker="lab",
            ),
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
        )
        users = await client.list_lab_users()
        assert {user["email"]: user["status"] for user in users} == {
            "active@example.com": "active",
            "group-active@example.com": "active",
            "group-pending@example.com": "pending",
            "orphan@example.com": "pending",
        }
        await client.close()

    asyncio.run(run())


def test_concurrent_create_409_waits_for_managed_user_and_continues(monkeypatch) -> None:
    searches = 0
    sleeps: list[int] = []

    async def no_sleep(seconds: int) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("app.identity.asyncio.sleep", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal searches
        if request.method == "POST" and request.url.path.endswith("/Users"):
            return httpx.Response(409, json={"detail": "duplicate"})
        if request.method == "PUT" and "UserActivationInitiator" in request.url.path:
            return httpx.Response(200)
        filter_expression = request.url.params.get("filter", "")
        if filter_expression.startswith("userName"):
            searches += 1
            resources = [] if searches < 4 else [
                {
                    "id": "managed",
                    "ocid": "ocid1.user.oc1..managed",
                    "userName": "ada@example.com",
                    "emails": [{"value": "ada@example.com"}],
                    "externalId": "lab",
                }
            ]
            return httpx.Response(200, json={"Resources": resources, "totalResults": len(resources)})
        if "groups.value" in filter_expression:
            resources = [{"id": "managed"}] if "pending" in filter_expression else []
            return httpx.Response(200, json={"Resources": resources})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def run() -> None:
        client = IdentityClient(
            Settings(
                identity_domain_url="https://identity.example.test",
                developer_group_id="developers",
                pending_group_id="pending",
                lab_marker="lab",
            ),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._token = "test-token"
        client._token_refresh_at = float("inf")
        result = await client.prepare_registration("Ada", "ada@example.com")
        assert result.status == "reconciled"
        await client.close()

    asyncio.run(run())
    assert searches == 4
    assert sleeps == [1, 1, 1]


def test_concurrent_create_409_rereads_foreign_user_as_conflict(monkeypatch) -> None:
    searches = 0

    async def no_sleep(_: int) -> None:
        return None

    monkeypatch.setattr("app.identity.asyncio.sleep", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal searches
        if request.method == "POST":
            return httpx.Response(409, json={"detail": "duplicate"})
        searches += 1
        resources = [] if searches == 1 else [
            {
                "id": "foreign",
                "userName": "other@example.com",
                "emails": [{"value": "ada@example.com"}],
                "externalId": "other-app",
            }
        ]
        return httpx.Response(200, json={"Resources": resources, "totalResults": len(resources)})

    async def run() -> None:
        client = IdentityClient(
            Settings(identity_domain_url="https://identity.example.test", lab_marker="lab"),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._token = "test-token"
        client._token_refresh_at = float("inf")
        try:
            await client.prepare_registration("Ada", "ada@example.com")
        except IdentityConflict:
            pass
        else:
            raise AssertionError("foreign user found after SCIM 409 must conflict")
        await client.close()

    asyncio.run(run())
    assert searches == 2


def test_concurrent_create_409_exhausts_consistency_window_as_pending(monkeypatch) -> None:
    searches = 0
    sleeps: list[int] = []

    async def no_sleep(seconds: int) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("app.identity.asyncio.sleep", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal searches
        if request.method == "POST":
            return httpx.Response(409, json={"detail": "duplicate"})
        searches += 1
        return httpx.Response(200, json={"Resources": [], "totalResults": 0})

    async def run() -> None:
        client = IdentityClient(
            Settings(identity_domain_url="https://identity.example.test", lab_marker="lab"),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._token = "test-token"
        client._token_refresh_at = float("inf")
        try:
            await client.prepare_registration("Ada", "ada@example.com")
        except IdentityPending as exc:
            assert "not visible yet" in str(exc)
        else:
            raise AssertionError("missing user after the SCIM consistency window must remain retryable")
        await client.close()

    asyncio.run(run())
    assert searches == 6
    assert sleeps == [1, 1, 1, 1, 1]


def test_developer_demotion_failure_keeps_registration_pending() -> None:
    class RemovalFailureIdentity(IdentityClient):
        async def find_user(self, email: str):
            return {
                "id": "managed",
                "ocid": "ocid1.user.oc1..managed",
                "userName": email,
                "externalId": "lab",
            }

        async def _is_member(self, group_id: str, user_id: str) -> bool:
            return group_id == "developers"

        async def add_member(self, group_id: str, user_id: str) -> None:
            return None

        async def remove_member(self, group_id: str, user_id: str) -> None:
            raise RuntimeError("temporary removal failure")

    async def run() -> None:
        client = RemovalFailureIdentity(
            Settings(
                identity_domain_url="https://identity.example.test",
                developer_group_id="developers",
                pending_group_id="pending",
                lab_marker="lab",
            ),
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
        )
        try:
            await client.prepare_registration("Ada", "ada@example.com")
        except IdentityPending:
            pass
        else:
            raise AssertionError("developer demotion failure must keep registration pending")
        await client.close()

    asyncio.run(run())


def test_transient_identity_lookup_is_reported_as_pending() -> None:
    async def run() -> None:
        client = IdentityClient(
            Settings(identity_domain_url="https://identity.example.test"),
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(503))
            ),
        )
        client._token = "test-token"
        client._token_refresh_at = float("inf")
        try:
            await client.prepare_registration("Ada", "ada@example.com")
        except IdentityPending as exc:
            assert "reconciling" in str(exc)
        else:
            raise AssertionError("Identity 5xx must remain in the identity phase")
        await client.close()

    asyncio.run(run())


def test_oauth_401_invalidates_token_and_retries_once() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("Authorization", "")
        requests.append((request.url.path, authorization))
        if request.url.path.endswith("/oauth2/v1/token"):
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 120})
        if authorization == "Bearer stale-token":
            return httpx.Response(401)
        return httpx.Response(200, json={"Resources": []})

    async def run() -> None:
        client = IdentityClient(
            Settings(
                identity_domain_url="https://identity.example.test",
                identity_oauth_client_id="client",
                identity_oauth_client_secret="secret",
            ),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._token = "stale-token"
        client._token_refresh_at = float("inf")
        before = time.monotonic()
        await client.healthcheck()
        assert before + 80 < client._token_refresh_at < before + 121
        await client.close()

    asyncio.run(run())
    assert requests == [
        ("/admin/v1/Users", "Bearer stale-token"),
        ("/oauth2/v1/token", "Basic Y2xpZW50OnNlY3JldA=="),
        ("/admin/v1/Users", "Bearer fresh-token"),
    ]


def test_expired_oauth_token_is_refreshed_before_identity_request() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("Authorization", "")
        requests.append((request.url.path, authorization))
        if request.url.path.endswith("/oauth2/v1/token"):
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 60})
        assert authorization == "Bearer fresh-token"
        return httpx.Response(200, json={"Resources": []})

    async def run() -> None:
        client = IdentityClient(
            Settings(
                identity_domain_url="https://identity.example.test",
                identity_oauth_client_id="client",
                identity_oauth_client_secret="secret",
            ),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._token = "expired-token"
        client._token_refresh_at = 0
        await client.healthcheck()
        await client.close()

    asyncio.run(run())
    assert requests == [
        ("/oauth2/v1/token", "Basic Y2xpZW50OnNlY3JldA=="),
        ("/admin/v1/Users", "Bearer fresh-token"),
    ]


def test_scim_filter_escapes_quotes_and_backslashes() -> None:
    observed = ""
    email = 'a"b\\c@example.com'

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed
        observed = request.url.params["filter"]
        return httpx.Response(200, json={"Resources": [], "totalResults": 0})

    async def run() -> None:
        client = IdentityClient(
            Settings(identity_domain_url="https://identity.example.test"),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._token = "test-token"
        client._token_refresh_at = float("inf")
        assert await client.find_user(email) is None
        await client.close()

    asyncio.run(run())
    literal = json.dumps(email)
    assert observed == f"userName eq {literal} or emails.value eq {literal}"
