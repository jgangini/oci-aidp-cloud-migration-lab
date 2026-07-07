import asyncio
import json

from app.aidp import AidpClient, INDUSTRIES, LocalAidpClient, csv_samples, normalized_user_key, user_notebooks
from app.config import Settings


def test_industry_kits_are_non_pii_and_have_four_csvs() -> None:
    for industry in INDUSTRIES:
        samples = csv_samples(industry)
        assert len(samples) == 4
        assert all("record_id,event_time" in content for content in samples.values())
        assert "@" not in "".join(samples.values())


def test_notebooks_are_valid_and_cover_each_medallion_layer() -> None:
    notebooks = user_notebooks("retail", normalized_user_key("ada@example.com"))
    assert list(notebooks) == [
        "01_nb_landing_retail.ipynb",
        "02_nb_bronze_retail.ipynb",
        "03_nb_silver_retail.ipynb",
        "04_nb_gold_retail.ipynb",
    ]
    rendered = json.dumps(notebooks)
    assert "/Volumes/aidp_lab/landing/landing_data/" in rendered
    assert "/Volumes/aidp_lab/bronze/bronze_data/" in rendered
    assert "/Volumes/aidp_lab/silver/silver_data/" in rendered
    assert "/Volumes/aidp_lab/gold/gold_data/" in rendered
    for notebook in notebooks.values():
        assert notebook["nbformat"] == 4
        assert all("cell_type" in cell for cell in notebook["cells"])
        json.loads(json.dumps(notebook))


def test_email_folder_is_deterministic_and_readable() -> None:
    assert normalized_user_key("Ada.Lovelace+Lab@example.com") == "ada-lovelace-lab-example-com"


def test_live_client_finds_the_reconciled_shared_compute() -> None:
    client = object.__new__(AidpClient)
    client._list = lambda _path: [{"displayName": "aidp_lab_shared_compute", "key": "cluster-key"}]
    assert client._shared_compute("workspace-key")["key"] == "cluster-key"


def test_live_client_scopes_notebook_and_job_requests_to_the_workspace() -> None:
    client = object.__new__(AidpClient)
    calls: list[tuple[str, str, dict | None]] = []

    def request(method: str, path: str, *, payload=None, **_kwargs):
        calls.append((method, path, payload))
        if method == "GET":
            return None
        if method == "POST" and "/notebook/" in path:
            return {"path": "/Workspace/Shared/users/ada/notebooks/Untitled.ipynb"}
        if method == "POST" and path.endswith("/jobs"):
            return {"key": "job-key"}
        return {}

    client._request = request
    client._list = lambda _path: []
    notebook_name, notebook = next(iter(user_notebooks("banking", "ada-example-com").items()))
    client._upload_notebook("workspace-key", f"/Workspace/Shared/users/ada/notebooks/{notebook_name}", notebook)
    client._ensure_job("workspace-key", "cluster-key", "ada", "banking", {notebook_name: notebook})

    assert all("/workspaces/workspace-key/" in path for _, path, _ in calls)
    assert any(method == "PUT" and path.endswith("/jobs/job-key") for method, path, _ in calls)


def test_local_aidp_provision_is_idempotent_and_cleanup_is_explicit() -> None:
    asyncio.run(_local_aidp_lifecycle())


async def _local_aidp_lifecycle() -> None:
    client = LocalAidpClient(Settings(local_development_mode=True))
    first = await client.provision_user("Ada@example.com", "healthcare")
    second = await client.provision_user("ada@example.com", "healthcare")
    assert first.workspace_path == second.workspace_path
    assert len(client.users) == 1
    await client.cleanup_user("ada@example.com")
    assert client.users == {}
