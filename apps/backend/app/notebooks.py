"""Generate the four participant-specific AIDP medallion notebooks."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from .industry_kits import DATASET_SPECS, GOLD_SPECS, INDUSTRIES


LAYER_PREFIXES = {
    "landing": "01_landing",
    "bronze": "02_bronze",
    "silver": "03_silver",
    "gold": "04_gold",
}

WORKSPACE_ROOT = "/Workspace/medallon"

_ENUM_VALUES = {
    "banking": {
        "branches.branch_type": ("urban", "suburban", "digital"),
        "branches.region": ("north", "south", "east", "west"),
        "branches.status": ("active",),
        "customers.customer_type": ("individual", "small_business"),
        "customers.segment": ("mass", "affluent", "business"),
        "customers.region": ("north", "south", "east", "west"),
        "customers.risk_band": ("low", "medium", "high"),
        "customers.status": ("active",),
        "accounts.account_type": ("checking", "savings", "credit"),
        "accounts.currency": ("usd",),
        "accounts.status": ("active",),
        "transactions.transaction_type": ("debit", "credit", "refund"),
        "transactions.channel": ("card", "mobile", "atm", "branch"),
        "transactions.merchant_category": ("grocery", "travel", "utilities", "services"),
        "transactions.currency": ("usd",),
        "transactions.status": ("posted",),
    },
    "telecommunications": {
        "plans.plan_type": ("prepaid", "postpaid", "business"),
        "plans.status": ("active",),
        "network_sites.region": ("north", "south", "central", "coastal"),
        "network_sites.technology": ("4g", "5g"),
        "network_sites.status": ("active",),
        "subscribers.segment": ("consumer", "family", "business"),
        "subscribers.region": ("north", "south", "central", "coastal"),
        "subscribers.status": ("active",),
        "usage_events.usage_type": ("data", "voice", "sms"),
        "usage_events.usage_unit": ("mb", "minutes", "messages"),
    },
    "retail": {
        "customers.segment": ("consumer", "small_business"),
        "customers.region": ("north", "south", "east", "west"),
        "customers.loyalty_tier": ("standard", "silver", "gold"),
        "customers.status": ("active",),
        "products.category": ("home", "electronics", "apparel", "grocery"),
        "products.status": ("active",),
        "orders.channel": ("web", "mobile", "store"),
        "orders.region": ("north", "south", "east", "west"),
        "orders.currency": ("usd",),
        "orders.order_status": ("completed", "refunded"),
    },
    "healthcare": {
        "patients.age_band": ("0-17", "18-39", "40-64", "65+"),
        "patients.region": ("north", "south", "east", "west"),
        "patients.coverage_type": ("public", "private", "self_pay"),
        "patients.risk_band": ("low", "medium", "high"),
        "patients.status": ("active",),
        "providers.specialty": ("primary_care", "cardiology", "orthopedics", "pediatrics"),
        "providers.region": ("north", "south", "east", "west"),
        "providers.facility_type": ("clinic", "hospital", "virtual"),
        "providers.status": ("active",),
        "appointments.appointment_type": ("consultation", "follow_up", "preventive"),
        "appointments.status": ("completed", "no_show", "scheduled"),
        "encounters.diagnosis_group": ("respiratory", "cardiovascular", "musculoskeletal", "preventive"),
        "encounters.procedure_group": ("evaluation", "imaging", "therapy", "screening"),
        "encounters.disposition": ("outpatient",),
    },
}

_STRICT_POSITIVE = {
    "banking": ("transactions.amount",),
    "telecommunications": ("network_sites.capacity_mb_day", "usage_events.usage_value"),
    "retail": ("products.unit_cost", "products.list_price", "order_items.quantity", "order_items.unit_price"),
    "healthcare": ("providers.daily_capacity", "encounters.cost_amount"),
}

_PATH_COMPONENT = re.compile(r"[A-Za-z0-9._-]+")

_TEMPORAL_ORDER = {
    "healthcare": (
        ("appointments", "scheduled_start", "scheduled_end"),
        ("encounters", "encounter_start", "encounter_end"),
    ),
}


def schema_name(layer: str) -> str:
    if layer not in LAYER_PREFIXES:
        raise ValueError(f"Unknown medallion layer: {layer}")
    return f"oci_{layer}"


def participant_folder(email: str) -> str:
    normalized = email.strip().casefold()
    if (
        normalized.count("@") != 1
        or len(normalized) > 254
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError("A valid participant email is required")
    return quote(normalized, safe="@._+-")


def workspace_participant_root(email: str) -> str:
    return f"{WORKSPACE_ROOT}/{participant_folder(email)}"


def workspace_root(email: str, industry: str) -> str:
    if industry not in INDUSTRIES:
        raise ValueError("Choose banking, telecommunications, retail, or healthcare")
    return f"{workspace_participant_root(email)}/{industry}"


def table_name(participant_key: str, industry: str, dataset: str) -> str:
    suffix = dataset if dataset.startswith(f"{industry}_") else f"{industry}_{dataset}"
    return f"{participant_key}_{suffix}"


def table_names(participant_key: str, industry: str, layer: str) -> tuple[str, ...]:
    if layer not in LAYER_PREFIXES:
        raise ValueError(f"Unknown medallion layer: {layer}")
    if industry not in INDUSTRIES:
        raise ValueError("Choose banking, telecommunications, retail, or healthcare")
    logical_names = list(DATASET_SPECS[industry])
    if layer == "silver":
        logical_names.append("quality_issues")
    elif layer == "gold":
        logical_names = [f"{industry}_{name}" for name in GOLD_SPECS[industry]]
    return tuple(table_name(participant_key, industry, name) for name in logical_names)


def participant_table_names(participant_key: str, layer: str) -> frozenset[str]:
    return frozenset(
        name
        for industry in INDUSTRIES
        for name in table_names(participant_key, industry, layer)
    )


def layer_uri(
    bucket: str,
    namespace: str,
    layer: str,
    participant_key: str,
    industry: str,
    dataset: str,
) -> str:
    prefix = LAYER_PREFIXES[layer]
    return f"oci://{bucket}@{namespace}/{prefix}/users/{participant_key}/{industry}/{dataset}/"


def _validate_inputs(industry: str, participant_key: str, bucket: str, namespace: str) -> None:
    if industry not in INDUSTRIES:
        raise ValueError("Choose banking, telecommunications, retail, or healthcare")
    if re.fullmatch(r"u_[0-9a-f]{16}", participant_key) is None:
        raise ValueError("participant_key must match u_<16 lowercase hex characters>")
    if not bucket or _PATH_COMPONENT.fullmatch(bucket) is None:
        raise ValueError("bucket must be a non-empty OCI path component")
    if not namespace or _PATH_COMPONENT.fullmatch(namespace) is None:
        raise ValueError("namespace must be a non-empty OCI path component")


def _ddl_statements(
    industry: str,
    participant_key: str,
    bucket: str,
    namespace: str,
    layer: str,
) -> str:
    tables: list[tuple[str, list[tuple[str, str]], str, str]] = []
    if layer in {"landing", "bronze", "silver"}:
        for dataset, spec in DATASET_SPECS[industry].items():
            fields = [
                (column["name"], "STRING" if layer in {"landing", "bronze"} else column["type"])
                for column in spec["columns"]
            ]
            if layer == "bronze":
                fields += [("_source_file", "STRING"), ("_ingested_at", "TIMESTAMP")]
            tables.append((dataset, fields, layer_uri(bucket, namespace, layer, participant_key, industry, dataset), "CSV" if layer == "landing" else "DELTA"))
        if layer == "silver":
            tables.append((
                "quality_issues",
                [
                    ("participant_key", "STRING"), ("industry", "STRING"),
                    ("dataset", "STRING"), ("source_row_id", "STRING"),
                    ("record_key", "STRING"), ("reason_codes", "STRING"),
                    ("raw_payload_json", "STRING"), ("quarantined_at", "TIMESTAMP"),
                ],
                layer_uri(bucket, namespace, layer, participant_key, industry, "quality_issues"),
                "DELTA",
            ))
    else:
        for short_name, fields in GOLD_SPECS[industry].items():
            logical_table_name = f"{industry}_{short_name}"
            tables.append((logical_table_name, list(fields), layer_uri(bucket, namespace, layer, participant_key, industry, logical_table_name), "DELTA"))
    schema = schema_name(layer)
    statements = []
    for dataset, fields, location, data_format in tables:
        physical_table_name = table_name(participant_key, industry, dataset)
        columns = ", ".join(f"`{name}` {kind}" for name, kind in fields)
        options = " OPTIONS (header 'true')" if data_format == "CSV" else ""
        statements.append(
            "spark.sql(\"\"\"CREATE EXTERNAL TABLE IF NOT EXISTS "
            f"aidp_lab.{schema}.{physical_table_name} ({columns}) USING {data_format}{options} "
            f"LOCATION '{location}'\"\"\")"
        )
    return "\n".join(statements)


def _markdown(cell_id: str, text: str) -> dict[str, Any]:
    return {
        "cell_type": "markdown",
        "id": cell_id,
        "metadata": {},
        "source": text.splitlines(True),
    }


def _code(cell_id: str, text: str) -> dict[str, Any]:
    return {
        "cell_type": "code",
        "id": cell_id,
        "metadata": {},
        "execution_count": None,
        "outputs": [],
        "source": text.splitlines(True),
    }


def _notebook(industry: str, title: str, narrative: str, program: str, expected: str) -> dict[str, Any]:
    introduction = f"""# {title}

