from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


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
    aidp_workbench_url: str = ""
    aidp_platform_id: str = ""
    aidp_workspace_name: str = ""
    aidp_region: str = ""
    oci_config_file: str = "/etc/aidp-lab/oci/config"
    objectstorage_namespace: str = ""
    bucket_name: str = ""
    aidp_settings_file: str = "/var/lib/aidp-lab/settings.json"
    lab_marker: str = "aidp-lab"
    session_secret_file: str = "/var/lib/aidp-lab/session.key"
    cookie_secure: bool = True
    local_development_mode: bool = False

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
            aidp_workbench_url=os.getenv("AIDP_WORKBENCH_URL", ""),
            aidp_platform_id=os.getenv("AIDP_PLATFORM_ID", ""),
            aidp_workspace_name=os.getenv("AIDP_WORKSPACE_NAME", ""),
            aidp_region=os.getenv("AIDP_REGION", ""),
            oci_config_file=os.getenv("OCI_CONFIG_FILE", "/etc/aidp-lab/oci/config"),
            objectstorage_namespace=os.getenv("OBJECTSTORAGE_NAMESPACE", ""),
            bucket_name=os.getenv("BUCKET_NAME", ""),
            aidp_settings_file=os.getenv("AIDP_SETTINGS_FILE", "/var/lib/aidp-lab/settings.json"),
            lab_marker=os.getenv("LAB_MARKER", "aidp-lab"),
            session_secret_file=os.getenv("SESSION_SECRET_FILE", "/var/lib/aidp-lab/session.key"),
            cookie_secure=os.getenv("COOKIE_SECURE", "true").lower() not in {"0", "false", "no"},
            local_development_mode=os.getenv("LOCAL_DEVELOPMENT_MODE", "false").lower() in {"1", "true", "yes"},
        )

    def identity_ready(self) -> bool:
        if self.local_development_mode:
            return True
        return all(
            (
                self.identity_domain_url,
                self.identity_oauth_client_id,
                self.identity_oauth_client_secret or self.oauth_secret_ocid,
                self.developer_group_id,
                self.pending_group_id,
            )
        )

    def aidp_ready(self) -> bool:
        return self.local_development_mode or all(
            (
                self.aidp_platform_id,
                self.aidp_workspace_name,
                self.aidp_region,
                self.oci_config_file,
                self.objectstorage_namespace,
                self.bucket_name,
            )
        )


class SettingsStore:
    """Persists only administrator-editable, non-secret Workbench configuration."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def get_workbench_url(self) -> str:
        path = Path(self._settings.aidp_settings_file)
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8")).get("aidp_workbench_url", "")
                if isinstance(value, str) and _valid_workbench_url(value):
                    return value
            except (OSError, ValueError, TypeError):
                pass
        return self._settings.aidp_workbench_url if _valid_workbench_url(self._settings.aidp_workbench_url) else ""

    def set_workbench_url(self, value: str) -> str:
        normalized = value.strip()
        if not _valid_workbench_url(normalized):
            raise ValueError("Enter a valid HTTPS Oracle AI Data Platform Workbench URL")
        path = Path(self._settings.aidp_settings_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"aidp_workbench_url": normalized}) + "\n", encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return normalized


def _valid_workbench_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and parsed.hostname is not None and (
        parsed.hostname.endswith(".datalake.oci.oraclecloud.com") or parsed.hostname.endswith(".example.invalid")
    )
