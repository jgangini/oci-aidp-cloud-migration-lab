from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .aidp import AidpClient, AidpProvisionError, AidpProvisionPending, LocalAidpClient
from .config import Settings, SettingsStore
from .identity import IdentityClient, IdentityConflict, IdentityPending, IdentityRejected, LocalIdentityClient
from .security import RateLimiter, issue_session, load_or_create_session_key, verify_secret, verify_session


COOKIE_NAME = "__Host-aidp_lab_admin"
LOCAL_COOKIE_NAME = "aidp_lab_admin"
CODE_PATTERN = re.compile(r"^[A-Z]{4}-[0-9]{4}$")
EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")


Industry = Literal["banking", "telecommunications", "retail", "healthcare"]


class UserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=5, max_length=254)
    industry: Industry

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 2:
            raise ValueError("Name is required")
        return value

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_PATTERN.fullmatch(value):
            raise ValueError("Enter a valid email address")
        return value


class RegistrationRequest(UserRequest):
    code: str = Field(min_length=9, max_length=9)

    @field_validator("code")
    @classmethod
    def clean_code(cls, value: str) -> str:
        value = value.strip().upper()
        if not CODE_PATTERN.fullmatch(value):
            raise ValueError("Code must match AAAA-0000")
        return value


class AdminUserRequest(UserRequest):
    pass


class SettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aidp_url: str = Field(min_length=1, max_length=2_048)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    cookie_name = COOKIE_NAME if settings.cookie_secure else LOCAL_COOKIE_NAME

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        for client in (app.state.identity_client, app.state.aidp_client):
            if client is not None:
                await client.close()

    app = FastAPI(title="OCI AIDP Lab", version="1.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.settings_store = SettingsStore(settings)
    app.state.session_key = load_or_create_session_key(settings.session_secret_file)
    app.state.register_limiter = RateLimiter(5, 60)
    app.state.login_limiter = RateLimiter(5, 60)
    app.state.identity_client = None
    app.state.aidp_client = None

    def default_factory() -> IdentityClient | LocalIdentityClient:
        if app.state.identity_client is None:
            app.state.identity_client = LocalIdentityClient(settings) if settings.local_development_mode else IdentityClient(settings)
        return app.state.identity_client

    app.state.identity_factory = default_factory

    def default_aidp_factory() -> AidpClient | LocalAidpClient:
        if app.state.aidp_client is None:
            app.state.aidp_client = LocalAidpClient(settings) if settings.local_development_mode else AidpClient(settings)
        return app.state.aidp_client

    app.state.aidp_factory = default_aidp_factory

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    def client_ip(request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        return forwarded or (request.client.host if request.client else "unknown")

    def require_identity() -> None:
        if not settings.identity_ready() or not settings.registration_code_hash:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Registration is not configured")

    def require_registration_ready() -> None:
        require_identity()
        if not settings.aidp_ready():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AIDP workspace provisioning is not configured")

    def require_admin(request: Request) -> str:
        username = verify_session(request.cookies.get(cookie_name, ""), app.state.session_key)
        if username != settings.admin_username:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Administrator session required")
        return username

    async def provision_user(name: str, email: str, industry: Industry) -> JSONResponse:
        try:
            result = await app.state.identity_factory().register(name, email)
        except IdentityConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except IdentityRejected as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        except IdentityPending as exc:
            return JSONResponse(status_code=202, content={"status": "pending", "message": str(exc)})
        try:
            material = await app.state.aidp_factory().provision_user(email, industry)
        except AidpProvisionPending as exc:
            return JSONResponse(status_code=202, content={"status": "pending", "message": str(exc)})
        except AidpProvisionError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        content: dict[str, Any] = {
            "status": "active",
            "email": result.email,
            "industry": industry,
            "workspace_path": material.workspace_path,
            "job_name": material.job_name,
        }
        aidp_url = app.state.settings_store.get_workbench_url()
        if aidp_url:
            content["aidp_url"] = aidp_url
        else:
            content["message"] = "Your account is ready. Ask the lab administrator to configure the Workbench URL."
        return JSONResponse(status_code=201 if result.status == "created" else 200, content=content)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        if not settings.identity_ready() or not settings.aidp_ready():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Lab services are unavailable")
        try:
            await app.state.identity_factory().healthcheck()
        except Exception as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Identity service is unavailable") from exc
        return {"status": "ok"}

    @app.get("/api/public/config")
    async def public_config() -> dict[str, Any]:
        return {
            "lab_name": "OCI AI Data Platform Lab",
            "registration_code_pattern": "AAAA-0000",
            "industries": ["banking", "telecommunications", "retail", "healthcare"],
        }

    @app.post("/api/register")
    async def register(payload: RegistrationRequest, request: Request) -> JSONResponse:
        require_registration_ready()
        if not app.state.register_limiter.allow(client_ip(request)):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many registration attempts")
        if not verify_secret(payload.code, settings.registration_code_hash):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid registration code")
        return await provision_user(payload.name, payload.email, payload.industry)

    @app.post("/api/admin/login", status_code=204)
    async def admin_login(payload: LoginRequest, request: Request) -> Response:
        if not app.state.login_limiter.allow(client_ip(request)):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many login attempts")
        valid_user = payload.username == settings.admin_username
        valid_password = verify_secret(payload.password, settings.admin_password_hash)
        if not (valid_user and valid_password):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid administrator credentials")
        response = Response(status_code=204)
        response.set_cookie(
            cookie_name,
            issue_session(app.state.session_key, settings.admin_username),
            max_age=28_800,
            secure=settings.cookie_secure,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/api/admin/logout", status_code=204)
    async def admin_logout() -> Response:
        response = Response(status_code=204)
        response.delete_cookie(cookie_name, path="/", secure=settings.cookie_secure, httponly=True, samesite="strict")
        return response

    @app.get("/api/admin/session")
    async def admin_session(username: str = Depends(require_admin)) -> dict[str, str]:
        return {"username": username}

    @app.get("/api/admin/settings")
    async def admin_settings(_admin: str = Depends(require_admin)) -> dict[str, str]:
        return {"aidp_url": app.state.settings_store.get_workbench_url()}

    @app.put("/api/admin/settings")
    async def update_admin_settings(payload: SettingsRequest, _admin: str = Depends(require_admin)) -> dict[str, str]:
        try:
            return {"aidp_url": app.state.settings_store.set_workbench_url(payload.aidp_url)}
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    @app.get("/api/admin/users")
    async def admin_users(_admin: str = Depends(require_admin)) -> dict[str, list[dict]]:
        require_identity()
        client = app.state.identity_factory()
        return {"users": await client.list_lab_users()}

    @app.post("/api/admin/users")
    async def admin_create_user(payload: AdminUserRequest, _admin: str = Depends(require_admin)) -> JSONResponse:
        require_identity()
        if not settings.aidp_ready():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AIDP workspace provisioning is not configured")
        return await provision_user(payload.name, payload.email, payload.industry)

    @app.delete("/api/admin/users/{user_id}", status_code=204)
    async def admin_delete_user(user_id: str, _admin: str = Depends(require_admin)) -> Response:
        require_identity()
        if not settings.aidp_ready():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AIDP workspace provisioning is not configured")
        client = app.state.identity_factory()
        try:
            user = await client.get_lab_user(user_id)
            if user is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Lab user not found")
            await app.state.aidp_factory().cleanup_user(user["email"])
            deleted = await client.delete_lab_user(user_id)
        except IdentityConflict as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        except AidpProvisionPending as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, f"AIDP cleanup is still in progress. Retry deletion shortly. {exc}") from exc
        except AidpProvisionError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, f"AIDP cleanup failed before Identity Domains deletion. {exc}") from exc
        if not deleted:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Lab user not found")
        return Response(status_code=204)

    return app


app = create_app()