**Audience:** participants learning the AIDP medallion pattern with PySpark.

**Prerequisites:** use the lab's shared compute and run the previous notebook first.

**Learning goal:** {narrative}

## Outline

1. Inspect the participant-scoped inputs.
2. Transform and persist this medallion layer.
3. Register external tables when this layer owns them.
4. Verify the row counts printed by the final statements.
"""
    verification = f"""## Expected result

{expected}

**Exercise:** rerun this notebook and confirm that counts do not increase. All writes use
participant-exclusive paths and overwrite mode, so a second run is idempotent.

**Common pitfall:** do not replace the participant paths with shared locations. That would mix
different students' data. As an extension, query the registered tables with `spark.sql`.
"""
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"display_name": "PySpark", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
            "aidp_lab": {"industry": industry},
        },
        "cells": [
            _markdown("introduction", introduction),
            _code("medallion-program", program),
            _markdown("expected-result", verification),
        ],
    }


def _serializable_specs(industry: str) -> dict[str, Any]:
    return {
        name: {
            "filename": spec["filename"],
            "primary_key": list(spec["primary_key"]),
            "foreign_keys": [list(item) for item in spec["foreign_keys"]],
            "columns": list(spec["columns"]),
        }
        for name, spec in DATASET_SPECS[industry].items()
    }


def _landing_program(
    industry: str,
    participant_key: str,
    email: str,
    bucket: str,
    namespace: str,
) -> str:
    source = f"{workspace_root(email, industry)}/source"
    specs = _serializable_specs(industry)
    destinations = {
        name: layer_uri(bucket, namespace, "landing", participant_key, industry, name)
        for name in specs
    }
    return f'''from pathlib import Path
from pyspark.sql.types import StringType, StructField, StructType

source_root = Path({source!r})
specs = {specs!r}
destinations = {json.dumps(destinations, sort_keys=True)}
source_files = sorted(source_root.glob("*.csv"))
assert {{item.name for item in source_files}} == {{spec["filename"] for spec in specs.values()}}

for dataset, spec in specs.items():
    source_file = source_root / spec["filename"]
    raw_schema = StructType([StructField(column["name"], StringType(), True) for column in spec["columns"]])
    frame = spark.read.option("header", True).schema(raw_schema).csv(str(source_file))
    source_count = frame.count()
    frame.write.mode("overwrite").option("header", True).csv(destinations[dataset])
    landing_count = spark.read.option("header", True).schema(raw_schema).csv(destinations[dataset]).count()
    assert landing_count == source_count, f"Landing count mismatch for {{dataset}}"
    print(f"Landing {{dataset}}: {{landing_count}} rows")
'''


def _bronze_program(industry: str, participant_key: str, bucket: str, namespace: str) -> str:
    specs = _serializable_specs(industry)
    sources = {
        name: layer_uri(bucket, namespace, "landing", participant_key, industry, name)
        for name in specs
    }
    destinations = {
        name: layer_uri(bucket, namespace, "bronze", participant_key, industry, name)
        for name in specs
    }
    landing_schema = schema_name("landing")
    landing_tables = {
        name: table_name(participant_key, industry, name) for name in specs
    }
    landing_ddl = _ddl_statements(industry, participant_key, bucket, namespace, "landing")
    bronze_ddl = _ddl_statements(industry, participant_key, bucket, namespace, "bronze")
    return f'''from pyspark.sql import functions as F

specs = {specs!r}
sources = {json.dumps(sources, sort_keys=True)}
destinations = {json.dumps(destinations, sort_keys=True)}
landing_tables = {json.dumps(landing_tables, sort_keys=True)}

{landing_ddl}

for dataset, spec in specs.items():
    frame = (spark.table(f"aidp_lab.{landing_schema}.{{landing_tables[dataset]}}")
        .withColumn("_source_file", F.input_file_name())
        .withColumn("_ingested_at", F.current_timestamp()))
    landing_count = frame.count()
    frame.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(destinations[dataset])
    bronze_count = spark.read.format("delta").load(destinations[dataset]).count()
    assert bronze_count == landing_count, f"Bronze count mismatch for {{dataset}}"
    print(f"Bronze {{dataset}}: {{bronze_count}} rows")

{bronze_ddl}
'''


def _silver_program(industry: str, participant_key: str, bucket: str, namespace: str) -> str:
    specs = _serializable_specs(industry)
    sources = {
        name: layer_uri(bucket, namespace, "bronze", participant_key, industry, name)
        for name in specs
    }
    destinations = {
        name: layer_uri(bucket, namespace, "silver", participant_key, industry, name)
        for name in specs
    }
    quality_uri = layer_uri(bucket, namespace, "silver", participant_key, industry, "quality_issues")
    enum_rules: dict[str, dict[str, list[str]]] = {}
    for qualified_name, allowed in _ENUM_VALUES[industry].items():
        dataset, column = qualified_name.split(".", 1)
        enum_rules.setdefault(dataset, {})[column] = list(allowed)
    positive_rules: dict[str, list[str]] = {}
    for qualified_name in _STRICT_POSITIVE.get(industry, ()):
        dataset, column = qualified_name.split(".", 1)
        positive_rules.setdefault(dataset, []).append(column)
    temporal_rules: dict[str, list[list[str]]] = {}
    for dataset, start, end in _TEMPORAL_ORDER.get(industry, ()):
        temporal_rules.setdefault(dataset, []).append([start, end])
    silver_ddl = _ddl_statements(industry, participant_key, bucket, namespace, "silver")
    return f'''from functools import reduce
from pyspark.sql import Window, functions as F

participant_key = {participant_key!r}
industry = {industry!r}
specs = {specs!r}
sources = {json.dumps(sources, sort_keys=True)}
destinations = {json.dumps(destinations, sort_keys=True)}
quality_uri = {quality_uri!r}
spark_types = {{"STRING": "string", "DATE": "date", "TIMESTAMP": "timestamp", "DOUBLE": "double", "BIGINT": "bigint", "BOOLEAN": "boolean"}}
enum_rules = {json.dumps(enum_rules, sort_keys=True)}
positive_rules = {json.dumps(positive_rules, sort_keys=True)}
temporal_rules = {json.dumps(temporal_rules, sort_keys=True)}

typed = {{}}
for dataset, spec in specs.items():
    frame = spark.read.format("delta").load(sources[dataset])
    cast_failure_columns = []
    for column in spec["columns"]:
        name, kind = column["name"], column["type"]
        raw_text = F.trim(F.col(name).cast("string"))
        if kind == "STRING":
            normalized = F.when(raw_text == "", F.lit(None)).otherwise(raw_text)
            if name not in {{"participant_key", "source_row_id"}} and not name.endswith("_id"):
                normalized = F.lower(normalized)
            frame = frame.withColumn(name, normalized)
        else:
            flag = f"_invalid_cast_{{name}}"
            frame = frame.withColumn(
                flag,
                raw_text.isNotNull() & (raw_text != "") & raw_text.cast(spark_types[kind]).isNull(),
            ).withColumn(name, raw_text.cast(spark_types[kind]))
            cast_failure_columns.append(flag)
    cast_invalid = reduce(
        lambda left, name: left | F.col(name), cast_failure_columns, F.lit(False)
    )
    frame = frame.withColumn("_cast_invalid", cast_invalid).drop(*cast_failure_columns)
    typed[dataset] = frame

quality_frames = []
accepted = {{}}
for dataset, spec in specs.items():
    frame = typed[dataset]
    required_checks = [
        F.col(column["name"]).isNull()
        | ((F.col(column["name"]) == "") if column["type"] == "STRING" else F.lit(False))
        for column in spec["columns"] if column["required"]
    ]
    required_invalid = reduce(lambda left, check: left | check, required_checks, F.lit(False))
    key_window = Window.partitionBy(*spec["primary_key"]).orderBy(F.col("updated_at").desc_nulls_last(), F.col("source_row_id").desc())
    frame = (frame.withColumn("_duplicate_rank", F.row_number().over(key_window))
        .withColumn("_fk_invalid", F.lit(False)))
    for local_column, reference_dataset, reference_column in spec["foreign_keys"]:
        marker = f"_ref_{{dataset}}_{{local_column}}"
        reference = accepted[reference_dataset].select(F.col(reference_column).alias(marker)).distinct()
        frame = frame.join(F.broadcast(reference), frame[local_column] == reference[marker], "left")
        frame = frame.withColumn(
            "_fk_invalid",
            F.col("_fk_invalid") | (F.col(local_column).isNotNull() & F.col(marker).isNull()),
        ).drop(marker)
    fk_invalid = F.col("_fk_invalid")
    enum_invalid = reduce(
        lambda left, item: left | (F.col(item[0]).isNotNull() & ~F.col(item[0]).isin(item[1])),
        enum_rules.get(dataset, {{}}).items(),
        F.lit(False),
    )
    range_invalid = reduce(
        lambda left, name: left | (F.col(name).isNotNull() & (F.col(name) <= 0)),
        positive_rules.get(dataset, []),
        F.lit(False),
    )
    temporal_invalid = reduce(
        lambda left, pair: left | (
            F.col(pair[0]).isNotNull()
            & F.col(pair[1]).isNotNull()
            & (F.col(pair[1]) <= F.col(pair[0]))
        ),
        temporal_rules.get(dataset, []),
        F.lit(False),
    )
    scope_invalid = F.col("participant_key") != F.lit(participant_key)
    duplicate_invalid = F.col("_duplicate_rank") > 1
    quality_invalid = (
        required_invalid | F.col("_cast_invalid") | fk_invalid | enum_invalid
        | range_invalid | temporal_invalid | scope_invalid | duplicate_invalid
    )
    frame = frame.withColumn("_quality_invalid", quality_invalid)
    reason = F.concat_ws(",",
        F.when(required_invalid, F.lit("required_value")),
        F.when(F.col("_cast_invalid"), F.lit("invalid_type")),
        F.when(fk_invalid, F.lit("foreign_key")),
        F.when(enum_invalid, F.lit("invalid_enum")),
        F.when(range_invalid, F.lit("invalid_range")),
        F.when(temporal_invalid, F.lit("invalid_time_order")),
        F.when(scope_invalid, F.lit("participant_scope")),
        F.when(duplicate_invalid, F.lit("duplicate_key")),
    )
    source_columns = [column["name"] for column in spec["columns"]]
    issues = (frame.filter(F.col("_quality_invalid"))
        .withColumn("industry", F.lit(industry))
        .withColumn("dataset", F.lit(dataset))
        .withColumn("record_key", F.concat_ws("|", *[F.col(name).cast("string") for name in spec["primary_key"]]))
        .withColumn("reason_codes", reason)
        .withColumn("raw_payload_json", F.to_json(F.struct(*[F.col(name) for name in source_columns])))
        .withColumn("quarantined_at", F.current_timestamp())
        .select("participant_key", "industry", "dataset", "source_row_id", "record_key", "reason_codes", "raw_payload_json", "quarantined_at"))
    quality_frames.append(issues)
    clean = frame.filter(~F.col("_quality_invalid")).select(*source_columns)
    accepted[dataset] = clean
    bronze_count = frame.count()
    clean_count = clean.count()
    assert clean_count <= bronze_count, f"Silver count increased for {{dataset}}"
    clean.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(destinations[dataset])
    print(f"Silver {{dataset}}: {{clean_count}} accepted rows")

quality = reduce(lambda left, right: left.unionByName(right), quality_frames)
quality.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(quality_uri)
quality_count = quality.count()
assert quality_count > 0, "The deterministic lab data must exercise the quarantine path"
print(f"Quality issues: {{quality_count}} rows")

{silver_ddl}
'''


def _gold_program(industry: str, participant_key: str, bucket: str, namespace: str) -> str:
    silver = {
        name: layer_uri(bucket, namespace, "silver", participant_key, industry, name)
        for name in DATASET_SPECS[industry]
    }
    gold = {
        name: layer_uri(bucket, namespace, "gold", participant_key, industry, f"{industry}_{name}")
        for name in GOLD_SPECS[industry]
    }
    gold_ddl = _ddl_statements(industry, participant_key, bucket, namespace, "gold")
    common = f'''from pyspark.sql import functions as F

participant_key = {participant_key!r}
silver = {json.dumps(silver, sort_keys=True)}
gold = {json.dumps(gold, sort_keys=True)}
'''
    if industry == "banking":
        body = '''customers = spark.read.format("delta").load(silver["customers"])
accounts = spark.read.format("delta").load(silver["accounts"])
transactions = spark.read.format("delta").load(silver["transactions"])
branches = spark.read.format("delta").load(silver["branches"])
latest_event = transactions.agg(F.max("event_time").alias("max_event_time"))
transactions_30d = (transactions.crossJoin(latest_event)
    .filter(F.col("event_time") >= F.col("max_event_time") - F.expr("INTERVAL 30 DAYS"))
    .drop("max_event_time"))
account_metrics = (accounts.groupBy("participant_key", "customer_id")
    .agg(F.countDistinct("account_id").alias("account_count"), F.sum("balance").alias("current_balance")))
transaction_metrics = (transactions_30d.join(
    accounts.select("participant_key", "account_id", "customer_id", "branch_id"),
    ["participant_key", "account_id"],
)
    .groupBy("participant_key", "customer_id")
    .agg(F.count("transaction_id").alias("transaction_count_30d"),
         F.sum(F.when(F.col("transaction_type") == "debit", F.col("amount")).otherwise(0)).alias("debit_amount_30d"),
         F.sum(F.when(F.col("transaction_type").isin("credit", "refund"), F.col("amount")).otherwise(0)).alias("credit_amount_30d"),
         F.max("event_time").alias("last_transaction_at")))
customer_value = customers.select("participant_key", "customer_id").join(account_metrics, ["participant_key", "customer_id"], "left").join(transaction_metrics, ["participant_key", "customer_id"], "left")
customer_value.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(gold["customer_value"])
branch_daily = (transactions.join(
    accounts.select("participant_key", "account_id", "branch_id"),
    ["participant_key", "account_id"],
)
    .withColumn("business_date", F.to_date("event_time"))
    .groupBy("participant_key", "business_date", "branch_id")
    .agg(F.countDistinct("account_id").alias("active_accounts"), F.count("transaction_id").alias("transaction_count"), F.sum("amount").alias("transaction_amount"), F.avg("amount").alias("average_transaction_amount")))
branch_daily.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(gold["branch_daily"])
customer_value.show(20, truncate=False)
'''
    elif industry == "telecommunications":
        body = '''plans = spark.read.format("delta").load(silver["plans"])
sites = spark.read.format("delta").load(silver["network_sites"])
subscribers = spark.read.format("delta").load(silver["subscribers"])
usage = spark.read.format("delta").load(silver["usage_events"])
subscriber_monthly = (usage.join(
    subscribers.select("participant_key", "subscriber_id", "plan_id"),
    ["participant_key", "subscriber_id"],
)
    .withColumn("month", F.trunc("event_time", "month"))
    .groupBy("participant_key", "month", "subscriber_id", "plan_id")
    .agg(F.sum(F.when(F.col("usage_type") == "data", F.col("usage_value")).otherwise(0)).alias("data_mb"), F.sum(F.when(F.col("usage_type") == "voice", F.col("usage_value")).otherwise(0)).alias("voice_minutes"), F.sum(F.when(F.col("usage_type") == "sms", F.col("usage_value")).otherwise(0)).alias("sms_count"), F.sum("charge_amount").alias("usage_charge"))
    .withColumn("overage_flag", F.col("usage_charge") > 0))
subscriber_monthly.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(gold["subscriber_monthly"])
site_daily = (usage.join(
    sites.select("participant_key", "site_id", "capacity_mb_day"),
    ["participant_key", "site_id"],
)
    .withColumn("event_date", F.to_date("event_time"))
    .groupBy("participant_key", "event_date", "site_id", "capacity_mb_day")
    .agg(F.countDistinct("subscriber_id").alias("unique_subscribers"), F.sum(F.when(F.col("usage_type") == "data", F.col("usage_value")).otherwise(0)).alias("data_mb"), F.sum(F.when(F.col("usage_type") == "voice", F.col("usage_value")).otherwise(0)).alias("voice_minutes"))
    .withColumn("utilization_pct", F.round(F.col("data_mb") / F.col("capacity_mb_day") * 100, 2)).drop("capacity_mb_day"))
site_daily.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(gold["site_daily"])
subscriber_monthly.show(20, truncate=False)
'''
    elif industry == "retail":
        body = '''customers = spark.read.format("delta").load(silver["customers"])
products = spark.read.format("delta").load(silver["products"])
orders = spark.read.format("delta").load(silver["orders"])
items = spark.read.format("delta").load(silver["order_items"])
lines = (items
    .join(
        orders.select("participant_key", "order_id", "customer_id", "order_time", "order_status"),
        ["participant_key", "order_id"],
    )
    .join(products.select("participant_key", "product_id", "unit_cost"), ["participant_key", "product_id"])
    .withColumn("gross", F.col("quantity") * F.col("unit_price"))
    .withColumn("net", F.col("gross") - F.col("discount_amount")))
customer_value = (lines.groupBy("participant_key", "customer_id")
    .agg(F.countDistinct("order_id").alias("order_count"), F.sum("quantity").alias("units"), F.sum("gross").alias("gross_revenue"), F.sum("discount_amount").alias("discount_amount"), F.sum("net").alias("net_revenue"), F.max("order_time").alias("last_order_at"))
    .withColumn("average_order_value", F.round(F.col("net_revenue") / F.col("order_count"), 2))
    .select("participant_key", "customer_id", "order_count", "units", "gross_revenue", "discount_amount", "net_revenue", "average_order_value", "last_order_at"))
customer_value.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(gold["customer_value"])
product_daily = (lines.withColumn("order_date", F.to_date("order_time"))
    .groupBy("participant_key", "order_date", "product_id")
    .agg(F.sum("quantity").alias("units"), F.sum("net").alias("net_revenue"), F.sum(F.col("net") - F.col("quantity") * F.col("unit_cost")).alias("gross_margin"), F.sum(F.when(F.col("order_status") == "refunded", F.col("quantity")).otherwise(0)).alias("refunded_units")))
product_daily.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(gold["product_daily"])
customer_value.show(20, truncate=False)
'''
    else:
        body = '''patients = spark.read.format("delta").load(silver["patients"])
providers = spark.read.format("delta").load(silver["providers"])
appointments = spark.read.format("delta").load(silver["appointments"])
encounters = spark.read.format("delta").load(silver["encounters"])
appointment_metrics = (appointments.groupBy("participant_key", "patient_id")
    .agg(F.count("appointment_id").alias("appointment_count"), F.sum(F.when(F.col("status") == "no_show", 1).otherwise(0)).alias("no_show_count")))
encounter_metrics = (encounters.groupBy("participant_key", "patient_id")
    .agg(F.count("encounter_id").alias("encounter_count"), F.sum("cost_amount").alias("total_cost"), F.max("encounter_start").alias("last_encounter_at")))
patient_utilization = patients.select("participant_key", "patient_id").join(appointment_metrics, ["participant_key", "patient_id"], "left").join(encounter_metrics, ["participant_key", "patient_id"], "left")
patient_utilization.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(gold["patient_utilization"])
scheduled = (appointments.withColumn("service_date", F.to_date("scheduled_start")).groupBy("participant_key", "service_date", "provider_id").agg(F.count("appointment_id").alias("scheduled_appointments"), F.sum(F.when(F.col("status") == "completed", 1).otherwise(0)).alias("completed_appointments"), F.avg(F.when(F.col("status") == "no_show", 1).otherwise(0)).alias("no_show_rate")))
performed = (encounters.withColumn("service_date", F.to_date("encounter_start")).withColumn("duration_minutes", (F.col("encounter_end").cast("long") - F.col("encounter_start").cast("long")) / 60).groupBy("participant_key", "service_date", "provider_id").agg(F.count("encounter_id").alias("encounter_count"), F.avg("duration_minutes").alias("average_duration_minutes"), F.sum("cost_amount").alias("total_cost")))
provider_daily = scheduled.join(performed, ["participant_key", "service_date", "provider_id"], "left")
provider_daily.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(gold["provider_daily"])
patient_utilization.show(20, truncate=False)
'''
    verification = '''for table_name, location in gold.items():
    row_count = spark.read.format("delta").load(location).count()
    assert row_count > 0, f"Gold table {table_name} is empty"
    print(f"Gold {table_name}: {row_count} rows")
'''
    return common + body + "\n" + verification + "\n" + gold_ddl + "\n"


def user_notebooks(
    industry: str,
    participant_key: str,
    email: str,
    bucket: str,
    namespace: str,
) -> dict[str, dict[str, Any]]:
    _validate_inputs(industry, participant_key, bucket, namespace)
    participant_folder(email)
    programs = {
        "landing": _landing_program(industry, participant_key, email, bucket, namespace),
        "bronze": _bronze_program(industry, participant_key, bucket, namespace),
        "silver": _silver_program(industry, participant_key, bucket, namespace),
        "gold": _gold_program(industry, participant_key, bucket, namespace),
    }
    narratives = {
        "landing": "Loads the four workspace CSV files into the participant's Object Storage Landing prefixes.",
        "bronze": "Preserves source values and lineage while converting each dataset to Delta.",
        "silver": "Casts, normalizes, deduplicates, validates relationships, and quarantines invalid rows.",
        "gold": "Builds the industry KPIs from accepted Silver records.",
    }
    expected = {
        "landing": "Four CSV prefixes contain the same row totals as the source files.",
        "bronze": "Four Landing CSV tables and four Bronze Delta tables are registered.",
        "silver": "Four clean Delta tables and a non-empty `quality_issues` table are registered.",
        "gold": "Two non-empty, industry-specific aggregate Delta tables are registered.",
    }
    return {
        f"{index:02d}_{layer}_{industry}.ipynb": _notebook(
            industry,
            f"{index:02d} {layer.title()} - {industry.title()}",
            narratives[layer],
            programs[layer],
            expected[layer],
        )
        for index, layer in enumerate(("landing", "bronze", "silver", "gold"), start=1)
    }
