"""Small AIDP Workbench client used by the registration service.

The shared workspace is intentional: every developer can inspect material under
/Shared/users. Names are deterministic so retries add missing material instead
of creating duplicate folders, jobs, or notebooks.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from oci._vendor import requests

from .config import Settings


API_VERSION = "20260430"
SHARED_COMPUTE_NAME = "aidp_lab_shared_compute"
INDUSTRIES = ("banking", "telecommunications", "retail", "healthcare")


class AidpProvisionPending(Exception):
    pass


class AidpProvisionError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class UserMaterial:
    email: str
    industry: str
    workspace_path: str
    job_name: str


def normalized_user_key(email: str) -> str:
    """Return the deterministic, readable folder name for an email address."""
    return re.sub(r"[^a-z0-9]+", "-", email.casefold()).strip("-")[:96]


def csv_samples(industry: str) -> dict[str, str]:
    if industry not in INDUSTRIES:
        raise ValueError("Choose banking, telecommunications, retail, or healthcare")
    data = {
        "banking": {
            "customers.csv": "record_id,event_time,entity,category,amount,region\nBK-C001,2026-01-01T09:00:00Z,customer,consumer,0.00,north\n",
            "accounts.csv": "record_id,event_time,entity,category,amount,region\nBK-A001,2026-01-01T09:10:00Z,account,checking,1250.00,north\n",
            "transactions.csv": "record_id,event_time,entity,category,amount,region\nBK-T001,2026-01-01T10:00:00Z,transaction,card,42.50,north\n",
            "branches.csv": "record_id,event_time,entity,category,amount,region\nBK-B001,2026-01-01T11:00:00Z,branch,urban,0.00,north\n",
        },
        "telecommunications": {
            "customers.csv": "record_id,event_time,entity,category,amount,region\nTC-C001,2026-01-01T09:00:00Z,subscriber,postpaid,0.00,central\n",
            "plans.csv": "record_id,event_time,entity,category,amount,region\nTC-P001,2026-01-01T09:10:00Z,plan,5g,55.00,central\n",
            "usage_events.csv": "record_id,event_time,entity,category,amount,region\nTC-U001,2026-01-01T10:00:00Z,usage,data_gb,3.20,central\n",
            "network_sites.csv": "record_id,event_time,entity,category,amount,region\nTC-N001,2026-01-01T11:00:00Z,site,metro,0.00,central\n",
        },
        "retail": {
            "customers.csv": "record_id,event_time,entity,category,amount,region\nRT-C001,2026-01-01T09:00:00Z,shopper,loyalty,0.00,west\n",
            "products.csv": "record_id,event_time,entity,category,amount,region\nRT-P001,2026-01-01T09:10:00Z,product,home,25.00,west\n",
            "orders.csv": "record_id,event_time,entity,category,amount,region\nRT-O001,2026-01-01T10:00:00Z,order,online,75.00,west\n",
            "store_sales.csv": "record_id,event_time,entity,category,amount,region\nRT-S001,2026-01-01T11:00:00Z,sale,store,33.00,west\n",
        },
        "healthcare": {
            "patients.csv": "record_id,event_time,entity,category,amount,region\nHC-P001,2026-01-01T09:00:00Z,member,preventive,0.00,east\n",
            "providers.csv": "record_id,event_time,entity,category,amount,region\nHC-R001,2026-01-01T09:10:00Z,provider,primary_care,0.00,east\n",
            "appointments.csv": "record_id,event_time,entity,category,amount,region\nHC-A001,2026-01-01T10:00:00Z,appointment,consultation,90.00,east\n",
            "encounters.csv": "record_id,event_time,entity,category,amount,region\nHC-E001,2026-01-01T11:00:00Z,encounter,outpatient,140.00,east\n",
        },
    }
    return data[industry]


def user_notebooks(industry: str, user_key: str) -> dict[str, dict[str, Any]]:
    """Generate executable, skimmable medallion tutorial notebooks."""
    source = f"/Workspace/Shared/users/{user_key}/data/{industry}"
    landing = f"/Volumes/aidp_lab/landing/landing_data/users/{user_key}/{industry}"
    bronze = f"/Volumes/aidp_lab/bronze/bronze_data/users/{user_key}/{industry}"
    silver = f"/Volumes/aidp_lab/silver/silver_data/users/{user_key}/{industry}"
    gold = f"/Volumes/aidp_lab/gold/gold_data/users/{user_key}/{industry}"

    def markdown(text: str) -> dict[str, Any]:
        return {"cell_type": "markdown", "metadata": {}, "source": text.splitlines(True)}

    def code(text: str) -> dict[str, Any]:
        return {
            "cell_type": "code",
            "metadata": {},
            "execution_count": None,
            "outputs": [],
            "source": text.splitlines(True),
        }

    def notebook(title: str, narrative: str, program: str) -> dict[str, Any]:
        return {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {"language_info": {"name": "python"}, "aidp_lab": {"industry": industry}},
            "cells": [markdown(f"# {title}\n\n{narrative}\n"), code(program)],
        }

    schema = """from pyspark.sql.types import StructType, StructField, StringType, TimestampType, DoubleType
