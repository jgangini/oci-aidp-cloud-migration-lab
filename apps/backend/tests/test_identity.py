import asyncio
import json
import time

import httpx

from app.config import Settings
from app.identity import IdentityClient, IdentityConflict, IdentityPending, IdentityRejected


def test_group_user_listing_paginates() -> None:
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
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


def test_identity_domain_password_rejection_is_safe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(400, json={"detail": "Password policy rejected the password"})

    async def run() -> None:
        client = IdentityClient(
            Settings(identity_domain_url="https://identity.example.test"),
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        client._token = "test-token"
        client._token_refresh_at = float("inf")
        try:
            await client.create_user("Ada", "ada@example.com", "invalid-password")
        except IdentityRejected as exc:
            assert "Password policy" in str(exc)
        else:
            raise AssertionError("Identity Domains 400 must become IdentityRejected")
        await client.close()

    asyncio.run(run())


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
            await client.register("Ada", "ada@example.com", "valid-password")
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
            return {"id": "managed-user", "userName": email, "externalId": "lab"}

        async def _is_member(self, group_id: str, user_id: str) -> bool:
            return self.developer if group_id == "developers" else self.pending

        async def add_member(self, group_id: str, user_id: str) -> None:
            if group_id == "pending":
                self.pending = True
                return
            if self.fail_developer_once:
                self.fail_developer_once = False
                raise httpx.ConnectError("temporary group failure")
            self.developer = True

        async def remove_member(self, group_id: str, user_id: str) -> None:
            self.pending = False

    async def run() -> None:
        client = PartialIdentity()
        try:
            await client.register("Ada", "ada@example.com", "valid-password")
        except IdentityPending:
            pass
        else:
            raise AssertionError("first partial failure must return pending")
        assert client.pending and not client.developer
        result = await client.register("Ada", "ada@example.com", "valid-password")
        assert result.status == "reconciled"
        assert client.developer and not client.pending
        await client.close()

    asyncio.run(run())


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
        filter_expression = request.url.params.get("filter", "")
        if filter_expression.startswith("userName"):
            searches += 1
            resources = [] if searches < 4 else [
                {
                    "id": "managed",
                    "userName": "ada@example.com",
                    "emails": [{"value": "ada@example.com"}],
                    "externalId": "lab",
                }
            ]
            return httpx.Response(200, json={"Resources": resources, "totalResults": len(resources)})
        if "groups.value" in filter_expression:
            resources = [{"id": "managed"}] if "developers" in filter_expression else []
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
        result = await client.register("Ada", "ada@example.com", "valid-password")
        assert result.status == "active"
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
            await client.register("Ada", "ada@example.com", "valid-password")
        except IdentityConflict:
            pass
        else:
            raise AssertionError("foreign user found after SCIM 409 must conflict")
        await client.close()

    asyncio.run(run())
    assert searches == 2


def test_concurrent_create_409_exhausts_consistency_window(monkeypatch) -> None:
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
            await client.register("Ada", "ada@example.com", "valid-password")
        except IdentityConflict:
            pass
        else:
            raise AssertionError("missing user after the SCIM consistency window must conflict")
        await client.close()

    asyncio.run(run())
    assert searches == 6
    assert sleeps == [1, 1, 1, 1, 1]


def test_pending_removal_failure_stays_pending_on_retry() -> None:
    class RemovalFailureIdentity(IdentityClient):
        async def find_user(self, email: str):
            return {"id": "managed", "userName": email, "externalId": "lab"}

        async def _is_member(self, group_id: str, user_id: str) -> bool:
            return group_id == "developers"

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
            await client.register("Ada", "ada@example.com", "valid-password")
        except IdentityPending:
            pass
        else:
            raise AssertionError("pending removal failure must remain pending")
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
