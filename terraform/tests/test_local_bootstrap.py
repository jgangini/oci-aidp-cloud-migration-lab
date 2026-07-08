from types import SimpleNamespace

import pytest

from scripts.bootstrap_local_oci_env import (
    one_named,
    platform_endpoint,
    platform_workspace_name,
)


def test_local_bootstrap_uses_exact_bucket_name() -> None:
    buckets = [
        SimpleNamespace(name="aidp-data-other"),
        SimpleNamespace(name="aidp-data-selected"),
    ]

    assert one_named("bucket", buckets, "aidp-data-selected").name == "aidp-data-selected"


def test_local_bootstrap_uses_alias_and_deterministic_workspace_fallbacks() -> None:
    platform = SimpleNamespace(
        alias_key="workbench-alias",
        default_workspace_name=None,
        display_name="aidp-lab-selected",
        web_socket_endpoint=None,
    )

    assert platform_endpoint(platform, "us-chicago-1") == "workbench-aliasord"
    assert platform_workspace_name(platform, "selected") == "aidp-lab-workspace-selected"


def test_local_bootstrap_rejects_workspace_fallback_for_another_platform() -> None:
    platform = SimpleNamespace(
        default_workspace_name=None,
        display_name="aidp-lab-another",
    )

    with pytest.raises(RuntimeError, match="does not match"):
        platform_workspace_name(platform, "selected")
