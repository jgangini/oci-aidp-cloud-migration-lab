import csv
import io
from collections import Counter
from datetime import UTC, date, datetime

import pytest

from app.industry_kits import DATASET_SPECS, GOLD_SPECS, INDUSTRIES, csv_samples
from app.notebooks import _ENUM_VALUES, _STRICT_POSITIVE, _TEMPORAL_ORDER


PARTICIPANT_KEY = "u_0123456789abcdef"
OTHER_PARTICIPANT_KEY = "u_fedcba9876543210"
EXPECTED_FILES = {
    "banking": {
        "branches.csv": 20,
        "customers.csv": 200,
        "accounts.csv": 320,
        "transactions.csv": 4_000,
    },
    "telecommunications": {
        "plans.csv": 12,
        "network_sites.csv": 30,
        "subscribers.csv": 250,
        "usage_events.csv": 6_000,
    },
    "retail": {
        "customers.csv": 300,
        "products.csv": 150,
        "orders.csv": 1_200,
        "order_items.csv": 3_000,
    },
    "healthcare": {
        "patients.csv": 240,
        "providers.csv": 48,
        "appointments.csv": 900,
        "encounters.csv": 700,
    },
}
PII_COLUMNS = {
    "email",
    "first_name",
    "last_name",
    "full_name",
    "phone",
    "address",
    "birth_date",
    "ssn",
}
CASE_CASES = {
    "banking": ("branches.csv", "region", " NORTH "),
    "telecommunications": ("plans.csv", "plan_type", " POSTPAID "),
    "retail": ("customers.csv", "loyalty_tier", " GOLD "),
    "healthcare": ("patients.csv", "region", " EAST "),
}
ENUM_CASES = {
    "banking": ("accounts.csv", "status", "unknown_state"),
    "telecommunications": ("usage_events.csv", "usage_unit", "gallons"),
    "retail": ("orders.csv", "order_status", "unknown_state"),
    "healthcare": ("patients.csv", "age_band", "unknown"),
}
NUMERIC_CASES = {
    "banking": ("transactions.csv", "amount"),
    "telecommunications": ("usage_events.csv", "usage_value"),
    "retail": ("products.csv", "unit_cost"),
    "healthcare": ("encounters.csv", "cost_amount"),
}
TIMESTAMP_CASES = {
    "banking": ("transactions.csv", "event_time"),
    "telecommunications": ("usage_events.csv", "event_time"),
    "retail": ("orders.csv", "order_time"),
    "healthcare": ("encounters.csv", "encounter_start"),
}


def rows(content: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(content)))


def test_industry_contract_has_exact_files_counts_and_no_pii() -> None:
    assert set(INDUSTRIES) == set(EXPECTED_FILES)
    for industry, expected in EXPECTED_FILES.items():
        samples = csv_samples(industry, PARTICIPANT_KEY)
        assert {name: len(rows(content)) for name, content in samples.items()} == expected
        assert {
            spec["filename"]: spec["rows"]
            for spec in DATASET_SPECS[industry].values()
        } == expected
        for content in samples.values():
            parsed = rows(content)
            assert parsed
            assert set(parsed[0]).isdisjoint(PII_COLUMNS)
            assert {row["participant_key"] for row in parsed} == {PARTICIPANT_KEY}
            assert "@" not in content


def test_industry_data_is_deterministic_but_participant_specific() -> None:
    for industry in INDUSTRIES:
        first = csv_samples(industry, PARTICIPANT_KEY)
        assert first == csv_samples(industry, PARTICIPANT_KEY)
        second = csv_samples(industry, OTHER_PARTICIPANT_KEY)
        assert first != second
        assert any(
            {
                key: value
                for key, value in left.items()
                if key not in {"participant_key", "source_row_id"}
            }
            != {
                key: value
                for key, value in right.items()
                if key not in {"participant_key", "source_row_id"}
            }
            for filename in first
            for left, right in zip(rows(first[filename]), rows(second[filename]))
        )


