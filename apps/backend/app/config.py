from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    admin_username: str = "admin"
    admin_password_hash: str = ""
    registration_code_hash: str = ""
    identity_domain_url: str = ""
    identity_oauth_client_id: str = ""
    oauth_secret_ocid: str = ""
    identity_oauth_client_secret: str = ""
    developer_group_id: str = ""
    pending_group_id: str = ""
    lab_marker: str = "aidp-lab"
    session_secret_file: str = "/var/lib/aidp-lab/session.key"
    cookie_secure: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            admin_username=os.getenv("ADMIN_USERNAME", "admin"),
            admin_password_hash=os.getenv("ADMIN_PASSWORD_HASH", ""),
            registration_code_hash=os.getenv("REGISTRATION_CODE_HASH", ""),
            identity_domain_url=os.getenv("IDENTITY_DOMAIN_URL", "").rstrip("/"),
            identity_oauth_client_id=os.getenv("IDENTITY_OAUTH_CLIENT_ID", ""),
            oauth_secret_ocid=os.getenv("OAUTH_SECRET_OCID", ""),
            identity_oauth_client_secret=os.getenv("IDENTITY_OAUTH_CLIENT_SECRET", ""),
            developer_group_id=os.getenv("IDENTITY_DEVELOPER_GROUP_ID", ""),
            pending_group_id=os.getenv("IDENTITY_PENDING_GROUP_ID", ""),
            lab_marker=os.getenv("LAB_MARKER", "aidp-lab"),
            session_secret_file=os.getenv("SESSION_SECRET_FILE", "/var/lib/aidp-lab/session.key"),
            cookie_secure=os.getenv("COOKIE_SECURE", "true").lower() not in {"0", "false", "no"},
        )

    def identity_ready(self) -> bool:
        return all(
            (
                self.identity_domain_url,
                self.identity_oauth_client_id,
                self.identity_oauth_client_secret or self.oauth_secret_ocid,
                self.developer_group_id,
                self.pending_group_id,
            )
        )
