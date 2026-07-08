from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from app.aidp import AidpProvisionConflict, AidpProvisionError, AidpProvisionPending, UserMaterial
from app.config import Settings
from app.identity import IdentityConflict, IdentityPending, IdentityRejected, RegistrationResult
from app.main import COOKIE_NAME, LOCAL_COOKIE_NAME, create_app
from app.security import RateLimiter, hash_secret


class FakeIdentity:
    def __init__(self, mode: str = "active") -> None:
        self.mode = mode

    async def prepare_registration(self, name: str, email: str) -> RegistrationResult:
        if self.mode == "conflict":
            raise IdentityConflict("existing unmanaged account")
        if self.mode == "pending":
            raise IdentityPending("reconciliation pending")
        if self.mode == "rejected":
            raise IdentityRejected("upstream secret detail must not escape")
        if self.mode == "existing":
            return RegistrationResult("reconciled", "user-id", "ocid1.user.oc1..ada", email)
        if self.mode == "reconciled":
            return RegistrationResult("reconciled", "user-id", "ocid1.user.oc1..ada", email)
        return RegistrationResult("created", "user-id", "ocid1.user.oc1..ada", email)

    async def activate_registration(self, user_id: str) -> None:
        if self.mode == "activation-pending":
            raise IdentityPending("activation pending")

    async def list_lab_users(self) -> list[dict]:
        return [{"id": "user-id", "name": "Ada", "email": "ada@example.com", "status": "active", "active": True, "managed": True}]

    async def delete_lab_user(self, user_id: str) -> bool:
        if self.mode == "foreign-delete":
            raise IdentityConflict("Only users created by this lab can be deleted")
        return user_id == "user-id"

    async def get_lab_user(self, user_id: str) -> dict | None:
        if self.mode == "foreign-delete":
            raise IdentityConflict("Only users created by this lab can be deleted")
        return {"id": user_id, "ocid": "ocid1.user.oc1..ada", "email": "ada@example.com"} if user_id == "user-id" else None

    async def healthcheck(self) -> None:
        if self.mode == "health-fail":
            raise RuntimeError("upstream secret detail must not escape")


class FakeAidp:
    def __init__(self, mode: str = "active") -> None:
        self.mode = mode
        self.cleaned: list[str] = []

    async def provision_user(self, user_ocid: str, email: str, industry: str) -> UserMaterial:
        if self.mode == "aidp-pending":
            raise AidpProvisionPending("workbench is creating shared material", "schemas")
        if self.mode == "aidp-conflict":
            raise AidpProvisionConflict("industry is immutable")
        if self.mode == "error":
            raise AidpProvisionError("AIDP policy is missing")
        return UserMaterial(
            email,
            industry,
            "u_0123456789abcdef",
            f"/Workspace/lab-users/u_0123456789abcdef/{industry}",
            f"wf_u_0123456789abcdef_{industry}_medallion",
        )

    async def cleanup_user(self, user_ocid: str) -> None:
        if self.mode == "cleanup-pending":
            raise AidpProvisionPending("cleanup in progress")
        self.cleaned.append(user_ocid)

    async def healthcheck(self) -> None:
        if self.mode == "health-fail-aidp":
            raise RuntimeError("technical client detail must not escape")
        return None

    async def close(self) -> None:
        return None


def make_client(tmp_path: Path, mode: str = "active") -> TestClient:
    settings = Settings(
        admin_username="lab-admin",
        admin_password_hash=hash_secret("long-admin-password", iterations=1_000, salt=b"admin-test-salt"),
        registration_code_hash=hash_secret("ABCD-1234", iterations=1_000, salt=b"code-test-salt"),
        identity_domain_url="https://identity.example.test",
        developer_group_id="developers",
        pending_group_id="pending",
        aidp_workbench_url="https://example.datalake.oci.oraclecloud.com#?tenant=test&domain=Default",
        aidp_platform_id="ocid1.aidataplatform.oc1..test",
        aidp_workspace_name="aidp-lab-workspace-test",
        aidp_region="us-chicago-1",
        oci_config_file="/etc/aidp-lab/oci/config",
        objectstorage_namespace="namespace",
        bucket_name="aidp-data-test",
        lab_marker="lab-test",
        session_secret_file=str(tmp_path / "session.key"),
        aidp_settings_file=str(tmp_path / "settings.json"),
        cookie_secure=False,
    )
    app = create_app(settings)
    app.state.identity_factory = lambda: FakeIdentity(mode)
    app.state.aidp_factory = lambda: FakeAidp(mode)
    return TestClient(app)


