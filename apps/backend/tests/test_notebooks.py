import ast
import json

import pytest

from app.industry_kits import DATASET_SPECS, GOLD_SPECS, INDUSTRIES
from app.notebooks import user_notebooks


PARTICIPANT = "u_0123456789abcdef"
BUCKET = "aidp-lab-4"
NAMESPACE = "tenantnamespace"


def _rendered(industry: str) -> tuple[dict, str]:
    notebooks = user_notebooks(industry, PARTICIPANT, BUCKET, NAMESPACE)
    return notebooks, json.dumps(notebooks, sort_keys=True)


@pytest.mark.parametrize("industry", INDUSTRIES)
def test_notebooks_are_deterministic_valid_tutorials_with_exact_names(industry: str) -> None:
    notebooks, rendered = _rendered(industry)
    assert list(notebooks) == [
        f"01_landing_{industry}.ipynb",
        f"02_bronze_{industry}.ipynb",
        f"03_silver_{industry}.ipynb",
        f"04_gold_{industry}.ipynb",
    ]
    assert rendered == json.dumps(
        user_notebooks(industry, PARTICIPANT, BUCKET, NAMESPACE), sort_keys=True
    )

    for name, notebook in notebooks.items():
        assert notebook["nbformat"] == 4
        assert notebook["nbformat_minor"] == 5
        assert json.loads(json.dumps(notebook)) == notebook
        assert len({cell["id"] for cell in notebook["cells"]}) == len(notebook["cells"])
        markdown = "".join(
            "".join(cell["source"])
            for cell in notebook["cells"]
            if cell["cell_type"] == "markdown"
        )
        assert "Learning goal" in markdown
        assert "Exercise" in markdown
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                compile("".join(cell["source"]), name, "exec")


@pytest.mark.parametrize("industry", INDUSTRIES)
def test_generated_dataset_specs_are_executable_python(industry: str) -> None:
    notebooks, _ = _rendered(industry)
    for layer in ("01_landing", "02_bronze", "03_silver"):
        notebook = notebooks[f"{layer}_{industry}.ipynb"]
        program = "".join(
            "".join(cell["source"])
            for cell in notebook["cells"]
            if cell["cell_type"] == "code"
        )
        assignment = next(
            node
            for node in ast.parse(program).body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "specs" for target in node.targets)
        )
        namespace: dict[str, object] = {}
        exec(compile(ast.Module(body=[assignment], type_ignores=[]), layer, "exec"), namespace)
        specs = namespace["specs"]
        assert list(specs) == list(DATASET_SPECS[industry])
        assert all(
            isinstance(column["required"], bool)
            for spec in specs.values()
            for column in spec["columns"]
        )


@pytest.mark.parametrize("industry", INDUSTRIES)
def test_notebooks_use_only_participant_workspace_and_object_storage_paths(industry: str) -> None:
    _, rendered = _rendered(industry)
    assert f"/Workspace/lab-users/{PARTICIPANT}/{industry}/source" in rendered
    for prefix in ("01_landing", "02_bronze", "03_silver", "04_gold"):
        assert (
            f"oci://{BUCKET}@{NAMESPACE}/{prefix}/users/{PARTICIPANT}/{industry}/"
            in rendered
        )
    for dataset in DATASET_SPECS[industry].values():
        assert dataset["filename"] in rendered

    forbidden = (
        "dbutils",
        "pip install",
        "sparksession",
        "display(",
        "bearer ",
        "private key",
        "key.pem",
        "/.oci",
        "password",
    )
    lowered = rendered.lower()
    assert all(token not in lowered for token in forbidden)


def test_banking_registers_exactly_fifteen_external_tables() -> None:
    notebooks, _ = _rendered("banking")
    programs = {
        name: "".join(
            "".join(cell["source"])
            for cell in notebook["cells"]
            if cell["cell_type"] == "code"
        )
        for name, notebook in notebooks.items()
    }
    ddl_counts = {
        name: program.count("CREATE EXTERNAL TABLE IF NOT EXISTS")
        for name, program in programs.items()
    }
    assert list(ddl_counts.values()) == [0, 8, 5, 2]
    assert sum(ddl_counts.values()) == 15
    all_programs = "\n".join(programs.values())
    assert "USING CSV OPTIONS (header 'true') LOCATION" in all_programs
    assert "USING DELTA LOCATION" in all_programs
    assert f"aidp_lab.{PARTICIPANT}_landing.branches" in all_programs
    assert f"aidp_lab.{PARTICIPANT}_silver.quality_issues" in all_programs


def test_banking_customer_metrics_use_a_real_thirty_day_window() -> None:
    notebooks, _ = _rendered("banking")
    program = "".join(
        "".join(cell["source"])
        for cell in notebooks["04_gold_banking.ipynb"]["cells"]
        if cell["cell_type"] == "code"
    )
    assert 'F.max("event_time").alias("max_event_time")' in program
    assert 'F.expr("INTERVAL 30 DAYS")' in program
    assert "transactions_30d.join(" in program


@pytest.mark.parametrize("industry", INDUSTRIES)
def test_silver_contract_normalizes_validates_and_quarantines(industry: str) -> None:
    notebooks, _ = _rendered(industry)
    program = "".join(
        "".join(cell["source"])
        for cell in notebooks[f"03_silver_{industry}.ipynb"]["cells"]
        if cell["cell_type"] == "code"
    )
    for marker in (
        "F.trim",
        "F.lower",
        "spark_types",
        "Window.partitionBy",
        "updated_at",
        "foreign_keys",
        "invalid_enum",
        "invalid_range",
        "invalid_time_order",
        "quality_issues",
        "assert quality_count > 0",
        "assert clean_count <= bronze_count",
    ):
        assert marker in program
    specs_assignment = next(
        node
        for node in ast.parse(program).body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "specs" for target in node.targets)
    )
    namespace: dict[str, object] = {}
    exec(
        compile(ast.Module(body=[specs_assignment], type_ignores=[]), "silver", "exec"),
        namespace,
    )
    assert list(namespace["specs"]) == list(DATASET_SPECS[industry])  # parents precede dependents
    assert len(DATASET_SPECS[industry]) == 4
    assert program.count('.mode("overwrite")') == 2  # four datasets in one loop plus quarantine


@pytest.mark.parametrize("industry", INDUSTRIES)
def test_gold_uses_exact_industry_table_names_and_safe_overwrites(industry: str) -> None:
    notebooks, _ = _rendered(industry)
    program = "".join(
        "".join(cell["source"])
        for cell in notebooks[f"04_gold_{industry}.ipynb"]["cells"]
        if cell["cell_type"] == "code"
    )
    expected_tables = {f"{industry}_{name}" for name in GOLD_SPECS[industry]}
    assert len(expected_tables) == 2
    assert all(f".{table} (" in program for table in expected_tables)
    assert program.count("CREATE EXTERNAL TABLE IF NOT EXISTS") == 2
    assert program.count('.mode("overwrite")') == 2
    assert program.count('option("overwriteSchema", "true")') == 2
    assert 'assert row_count > 0' in program


@pytest.mark.parametrize(
    ("participant, bucket, namespace"),
    (("email@example.com", BUCKET, NAMESPACE), (PARTICIPANT, "bad'", NAMESPACE), (PARTICIPANT, BUCKET, "")),
)
def test_notebook_paths_reject_unsafe_components(
    participant: str, bucket: str, namespace: str
) -> None:
    with pytest.raises(ValueError):
        user_notebooks("banking", participant, bucket, namespace)