@pytest.mark.parametrize("industry", INDUSTRIES)
def test_quality_anomalies_cover_every_rule_family_per_industry(industry: str) -> None:
    samples = {
        name: rows(content)
        for name, content in csv_samples(industry, PARTICIPANT_KEY).items()
    }
    specs = DATASET_SPECS[industry]

    duplicate_groups = []
    for dataset, spec in specs.items():
        parsed = samples[spec["filename"]]
        keys = [tuple(row[column] for column in spec["primary_key"]) for row in parsed]
        duplicates = {key for key, count in Counter(keys).items() if count > 1}
        for key in duplicates:
            candidates = [
                row for row in parsed
                if tuple(row[column] for column in spec["primary_key"]) == key
            ]
            assert len({row["updated_at"] for row in candidates}) == len(candidates)
            assert max(candidates, key=lambda row: row["updated_at"])["updated_at"] == max(
                row["updated_at"] for row in candidates
            )
        duplicate_groups.extend(duplicates)
    assert duplicate_groups

    orphan_relations = []
    for dataset, spec in specs.items():
        parsed = samples[spec["filename"]]
        for local_column, parent_name, parent_column in spec["foreign_keys"]:
            parent = samples[specs[parent_name]["filename"]]
            parent_values = {row[parent_column] for row in parent}
            if any(
                row[local_column] and row[local_column] not in parent_values
                for row in parsed
            ):
                orphan_relations.append((dataset, local_column))
    assert orphan_relations

    case_file, case_column, case_value = CASE_CASES[industry]
    assert any(row[case_column] == case_value for row in samples[case_file])
    enum_file, enum_column, enum_value = ENUM_CASES[industry]
    assert any(row[enum_column] == enum_value for row in samples[enum_file])
    numeric_file, numeric_column = NUMERIC_CASES[industry]
    assert any(float(row[numeric_column]) <= 0 for row in samples[numeric_file])
    timestamp_file, timestamp_column = TIMESTAMP_CASES[industry]
    assert any(
        _invalid_timestamp(row[timestamp_column])
        for row in samples[timestamp_file]
    )


def _invalid_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    return False


def _cast_row(industry: str, dataset: str, row: dict[str, str]) -> tuple[dict[str, object], bool]:
    typed: dict[str, object] = {}
    invalid_cast = False
    for column in DATASET_SPECS[industry][dataset]["columns"]:
        name, kind = column["name"], column["type"]
        raw = row[name].strip()
        if not raw:
            typed[name] = None
            continue
        try:
            if kind == "STRING":
                typed[name] = raw if name in {"participant_key", "source_row_id"} or name.endswith("_id") else raw.lower()
            elif kind == "DATE":
                typed[name] = date.fromisoformat(raw)
            elif kind == "TIMESTAMP":
                typed[name] = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            elif kind == "DOUBLE":
                typed[name] = float(raw)
            elif kind == "BIGINT":
                typed[name] = int(raw)
            elif kind == "BOOLEAN":
                typed[name] = {"true": True, "false": False}[raw.lower()]
            else:
                raise AssertionError(f"unsupported type {kind}")
        except (KeyError, ValueError):
            typed[name] = None
            invalid_cast = True
    return typed, invalid_cast


def _duplicate_indexes(
    candidates: list[tuple[dict[str, object], bool]], spec: dict
) -> set[int]:
    groups: dict[tuple[object, ...], list[int]] = {}
    for index, (typed, _) in enumerate(candidates):
        key = tuple(typed[name] for name in spec["primary_key"])
        groups.setdefault(key, []).append(index)
    duplicates: set[int] = set()
    for indexes in groups.values():
        ranked = sorted(
            indexes,
            key=lambda index: (
                candidates[index][0].get("updated_at")
                or datetime.min.replace(tzinfo=UTC),
                str(candidates[index][0].get("source_row_id") or ""),
            ),
            reverse=True,
        )
        duplicates.update(ranked[1:])
    return duplicates


def _enum_is_invalid(industry: str, dataset: str, typed: dict[str, object]) -> bool:
    rules = {
        qualified_name.split(".", 1)[1]: allowed
        for qualified_name, allowed in _ENUM_VALUES[industry].items()
        if qualified_name.startswith(f"{dataset}.")
    }
    return any(typed[name] is not None and typed[name] not in allowed for name, allowed in rules.items())


def _range_or_time_is_invalid(
    industry: str, dataset: str, typed: dict[str, object]
) -> bool:
    positive = [
        qualified_name.split(".", 1)[1]
        for qualified_name in _STRICT_POSITIVE.get(industry, ())
        if qualified_name.startswith(f"{dataset}.")
    ]
    invalid_range = any(typed[name] is not None and float(typed[name]) <= 0 for name in positive)
    invalid_time = any(
        owner == dataset
        and typed[start] is not None
        and typed[end] is not None
        and typed[end] <= typed[start]
        for owner, start, end in _TEMPORAL_ORDER.get(industry, ())
    )
    return invalid_range or invalid_time


def _foreign_key_is_invalid(
    typed: dict[str, object],
    spec: dict,
    accepted: dict[str, list[dict[str, object]]],
) -> bool:
    return any(
        typed[local] is not None
        and typed[local] not in {item[remote] for item in accepted[parent]}
        for local, parent, remote in spec["foreign_keys"]
    )


def _candidate_is_invalid(
    industry: str,
    dataset: str,
    typed: dict[str, object],
    invalid_cast: bool,
    duplicate: bool,
    spec: dict,
    accepted: dict[str, list[dict[str, object]]],
) -> bool:
    required = any(
        column["required"] and typed[column["name"]] is None
        for column in spec["columns"]
    )
    return any(
        (
            invalid_cast,
            duplicate,
            required,
            typed["participant_key"] != PARTICIPANT_KEY,
            _enum_is_invalid(industry, dataset, typed),
            _range_or_time_is_invalid(industry, dataset, typed),
            _foreign_key_is_invalid(typed, spec, accepted),
        )
    )