def test_invalid_code_never_calls_identity(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.app.state.identity_factory = lambda: (_ for _ in ()).throw(AssertionError("must not call identity"))
    response = client.post(
        "/api/register",
        json={"name": "Ada Lovelace", "email": "ada@example.com", "industry": "banking", "code": "WXYZ-9999"},
    )
    assert response.status_code == 422


def test_registration_rate_limit_is_per_opaque_participant(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.app.state.register_limiter = RateLimiter(limit=1, window_seconds=60)
    payload = {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "industry": "banking",
        "code": "ABCD-1234",
    }
    assert client.post("/api/register", json=payload).status_code == 201
    assert client.post("/api/register", json={**payload, "email": "grace@example.com"}).status_code == 201
    limited = client.post("/api/register", json=payload)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0


def test_invalid_code_limit_is_shared_by_source_ip(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.app.state.invalid_code_limiter = RateLimiter(limit=1, window_seconds=60)
    payload = {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "industry": "banking",
        "code": "WXYZ-9999",
    }
    assert client.post("/api/register", json=payload).status_code == 422
    limited = client.post("/api/register", json={**payload, "email": "grace@example.com"})
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0


def test_identity_demotion_pending_never_calls_aidp(tmp_path: Path) -> None:
    class DemotionPendingIdentity(FakeIdentity):
        async def prepare_registration(self, name: str, email: str) -> RegistrationResult:
            raise IdentityPending("developer access demotion is still in progress")

    class MustNotCallAidp(FakeAidp):
        async def provision_user(self, user_ocid: str, email: str, industry: str) -> UserMaterial:
            raise AssertionError("AIDP must not run before Identity is safely pending")

    client = make_client(tmp_path)
    client.app.state.identity_factory = lambda: DemotionPendingIdentity()
    client.app.state.aidp_factory = lambda: MustNotCallAidp()
    response = client.post(
        "/api/register",
        json={"name": "Ada Lovelace", "email": "ada@example.com", "industry": "banking", "code": "ABCD-1234"},
    )
    assert response.status_code == 202
    assert response.json()["phase"] == "identity"


def test_health_requires_usable_signed_identity_operation(tmp_path: Path) -> None:
    healthy = make_client(tmp_path / "healthy").get("/api/health")
    unhealthy = make_client(tmp_path / "unhealthy", "health-fail").get("/api/health")
    aidp_unhealthy = make_client(tmp_path / "aidp-unhealthy", "health-fail-aidp").get("/api/health")
    assert healthy.status_code == 200
    assert healthy.json() == {"status": "ok"}
    assert unhealthy.status_code == 503
    assert unhealthy.json() == {"detail": "Lab control services are unavailable"}
    assert aidp_unhealthy.status_code == 503
    assert aidp_unhealthy.json() == {"detail": "Lab control services are unavailable"}


def test_scim_filter_metacharacters_are_rejected_in_email(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    response = client.post(
        "/api/register",
        json={"name": "Ada Lovelace", "email": 'ada"@example.com', "industry": "banking", "code": "ABCD-1234"},
    )
    assert response.status_code == 422


def test_register_active_pending_and_conflict(tmp_path: Path) -> None:
    payload = {"name": "Ada Lovelace", "email": "ADA@example.com", "industry": "banking", "code": "abcd-1234"}
    active = make_client(tmp_path / "a").post("/api/register", json=payload)
    pending = make_client(tmp_path / "p", "pending").post("/api/register", json=payload)
    conflict = make_client(tmp_path / "c", "conflict").post("/api/register", json=payload)
    existing = make_client(tmp_path / "e", "existing").post("/api/register", json=payload)
    reconciled = make_client(tmp_path / "rr", "reconciled").post("/api/register", json=payload)
    rejected = make_client(tmp_path / "r", "rejected").post("/api/register", json=payload)
    assert active.status_code == 201
    assert active.json()["email"] == "ada@example.com"
    assert active.json()["participant_key"] == "u_0123456789abcdef"
    assert active.json()["aidp_url"].startswith("https://example.datalake.oci.oraclecloud.com")
    assert pending.status_code == 202
    assert pending.json()["phase"] == "identity"
    assert conflict.status_code == 409
    assert existing.status_code == 200
    assert existing.json()["status"] == "active"
    assert reconciled.status_code == 200
    assert reconciled.json()["status"] == "active"
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "Identity Domains rejected this registration request"


def test_registration_reports_exact_aidp_and_activation_phases(tmp_path: Path) -> None:
    payload = {"name": "Ada Lovelace", "email": "ada@example.com", "industry": "banking", "code": "ABCD-1234"}
    aidp_pending = make_client(tmp_path / "aidp", "aidp-pending").post("/api/register", json=payload)
    activation_pending = make_client(tmp_path / "activation", "activation-pending").post("/api/register", json=payload)
    immutable = make_client(tmp_path / "immutable", "aidp-conflict").post("/api/register", json=payload)
    assert aidp_pending.status_code == 202
    assert aidp_pending.json()["phase"] == "schemas"
    assert activation_pending.status_code == 202
    assert activation_pending.json()["phase"] == "permissions"
    assert immutable.status_code == 409


def test_immutable_industry_conflict_restores_prior_developer_access(tmp_path: Path) -> None:
    identity = FakeIdentity()
    restored: list[str] = []

    async def prepare(_name: str, email: str) -> RegistrationResult:
        return RegistrationResult(
            "reconciled",
            "user-id",
            "ocid1.user.oc1..ada",
            email,
            was_developer=True,
        )

    async def activate(user_id: str) -> None:
        restored.append(user_id)

    identity.prepare_registration = prepare
    identity.activate_registration = activate
    client = make_client(tmp_path)
    client.app.state.identity_factory = lambda: identity
    client.app.state.aidp_factory = lambda: FakeAidp("aidp-conflict")
    response = client.post(
        "/api/register",
        json={
            "name": "Ada Lovelace",
            "email": "ada@example.com",
            "industry": "retail",
            "code": "ABCD-1234",
        },
    )
    assert response.status_code == 409
    assert restored == ["user-id"]


def test_registration_orders_identity_aidp_and_activation(tmp_path: Path) -> None:
    events: list[str] = []

    class RecordingIdentity(FakeIdentity):
        async def prepare_registration(self, name: str, email: str) -> RegistrationResult:
            events.append("identity.prepare")
            return await super().prepare_registration(name, email)

        async def activate_registration(self, user_id: str) -> None:
            events.append("identity.activate")
            await super().activate_registration(user_id)

    class RecordingAidp(FakeAidp):
        async def provision_user(self, user_ocid: str, email: str, industry: str) -> UserMaterial:
            events.append("aidp.provision")
            return await super().provision_user(user_ocid, email, industry)

    client = make_client(tmp_path / "active")
    client.app.state.identity_factory = lambda: RecordingIdentity()
    client.app.state.aidp_factory = lambda: RecordingAidp()
    payload = {"name": "Ada Lovelace", "email": "ada@example.com", "industry": "banking", "code": "ABCD-1234"}
    assert client.post("/api/register", json=payload).status_code == 201
    assert events == ["identity.prepare", "aidp.provision", "identity.activate"]

    events.clear()
    client = make_client(tmp_path / "pending")
    client.app.state.identity_factory = lambda: RecordingIdentity()
    client.app.state.aidp_factory = lambda: RecordingAidp("aidp-pending")
    assert client.post("/api/register", json=payload).status_code == 202
    assert events == ["identity.prepare", "aidp.provision"]

    events.clear()
    client = make_client(tmp_path / "error")
    client.app.state.identity_factory = lambda: RecordingIdentity()
    client.app.state.aidp_factory = lambda: RecordingAidp("error")
    assert client.post("/api/register", json=payload).status_code == 503
    assert events == ["identity.prepare", "aidp.provision"]


def test_registration_requires_a_supported_industry_and_never_accepts_password(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    invalid = client.post(
        "/api/register",
        json={"name": "Ada Lovelace", "email": "ada@example.com", "industry": "energy", "code": "ABCD-1234"},
    )
    password_rejected = client.post(
        "/api/register",
        json={"name": "Ada Lovelace", "email": "ada@example.com", "industry": "banking", "code": "ABCD-1234", "password": "ignored"},
    )
    assert invalid.status_code == 422
    assert password_rejected.status_code == 422
    valid = client.post(
        "/api/register",
        json={"name": "Ada Lovelace", "email": "ada@example.com", "industry": "banking", "code": "ABCD-1234"},
    )
    assert valid.status_code == 201
    assert valid.json()["industry"] == "banking"
    assert valid.json()["job_name"] == "wf_u_0123456789abcdef_banking_medallion"


def test_admin_cookie_and_live_users(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    denied = client.get("/api/admin/users")
    login = client.post("/api/admin/login", json={"username": "lab-admin", "password": "long-admin-password"})
    users = client.get("/api/admin/users")
    settings = client.get("/api/admin/settings")
    assert denied.status_code == 401
    assert login.status_code in {200, 204}
    assert LOCAL_COOKIE_NAME in client.cookies
    assert users.status_code == 200
    assert users.json()["users"][0]["status"] == "active"
    assert settings.status_code == 200
    assert settings.json()["aidp_url"].startswith("https://example.datalake.oci.oraclecloud.com")


def test_admin_can_persist_only_a_valid_direct_workbench_url(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.post("/api/admin/login", json={"username": "lab-admin", "password": "long-admin-password"})
    invalid = client.put("/api/admin/settings", json={"aidp_url": "https://cloud.oracle.com/ai-data-platform/"})
    valid = client.put(
        "/api/admin/settings",
        json={"aidp_url": "https://demo.datalake.oci.oraclecloud.com#?tenant=test&domain=Default"},
    )
    assert invalid.status_code == 422
    assert valid.status_code == 200
    assert client.get("/api/admin/settings").json() == valid.json()

    unexpected = client.put(
        "/api/admin/settings",
        json={"aidp_url": valid.json()["aidp_url"], "unexpected": "not accepted"},
    )
    assert unexpected.status_code == 422


def test_admin_can_create_and_delete_managed_user(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.post("/api/admin/login", json={"username": "lab-admin", "password": "long-admin-password"})
    created = client.post("/api/admin/users", json={"name": "Ada Lovelace", "email": "ada@example.com", "industry": "retail"})
    deleted = client.delete("/api/admin/users/user-id")
    assert created.status_code == 201
    assert created.json()["email"] == "ada@example.com"
    assert deleted.status_code == 204


def test_admin_cannot_delete_unmanaged_user(tmp_path: Path) -> None:
    client = make_client(tmp_path, "foreign-delete")
    client.post("/api/admin/login", json={"username": "lab-admin", "password": "long-admin-password"})
    response = client.delete("/api/admin/users/foreign-id")
    assert response.status_code == 403


def test_admin_delete_keeps_identity_user_when_aidp_cleanup_is_pending(tmp_path: Path) -> None:
    client = make_client(tmp_path, "cleanup-pending")
    client.post("/api/admin/login", json={"username": "lab-admin", "password": "long-admin-password"})
    response = client.delete("/api/admin/users/user-id")
    assert response.status_code == 409
    assert "AIDP cleanup is still in progress" in response.json()["detail"]


def test_local_development_mode_runs_user_lifecycle_without_oci(tmp_path: Path) -> None:
    settings = Settings(
        admin_username="local-admin",
        admin_password_hash=hash_secret("local-admin-password", iterations=1_000, salt=b"local-admin-salt"),
        registration_code_hash=hash_secret("AIDP-2026", iterations=1_000, salt=b"local-code-salt"),
        aidp_workbench_url="https://example.datalake.oci.oraclecloud.com#?tenant=local&domain=Default",
        session_secret_file=str(tmp_path / "session.key"),
        cookie_secure=False,
        local_development_mode=True,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 200
        assert client.post("/api/admin/login", json={"username": "local-admin", "password": "local-admin-password"}).status_code == 204
        created = client.post("/api/admin/users", json={"name": "Ada Lovelace", "email": "ada@example.com", "industry": "healthcare"})
        assert created.status_code == 201
        user_id = client.get("/api/admin/users").json()["users"][0]["id"]
        assert client.delete(f"/api/admin/users/{user_id}").status_code == 204
        assert client.get("/api/admin/users").json()["users"] == []


def test_https_profile_keeps_the_host_cookie_prefix(tmp_path: Path) -> None:
    settings = make_client(tmp_path).app.state.settings
    app = create_app(replace(settings, cookie_secure=True))
    with TestClient(app) as client:
        response = client.post("/api/admin/login", json={"username": "lab-admin", "password": "long-admin-password"})
    assert f"{COOKIE_NAME}=" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]


def test_default_identity_client_is_singleton_and_closes(tmp_path: Path, monkeypatch) -> None:
    class ClosingIdentity:
        def __init__(self, settings: Settings) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("app.main.IdentityClient", ClosingIdentity)
    settings = make_client(tmp_path).app.state.settings
    app = create_app(settings)
    with TestClient(app):
        first = app.state.identity_factory()
        second = app.state.identity_factory()
        assert first is second
    assert first.closed