schema = StructType([
    StructField("record_id", StringType(), False),
    StructField("event_time", TimestampType(), True),
    StructField("entity", StringType(), True),
    StructField("category", StringType(), True),
    StructField("amount", DoubleType(), True),
    StructField("region", StringType(), True),
])
"""
    return {
        f"01_nb_landing_{industry}.ipynb": notebook(
            f"01 Landing — {industry.title()}",
            "Copies the four synthetic source CSV files from the Workspace into Landing. Re-running is safe.",
            f"""source = {source!r}
landing = {landing!r}
csv_files = [item for item in dbutils.fs.ls(source) if item.path.endswith(".csv")]
for csv_file in csv_files:
    dbutils.fs.cp(csv_file.path, f"{{landing}}/{{csv_file.name}}", True)
assert len(csv_files) == 4
print(f"Landing ready: {{landing}}")
""",
        ),
        f"02_nb_bronze_{industry}.ipynb": notebook(
            f"02 Bronze — {industry.title()}",
            "Reads Landing with an explicit schema, adds lineage, and writes Delta data to Bronze.",
            f"""from pyspark.sql import functions as F
{schema}
landing = {landing!r}
bronze = {bronze!r}
for csv_file in dbutils.fs.ls(landing):
    if not csv_file.path.endswith(".csv"):
        continue
    dataset = csv_file.name.removesuffix(".csv")
    frame = (spark.read.option("header", True).schema(schema).csv(csv_file.path)
        .withColumn("_source_file", F.input_file_name())
        .withColumn("_ingested_at", F.current_timestamp()))
    frame.write.format("delta").mode("overwrite").save(f"{{bronze}}/{{dataset}}")
print("Bronze quality and lineage fields written.")
""",
        ),
        f"03_nb_silver_{industry}.ipynb": notebook(
            f"03 Silver — {industry.title()}",
            "Applies simple quality rules: required IDs, normalized dimensions, and deterministic deduplication.",
            f"""from pyspark.sql import functions as F
bronze = {bronze!r}
silver = {silver!r}
for dataset in [item.name.rstrip("/") for item in dbutils.fs.ls(bronze)]:
    frame = spark.read.format("delta").load(f"{{bronze}}/{{dataset}}")
    clean = (frame.filter(F.col("record_id").isNotNull())
        .withColumn("category", F.trim(F.lower("category")))
        .withColumn("region", F.trim(F.lower("region")))
        .dropDuplicates(["record_id"]))
    clean.write.format("delta").mode("overwrite").save(f"{{silver}}/{{dataset}}")
print("Silver quality rules completed.")
""",
        ),
        f"04_nb_gold_{industry}.ipynb": notebook(
            f"04 Gold — {industry.title()}",
            "Creates a small KPI aggregate by category and region for the selected industry.",
            f"""from functools import reduce
from pyspark.sql import functions as F
silver = {silver!r}
gold = {gold!r}
frames = [spark.read.format("delta").load(f"{{silver}}/{{item.name.rstrip('/')}}") for item in dbutils.fs.ls(silver)]
all_records = reduce(lambda left, right: left.unionByName(right, allowMissingColumns=True), frames)
kpis = (all_records.groupBy("category", "region")
    .agg(F.count("*").alias("record_count"), F.round(F.sum("amount"), 2).alias("total_amount"))
    .withColumn("industry", F.lit({industry!r})))