def _assert_silver_constraints(
    clean: list[dict[str, object]],
    spec: dict,
    accepted: dict[str, list[dict[str, object]]],
) -> None:
    keys = [tuple(row[name] for name in spec["primary_key"]) for row in clean]
    assert len(keys) == len(set(keys))
    for local, parent, remote in spec["foreign_keys"]:
        parent_values = {row[remote] for row in accepted[parent]}
        assert all(row[local] is None or row[local] in parent_values for row in clean)


def _reference_silver(industry: str) -> tuple[dict[str, list[dict[str, object]]], int, int]:
    parsed = {
        dataset: rows(csv_samples(industry, PARTICIPANT_KEY)[spec["filename"]])
        for dataset, spec in DATASET_SPECS[industry].items()
    }
    bronze_total = sum(len(dataset_rows) for dataset_rows in parsed.values())
    accepted: dict[str, list[dict[str, object]]] = {}
    issue_count = 0
    for dataset, spec in DATASET_SPECS[industry].items():
        candidates = []
        for raw in parsed[dataset]:
            typed, invalid_cast = _cast_row(industry, dataset, raw)
            candidates.append((typed, invalid_cast))
        duplicate_indexes = _duplicate_indexes(candidates, spec)
        clean: list[dict[str, object]] = []
        for index, (typed, invalid_cast) in enumerate(candidates):
            if _candidate_is_invalid(
                industry,
                dataset,
                typed,
                invalid_cast,
                index in duplicate_indexes,
                spec,
                accepted,
            ):
                issue_count += 1
            else:
                clean.append(typed)
        accepted[dataset] = clean
        _assert_silver_constraints(clean, spec, accepted)
    return accepted, issue_count, bronze_total


def _reference_gold_group_counts(
    industry: str, silver: dict[str, list[dict[str, object]]]
) -> dict[str, int]:
    if industry == "banking":
        accounts = {row["account_id"]: row for row in silver["accounts"]}
        joined = [
            (row, accounts[row["account_id"]])
            for row in silver["transactions"]
            if row["account_id"] in accounts
        ]
        return {
            "customer_value": len(silver["customers"]),
            "branch_daily": len({(row["event_time"].date(), account["branch_id"]) for row, account in joined}),
        }
    if industry == "telecommunications":
        subscribers = {row["subscriber_id"]: row for row in silver["subscribers"]}
        sites = {row["site_id"] for row in silver["network_sites"]}
        return {
            "subscriber_monthly": len({
                (row["event_time"].year, row["event_time"].month, row["subscriber_id"], subscribers[row["subscriber_id"]]["plan_id"])
                for row in silver["usage_events"]
                if row["subscriber_id"] in subscribers
            }),
            "site_daily": len({
                (row["event_time"].date(), row["site_id"])
                for row in silver["usage_events"]
                if row["site_id"] in sites
            }),
        }
    if industry == "retail":
        orders = {row["order_id"]: row for row in silver["orders"]}
        products = {row["product_id"] for row in silver["products"]}
        lines = [
            (row, orders[row["order_id"]])
            for row in silver["order_items"]
            if row["order_id"] in orders and row["product_id"] in products
        ]
        return {
            "customer_value": len({order["customer_id"] for _, order in lines}),
            "product_daily": len({(order["order_time"].date(), row["product_id"]) for row, order in lines}),
        }
    return {
        "patient_utilization": len(silver["patients"]),
        "provider_daily": len({
            (row["scheduled_start"].date(), row["provider_id"])
            for row in silver["appointments"]
        }),
    }


@pytest.mark.parametrize("industry", INDUSTRIES)
def test_reference_medallion_has_quarantine_and_nonempty_gold(industry: str) -> None:
    silver, issue_count, bronze_total = _reference_silver(industry)
    assert bronze_total == sum(spec["rows"] for spec in DATASET_SPECS[industry].values())
    assert 0 < sum(map(len, silver.values())) <= bronze_total
    assert issue_count > 0
    gold_counts = _reference_gold_group_counts(industry, silver)
    assert set(gold_counts) == set(GOLD_SPECS[industry])
    assert all(count > 0 for count in gold_counts.values())


def test_industry_and_participant_key_are_validated() -> None:
    with pytest.raises(ValueError, match="Choose banking"):
        csv_samples("energy", PARTICIPANT_KEY)
    with pytest.raises(ValueError, match="participant_key"):
        csv_samples("banking", "ada@example.com")
    with pytest.raises(ValueError, match="participant_key"):
        csv_samples("banking", "u_zzzzzzzzzzzzzzzz")