kpis.write.format("delta").mode("overwrite").save(f"{{gold}}/kpis")
display(kpis.orderBy("category", "region"))
""",
        ),
    }


class LocalAidpClient:
    """In-memory AIDP adapter for the Docker development and test profile."""

    def __init__(self, _: Settings) -> None:
        self.users: dict[str, UserMaterial] = {}

    async def close(self) -> None:
        return None

    async def provision_user(self, email: str, industry: str) -> UserMaterial:
        key = normalized_user_key(email)
        csv_samples(industry)
        user_notebooks(industry, key)
        material = UserMaterial(email, industry, f"/Shared/users/{key}/data/{industry}", f"aidp-lab-medallion-{industry}-{key}")
        self.users[email.casefold()] = material
        return material

    async def cleanup_user(self, email: str) -> None:
        self.users.pop(email.casefold(), None)


class AidpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base = f"https://aidp.{settings.aidp_region}.oci.oraclecloud.com/{API_VERSION}/aiDataPlatforms/{settings.aidp_platform_id}"
        import oci

        self.signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        self.session = requests.Session()

    async def close(self) -> None:
        self.session.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        request_headers = {"Accept": "application/json", **(headers or {})}
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
        if method.upper() == "POST":
            request_headers["opc-retry-token"] = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"{path}:{json.dumps(payload or {}, sort_keys=True)}")
            )
        try:
            response = self.session.request(
                method,
                f"{self.base}{path}",
                auth=self.signer,
                json=payload,
                data=data,
                headers=request_headers,
                timeout=(10, 60),
            )
        except requests.exceptions.RequestException as exc:
            raise AidpProvisionPending("AIDP is still accepting the requested material. Retry shortly.") from exc
        if response.status_code in {408, 409, 429, 500, 502, 503, 504}:
            raise AidpProvisionPending("AIDP is still reconciling the requested material. Retry shortly.")
        if response.status_code == 404 and allow_not_found:
            return None
        if response.status_code >= 400:
            raise AidpProvisionError(
                f"AIDP could not complete this operation ({response.status_code}). Check the AIDP policy and retry."
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def _list(self, path: str) -> list[dict[str, Any]]:
        body = self._request("GET", path)
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            return list(body.get("items") or body.get("Items") or [])
        return []

    def _workspace(self) -> dict[str, Any]:
        workspaces = [
            item
            for item in self._list("/workspaces")
            if item.get("displayName") == self.settings.aidp_workspace_name
        ]
        if len(workspaces) != 1:
            raise AidpProvisionPending("The default AIDP workspace is not ready yet. Retry shortly.")
        return workspaces[0]

    def _shared_compute(self, workspace_key: str) -> dict[str, Any]:
        clusters = [
            item
            for item in self._list(f"/workspaces/{workspace_key}/clusters")
            if item.get("displayName") == SHARED_COMPUTE_NAME
        ]
        if len(clusters) != 1:
            raise AidpProvisionPending("The shared AIDP compute is not ready yet. Retry shortly.")
        return clusters[0]

    def _ensure_folder(self, workspace_key: str, path: str) -> None:
        self._request(
            "POST",
            f"/workspaces/{workspace_key}/objects",
            data=b"",
            headers={
                "path": path,
                "type": "FOLDER",
                "is-overwrite": "true",
                "Content-Type": "application/octet-stream",
            },
        )

    def _upload_file(self, workspace_key: str, path: str, content: bytes) -> None:
        self._request(
            "POST",
            f"/workspaces/{workspace_key}/objects",
            data=content,
            headers={
                "path": path,
                "type": "FILE",
                "is-overwrite": "true",
                "Content-Type": "application/octet-stream",
            },
        )

    def _upload_notebook(self, workspace_key: str, path: str, notebook: dict[str, Any]) -> None:
        content_path = f"/workspaces/{workspace_key}/notebook/api/contents/{quote(path, safe='')}"
        if self._request("GET", content_path, allow_not_found=True) is None:
            parent = path.rsplit("/", 1)[0]
            created = self._request(
                "POST",
                f"/workspaces/{workspace_key}/notebook/api/contents/{quote(parent, safe='')}",
                payload={"copy_from": None, "ext": ".ipynb", "type": "notebook", "freeformTags": None, "definedTags": None},
            )
            created_path = str((created or {}).get("path") or "")
            if not created_path:
                raise AidpProvisionError("AIDP did not return a notebook path. Retry shortly.")
            self._request(
                "PATCH",
                f"/workspaces/{workspace_key}/notebook/api/contents/{quote(created_path, safe='')}",
                payload={"path": path},
            )
        self._request(
            "PUT",
            content_path,
            payload={
                "name": path.rsplit("/", 1)[-1],
                "path": path,
                "type": "notebook",
                "content": notebook,
                "format": "json",
            },
        )

    def _ensure_job(
        self,
        workspace_key: str,
        compute_key: str,
        user_key: str,
        industry: str,
        notebooks: dict[str, dict[str, Any]],
    ) -> str:
        job_name = f"aidp-lab-medallion-{industry}-{user_key}"
        jobs = [
            item
            for item in self._list(f"/workspaces/{workspace_key}/jobs")
            if item.get("displayName") == job_name or item.get("name") == job_name
        ]
        if len(jobs) > 1:
            raise AidpProvisionError("AIDP has duplicate jobs for this lab user. Resolve them before retrying.")
        job_key = str((jobs[0] if jobs else {}).get("key") or "")
        if not job_key:
            created = self._request(
                "POST",
                f"/workspaces/{workspace_key}/jobs",
                payload={
                    "name": job_name,
                    "path": f"/Workspace/Shared/users/{user_key}/workflows",
                    "description": f"{industry.title()} medallion tutorial for {user_key}",
                    "maxConcurrentRuns": 1,
                },
            )
            job_key = str((created or {}).get("key") or "")
            if not job_key:
                raise AidpProvisionError("AIDP did not return a workflow key. Retry shortly.")
        tasks: list[dict[str, Any]] = []
        for index, notebook_name in enumerate(notebooks):
            task_key = f"stage_{index + 1}"
            tasks.append(
                {
                    "type": "NOTEBOOK_TASK",
                    "taskKey": task_key,
                    "dependsOn": [] if index == 0 else [{"taskKey": f"stage_{index}"}],
                    "runIf": "ALL_SUCCESS",
                    "maxRetries": 0,
                    "isRetryOnTimeout": False,
                    "notebookPath": f"/Workspace/Shared/users/{user_key}/notebooks/{notebook_name}",
                    "cluster": {"clusterKey": compute_key},
                    "parameters": [],
                }
            )
        self._request(
            "PUT",
            f"/workspaces/{workspace_key}/jobs/{job_key}",
            payload={
                "name": job_name,
                "path": f"/Workspace/Shared/users/{user_key}/workflows",
                "description": f"{industry.title()} medallion tutorial for {user_key}",
                "maxConcurrentRuns": 1,
                "jobClusters": [{"clusterKey": compute_key}],
                "tasks": tasks,
            },
        )
        return job_name

    async def provision_user(self, email: str, industry: str) -> UserMaterial:
        if industry not in INDUSTRIES:
            raise ValueError("Choose banking, telecommunications, retail, or healthcare")
        workspace_key = str(self._workspace()["key"])
        compute_key = str(self._shared_compute(workspace_key)["key"])
        user_key = normalized_user_key(email)
        root = f"/Workspace/Shared/users/{user_key}"
        data_path = f"{root}/data/{industry}"
        notebooks_path = f"{root}/notebooks"
        for path in ("/Workspace/Shared", "/Workspace/Shared/users", root, f"{root}/data", data_path, notebooks_path, f"{root}/workflows"):
            self._ensure_folder(workspace_key, path)
        for name, content in csv_samples(industry).items():
            self._upload_file(workspace_key, f"{data_path}/{name}", content.encode("utf-8"))
        notebooks = user_notebooks(industry, user_key)
        for name, notebook in notebooks.items():
            self._upload_notebook(workspace_key, f"{notebooks_path}/{name}", notebook)
        job_name = self._ensure_job(workspace_key, compute_key, user_key, industry, notebooks)
        return UserMaterial(email, industry, f"/Shared/users/{user_key}/data/{industry}", job_name)

    async def cleanup_user(self, email: str) -> None:
        workspace_key = str(self._workspace()["key"])
        user_key = normalized_user_key(email)
        jobs = [
            item
            for item in self._list(f"/workspaces/{workspace_key}/jobs")
            if str(item.get("name") or item.get("displayName") or "").endswith(user_key)
        ]
        for job in jobs:
            key = job.get("key") or job.get("id")
            if key:
                self._request("DELETE", f"/workspaces/{workspace_key}/jobs/{key}", allow_not_found=True)
        self._request(
            "DELETE",
            f"/workspaces/{workspace_key}/notebook/api/contents/{quote(f'/Workspace/Shared/users/{user_key}', safe='')}",
            allow_not_found=True,
        )
