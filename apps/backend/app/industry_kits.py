"""Deterministic, synthetic industry datasets for the AIDP lab."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import UTC, datetime, timedelta
from typing import Any


INDUSTRIES = ("banking", "telecommunications", "retail", "healthcare")


def _columns(*items: tuple[str, str, bool]) -> tuple[dict[str, Any], ...]:
    return tuple({"name": name, "type": kind, "required": required} for name, kind, required in items)


DATASET_SPECS: dict[str, dict[str, dict[str, Any]]] = {
    "banking": {
        "branches": {
            "filename": "branches.csv",
            "rows": 20,
            "primary_key": ("branch_id",),
            "foreign_keys": (),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("branch_id", "STRING", True), ("branch_type", "STRING", True),
                ("region", "STRING", True), ("opened_date", "DATE", True),
                ("status", "STRING", True), ("updated_at", "TIMESTAMP", True),
            ),
        },
        "customers": {
            "filename": "customers.csv",
            "rows": 200,
            "primary_key": ("customer_id",),
            "foreign_keys": (),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("customer_id", "STRING", True), ("customer_type", "STRING", True),
                ("segment", "STRING", True), ("region", "STRING", True),
                ("risk_band", "STRING", True), ("onboarding_date", "DATE", True),
                ("status", "STRING", True), ("updated_at", "TIMESTAMP", True),
            ),
        },
        "accounts": {
            "filename": "accounts.csv",
            "rows": 320,
            "primary_key": ("account_id",),
            "foreign_keys": (("customer_id", "customers", "customer_id"), ("branch_id", "branches", "branch_id")),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("account_id", "STRING", True), ("customer_id", "STRING", True),
                ("branch_id", "STRING", True), ("account_type", "STRING", True),
                ("currency", "STRING", True), ("opened_at", "TIMESTAMP", True),
                ("status", "STRING", True), ("balance", "DOUBLE", True),
                ("credit_limit", "DOUBLE", True), ("updated_at", "TIMESTAMP", True),
            ),
        },
        "transactions": {
            "filename": "transactions.csv",
            "rows": 4000,
            "primary_key": ("transaction_id",),
            "foreign_keys": (("account_id", "accounts", "account_id"),),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("transaction_id", "STRING", True), ("account_id", "STRING", True),
                ("event_time", "TIMESTAMP", True), ("transaction_type", "STRING", True),
                ("channel", "STRING", True), ("merchant_category", "STRING", False),
                ("currency", "STRING", True), ("amount", "DOUBLE", True),
                ("status", "STRING", True), ("updated_at", "TIMESTAMP", True),
            ),
        },
    },
    "telecommunications": {
        "plans": {
            "filename": "plans.csv", "rows": 12, "primary_key": ("plan_id",), "foreign_keys": (),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("plan_id", "STRING", True), ("plan_type", "STRING", True),
                ("monthly_fee", "DOUBLE", True), ("included_data_mb", "BIGINT", True),
                ("included_voice_minutes", "BIGINT", True), ("overage_rate", "DOUBLE", True),
                ("status", "STRING", True), ("updated_at", "TIMESTAMP", True),
            ),
        },
        "network_sites": {
            "filename": "network_sites.csv", "rows": 30, "primary_key": ("site_id",), "foreign_keys": (),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("site_id", "STRING", True), ("region", "STRING", True),
                ("technology", "STRING", True), ("capacity_mb_day", "BIGINT", True),
                ("commissioned_date", "DATE", True), ("status", "STRING", True),
                ("updated_at", "TIMESTAMP", True),
            ),
        },
        "subscribers": {
            "filename": "subscribers.csv", "rows": 250, "primary_key": ("subscriber_id",),
            "foreign_keys": (("plan_id", "plans", "plan_id"), ("home_site_id", "network_sites", "site_id")),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("subscriber_id", "STRING", True), ("plan_id", "STRING", True),
                ("home_site_id", "STRING", True), ("segment", "STRING", True),
                ("region", "STRING", True), ("activation_date", "DATE", True),
                ("status", "STRING", True), ("updated_at", "TIMESTAMP", True),
            ),
        },
        "usage_events": {
            "filename": "usage_events.csv", "rows": 6000, "primary_key": ("event_id",),
            "foreign_keys": (("subscriber_id", "subscribers", "subscriber_id"), ("site_id", "network_sites", "site_id")),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("event_id", "STRING", True), ("subscriber_id", "STRING", True),
                ("site_id", "STRING", True), ("event_time", "TIMESTAMP", True),
                ("usage_type", "STRING", True), ("usage_value", "DOUBLE", True),
                ("usage_unit", "STRING", True), ("charge_amount", "DOUBLE", True),
                ("updated_at", "TIMESTAMP", True),
            ),
        },
    },
    "retail": {
        "customers": {
            "filename": "customers.csv", "rows": 300, "primary_key": ("customer_id",), "foreign_keys": (),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("customer_id", "STRING", True), ("segment", "STRING", True),
                ("region", "STRING", True), ("loyalty_tier", "STRING", True),
                ("signup_date", "DATE", True), ("status", "STRING", True),
                ("updated_at", "TIMESTAMP", True),
            ),
        },
        "products": {
            "filename": "products.csv", "rows": 150, "primary_key": ("product_id",), "foreign_keys": (),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("product_id", "STRING", True), ("category", "STRING", True),
                ("brand_label", "STRING", True), ("unit_cost", "DOUBLE", True),
                ("list_price", "DOUBLE", True), ("status", "STRING", True),
                ("updated_at", "TIMESTAMP", True),
            ),
        },
        "orders": {
            "filename": "orders.csv", "rows": 1200, "primary_key": ("order_id",),
            "foreign_keys": (("customer_id", "customers", "customer_id"),),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("order_id", "STRING", True), ("customer_id", "STRING", True),
                ("order_time", "TIMESTAMP", True), ("channel", "STRING", True),
                ("region", "STRING", True), ("currency", "STRING", True),
                ("order_status", "STRING", True), ("discount_amount", "DOUBLE", True),
                ("declared_total", "DOUBLE", True), ("updated_at", "TIMESTAMP", True),
            ),
        },
        "order_items": {
            "filename": "order_items.csv", "rows": 3000, "primary_key": ("order_id", "line_number"),
            "foreign_keys": (("order_id", "orders", "order_id"), ("product_id", "products", "product_id")),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("order_id", "STRING", True), ("line_number", "BIGINT", True),
                ("product_id", "STRING", True), ("quantity", "BIGINT", True),
                ("unit_price", "DOUBLE", True), ("discount_amount", "DOUBLE", True),
                ("updated_at", "TIMESTAMP", True),
            ),
        },
    },
    "healthcare": {
        "patients": {
            "filename": "patients.csv", "rows": 240, "primary_key": ("patient_id",), "foreign_keys": (),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("patient_id", "STRING", True), ("age_band", "STRING", True),
                ("region", "STRING", True), ("coverage_type", "STRING", True),
                ("risk_band", "STRING", True), ("status", "STRING", True),
                ("updated_at", "TIMESTAMP", True),
            ),
        },
        "providers": {
            "filename": "providers.csv", "rows": 48, "primary_key": ("provider_id",), "foreign_keys": (),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("provider_id", "STRING", True), ("specialty", "STRING", True),
                ("region", "STRING", True), ("facility_type", "STRING", True),
                ("daily_capacity", "BIGINT", True), ("status", "STRING", True),
                ("updated_at", "TIMESTAMP", True),
            ),
        },
        "appointments": {
            "filename": "appointments.csv", "rows": 900, "primary_key": ("appointment_id",),
            "foreign_keys": (("patient_id", "patients", "patient_id"), ("provider_id", "providers", "provider_id")),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("appointment_id", "STRING", True), ("patient_id", "STRING", True),
                ("provider_id", "STRING", True), ("booked_at", "TIMESTAMP", True),
                ("scheduled_start", "TIMESTAMP", True), ("scheduled_end", "TIMESTAMP", True),
                ("appointment_type", "STRING", True), ("status", "STRING", True),
                ("updated_at", "TIMESTAMP", True),
            ),
        },
        "encounters": {
            "filename": "encounters.csv", "rows": 700, "primary_key": ("encounter_id",),
            "foreign_keys": (("appointment_id", "appointments", "appointment_id"), ("patient_id", "patients", "patient_id"), ("provider_id", "providers", "provider_id")),
            "columns": _columns(
                ("participant_key", "STRING", True), ("source_row_id", "STRING", True),
                ("encounter_id", "STRING", True), ("appointment_id", "STRING", False),
                ("patient_id", "STRING", True), ("provider_id", "STRING", True),
                ("encounter_start", "TIMESTAMP", True), ("encounter_end", "TIMESTAMP", True),
                ("diagnosis_group", "STRING", True), ("procedure_group", "STRING", True),
                ("cost_amount", "DOUBLE", True), ("disposition", "STRING", True),
                ("updated_at", "TIMESTAMP", True),
            ),
        },
    },
}


GOLD_SPECS: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {
    "banking": {
        "customer_value": (("participant_key", "STRING"), ("customer_id", "STRING"), ("account_count", "BIGINT"), ("current_balance", "DOUBLE"), ("transaction_count_30d", "BIGINT"), ("debit_amount_30d", "DOUBLE"), ("credit_amount_30d", "DOUBLE"), ("last_transaction_at", "TIMESTAMP")),
        "branch_daily": (("participant_key", "STRING"), ("business_date", "DATE"), ("branch_id", "STRING"), ("active_accounts", "BIGINT"), ("transaction_count", "BIGINT"), ("transaction_amount", "DOUBLE"), ("average_transaction_amount", "DOUBLE")),
    },
    "telecommunications": {
        "subscriber_monthly": (("participant_key", "STRING"), ("month", "DATE"), ("subscriber_id", "STRING"), ("plan_id", "STRING"), ("data_mb", "DOUBLE"), ("voice_minutes", "DOUBLE"), ("sms_count", "DOUBLE"), ("usage_charge", "DOUBLE"), ("overage_flag", "BOOLEAN")),
        "site_daily": (("participant_key", "STRING"), ("event_date", "DATE"), ("site_id", "STRING"), ("unique_subscribers", "BIGINT"), ("data_mb", "DOUBLE"), ("voice_minutes", "DOUBLE"), ("utilization_pct", "DOUBLE")),
    },
    "retail": {
        "customer_value": (("participant_key", "STRING"), ("customer_id", "STRING"), ("order_count", "BIGINT"), ("units", "BIGINT"), ("gross_revenue", "DOUBLE"), ("discount_amount", "DOUBLE"), ("net_revenue", "DOUBLE"), ("average_order_value", "DOUBLE"), ("last_order_at", "TIMESTAMP")),
        "product_daily": (("participant_key", "STRING"), ("order_date", "DATE"), ("product_id", "STRING"), ("units", "BIGINT"), ("net_revenue", "DOUBLE"), ("gross_margin", "DOUBLE"), ("refunded_units", "BIGINT")),
    },
    "healthcare": {
        "patient_utilization": (("participant_key", "STRING"), ("patient_id", "STRING"), ("appointment_count", "BIGINT"), ("no_show_count", "BIGINT"), ("encounter_count", "BIGINT"), ("total_cost", "DOUBLE"), ("last_encounter_at", "TIMESTAMP")),
        "provider_daily": (("participant_key", "STRING"), ("service_date", "DATE"), ("provider_id", "STRING"), ("scheduled_appointments", "BIGINT"), ("completed_appointments", "BIGINT"), ("no_show_rate", "DOUBLE"), ("encounter_count", "BIGINT"), ("average_duration_minutes", "DOUBLE"), ("total_cost", "DOUBLE")),
    },
}


_START = datetime(2026, 1, 1, tzinfo=UTC)


def _pick(key: str, dataset: str, index: int, field: str, values: tuple[Any, ...]) -> Any:
    digest = hashlib.sha256(f"dataset-v1|{key}|{dataset}|{index}|{field}".encode()).digest()
    return values[int.from_bytes(digest[:8], "big") % len(values)]


def _number(key: str, dataset: str, index: int, field: str, low: int, high: int) -> int:
    return int(_pick(key, dataset, index, field, tuple(range(low, high + 1))))


def _timestamp(index: int, step_minutes: int = 17) -> str:
    return (_START + timedelta(minutes=index * step_minutes)).isoformat().replace("+00:00", "Z")


def _date(index: int) -> str:
    return (_START + timedelta(days=index % 31)).date().isoformat()


def _common(key: str, dataset: str, index: int) -> dict[str, str]:
    return {"participant_key": key, "source_row_id": f"{dataset}-{index + 1:06d}"}


def _render(industry: str, dataset: str, rows: list[dict[str, Any]]) -> str:
    fields = [column["name"] for column in DATASET_SPECS[industry][dataset]["columns"]]
    target = io.StringIO(newline="")
    writer = csv.DictWriter(target, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return target.getvalue()


def _banking(key: str) -> dict[str, list[dict[str, Any]]]:
    regions = ("north", "south", "east", "west")
    branches = []
    for i in range(20):
        row = {**_common(key, "branches", i), "branch_id": f"BR-{i + 1:03d}", "branch_type": _pick(key, "branches", i, "type", ("urban", "suburban", "digital")), "region": _pick(key, "branches", i, "region", regions), "opened_date": _date(i), "status": "active", "updated_at": _timestamp(i)}
        branches.append(row)
    branches[-1]["branch_id"] = branches[-2]["branch_id"]
    branches[3]["region"] = " NORTH "

    customers = []
    for i in range(200):
        row = {**_common(key, "customers", i), "customer_id": f"CU-{i + 1:05d}", "customer_type": _pick(key, "customers", i, "type", ("individual", "small_business")), "segment": _pick(key, "customers", i, "segment", ("mass", "affluent", "business")), "region": _pick(key, "customers", i, "region", regions), "risk_band": _pick(key, "customers", i, "risk", ("low", "medium", "high")), "onboarding_date": _date(i), "status": "active", "updated_at": _timestamp(i)}
        customers.append(row)
    customers[-1]["customer_id"] = customers[-2]["customer_id"]
    customers[7]["segment"] = " AFFLUENT "

    accounts = []
    for i in range(320):
        balance = _number(key, "accounts", i, "balance", 100, 25000)
        row = {**_common(key, "accounts", i), "account_id": f"AC-{i + 1:05d}", "customer_id": f"CU-{i % 200 + 1:05d}", "branch_id": f"BR-{i % 20 + 1:03d}", "account_type": _pick(key, "accounts", i, "type", ("checking", "savings", "credit")), "currency": "USD", "opened_at": _timestamp(i, 53), "status": "active", "balance": f"{balance:.2f}", "credit_limit": f"{_number(key, 'accounts', i, 'limit', 500, 15000):.2f}", "updated_at": _timestamp(i)}
        accounts.append(row)
    accounts[-1]["account_id"] = accounts[-2]["account_id"]
    accounts[-2]["customer_id"] = "CU-ORPHAN"
    accounts[-3]["status"] = "unknown_state"

    transactions = []
    for i in range(4000):
        row = {**_common(key, "transactions", i), "transaction_id": f"TX-{i + 1:07d}", "account_id": f"AC-{i % 320 + 1:05d}", "event_time": _timestamp(i, 11), "transaction_type": _pick(key, "transactions", i, "type", ("debit", "credit", "refund")), "channel": _pick(key, "transactions", i, "channel", ("card", "mobile", "atm", "branch")), "merchant_category": _pick(key, "transactions", i, "merchant", ("grocery", "travel", "utilities", "services")), "currency": "USD", "amount": f"{_number(key, 'transactions', i, 'amount', 1, 1200) / 4:.2f}", "status": "posted", "updated_at": _timestamp(i, 11)}
        transactions.append(row)
    transactions[-1]["transaction_id"] = transactions[-2]["transaction_id"]
    transactions[-3]["account_id"] = "AC-ORPHAN"
    transactions[-4]["amount"] = "-10.00"
    transactions[-5]["event_time"] = "not-a-timestamp"
    return {"branches": branches, "customers": customers, "accounts": accounts, "transactions": transactions}


def _telecommunications(key: str) -> dict[str, list[dict[str, Any]]]:
    regions = ("north", "south", "central", "coastal")
    plans = []
    for i in range(12):
        plans.append({**_common(key, "plans", i), "plan_id": f"PL-{i + 1:03d}", "plan_type": _pick(key, "plans", i, "type", ("prepaid", "postpaid", "business")), "monthly_fee": f"{20 + i * 5:.2f}", "included_data_mb": str(5000 + i * 2500), "included_voice_minutes": str(200 + i * 50), "overage_rate": "0.02", "status": "active", "updated_at": _timestamp(i)})
    plans[-1]["plan_type"] = " POSTPAID "

    sites = []
    for i in range(30):
        sites.append({**_common(key, "network_sites", i), "site_id": f"ST-{i + 1:04d}", "region": _pick(key, "network_sites", i, "region", regions), "technology": _pick(key, "network_sites", i, "technology", ("4g", "5g")), "capacity_mb_day": str(100000 + i * 5000), "commissioned_date": _date(i), "status": "active", "updated_at": _timestamp(i)})
    sites[-1]["site_id"] = sites[-2]["site_id"]
    sites[-3]["capacity_mb_day"] = "-1"

    subscribers = []
    for i in range(250):
        subscribers.append({**_common(key, "subscribers", i), "subscriber_id": f"SU-{i + 1:05d}", "plan_id": f"PL-{i % 12 + 1:03d}", "home_site_id": f"ST-{i % 30 + 1:04d}", "segment": _pick(key, "subscribers", i, "segment", ("consumer", "family", "business")), "region": _pick(key, "subscribers", i, "region", regions), "activation_date": _date(i), "status": "active", "updated_at": _timestamp(i)})
    subscribers[-1]["subscriber_id"] = subscribers[-2]["subscriber_id"]
    subscribers[-3]["plan_id"] = "PL-ORPHAN"
    subscribers[9]["region"] = " CENTRAL "

    usage = []
    units = {"data": "mb", "voice": "minutes", "sms": "messages"}
    for i in range(6000):
        kind = _pick(key, "usage_events", i, "type", tuple(units))
        usage.append({**_common(key, "usage_events", i), "event_id": f"EV-{i + 1:07d}", "subscriber_id": f"SU-{i % 250 + 1:05d}", "site_id": f"ST-{i % 30 + 1:04d}", "event_time": _timestamp(i, 7), "usage_type": kind, "usage_value": f"{_number(key, 'usage_events', i, 'value', 1, 5000) / 10:.1f}", "usage_unit": units[kind], "charge_amount": f"{_number(key, 'usage_events', i, 'charge', 0, 500) / 100:.2f}", "updated_at": _timestamp(i, 7)})
    usage[-1]["event_id"] = usage[-2]["event_id"]
    usage[-3]["subscriber_id"] = "SU-ORPHAN"
    usage[-4]["usage_value"] = "-5"
    usage[-5]["usage_unit"] = "gallons"
    usage[-6]["event_time"] = "not-a-timestamp"
    return {"plans": plans, "network_sites": sites, "subscribers": subscribers, "usage_events": usage}


def _retail(key: str) -> dict[str, list[dict[str, Any]]]:
    regions = ("north", "south", "east", "west")
    customers = []
    for i in range(300):
        customers.append({**_common(key, "customers", i), "customer_id": f"RC-{i + 1:05d}", "segment": _pick(key, "customers", i, "segment", ("consumer", "small_business")), "region": _pick(key, "customers", i, "region", regions), "loyalty_tier": _pick(key, "customers", i, "tier", ("standard", "silver", "gold")), "signup_date": _date(i), "status": "active", "updated_at": _timestamp(i)})
    customers[-1]["customer_id"] = customers[-2]["customer_id"]
    customers[11]["loyalty_tier"] = " GOLD "

    products = []
    for i in range(150):
        cost = _number(key, "products", i, "cost", 4, 200)
        products.append({**_common(key, "products", i), "product_id": f"PR-{i + 1:05d}", "category": _pick(key, "products", i, "category", ("home", "electronics", "apparel", "grocery")), "brand_label": f"Brand-{i % 15 + 1:02d}", "unit_cost": f"{cost:.2f}", "list_price": f"{cost * 1.4:.2f}", "status": "active", "updated_at": _timestamp(i)})
    products[-1]["product_id"] = products[-2]["product_id"]
    products[-3]["unit_cost"] = "-2.00"

    orders = []
    for i in range(1200):
        orders.append({**_common(key, "orders", i), "order_id": f"OR-{i + 1:06d}", "customer_id": f"RC-{i % 300 + 1:05d}", "order_time": _timestamp(i, 29), "channel": _pick(key, "orders", i, "channel", ("web", "mobile", "store")), "region": _pick(key, "orders", i, "region", regions), "currency": "USD", "order_status": _pick(key, "orders", i, "status", ("completed", "completed", "refunded")), "discount_amount": f"{_number(key, 'orders', i, 'discount', 0, 20):.2f}", "declared_total": f"{_number(key, 'orders', i, 'total', 20, 500):.2f}", "updated_at": _timestamp(i, 29)})
    orders[-1]["order_id"] = orders[-2]["order_id"]
    orders[-3]["customer_id"] = "RC-ORPHAN"
    orders[-4]["order_time"] = "not-a-timestamp"
    orders[-5]["order_status"] = "unknown_state"

    items = []
    for i in range(3000):
        items.append({**_common(key, "order_items", i), "order_id": f"OR-{i % 1200 + 1:06d}", "line_number": str(i // 1200 + 1), "product_id": f"PR-{i % 150 + 1:05d}", "quantity": str(_number(key, "order_items", i, "quantity", 1, 4)), "unit_price": f"{_number(key, 'order_items', i, 'price', 5, 300):.2f}", "discount_amount": f"{_number(key, 'order_items', i, 'discount', 0, 10):.2f}", "updated_at": _timestamp(i, 13)})
    items[-1]["order_id"] = items[-2]["order_id"]
    items[-1]["line_number"] = items[-2]["line_number"]
    items[-3]["product_id"] = "PR-ORPHAN"
    items[-4]["quantity"] = "0"
    return {"customers": customers, "products": products, "orders": orders, "order_items": items}


def _healthcare(key: str) -> dict[str, list[dict[str, Any]]]:
    regions = ("north", "south", "east", "west")
    patients = []
    for i in range(240):
        patients.append({**_common(key, "patients", i), "patient_id": f"PT-{i + 1:05d}", "age_band": _pick(key, "patients", i, "age", ("0-17", "18-39", "40-64", "65+")), "region": _pick(key, "patients", i, "region", regions), "coverage_type": _pick(key, "patients", i, "coverage", ("public", "private", "self_pay")), "risk_band": _pick(key, "patients", i, "risk", ("low", "medium", "high")), "status": "active", "updated_at": _timestamp(i)})
    patients[-1]["patient_id"] = patients[-2]["patient_id"]
    patients[-3]["age_band"] = "unknown"
    patients[5]["region"] = " EAST "

    providers = []
    for i in range(48):
        providers.append({**_common(key, "providers", i), "provider_id": f"PV-{i + 1:04d}", "specialty": _pick(key, "providers", i, "specialty", ("primary_care", "cardiology", "orthopedics", "pediatrics")), "region": _pick(key, "providers", i, "region", regions), "facility_type": _pick(key, "providers", i, "facility", ("clinic", "hospital", "virtual")), "daily_capacity": str(8 + i % 13), "status": "active", "updated_at": _timestamp(i)})
    providers[-1]["provider_id"] = providers[-2]["provider_id"]
    providers[-3]["daily_capacity"] = "0"

    appointments = []
    for i in range(900):
        start = _START + timedelta(minutes=i * 43)
        appointments.append({**_common(key, "appointments", i), "appointment_id": f"AP-{i + 1:06d}", "patient_id": f"PT-{i % 240 + 1:05d}", "provider_id": f"PV-{i % 48 + 1:04d}", "booked_at": (start - timedelta(days=5)).isoformat().replace("+00:00", "Z"), "scheduled_start": start.isoformat().replace("+00:00", "Z"), "scheduled_end": (start + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"), "appointment_type": _pick(key, "appointments", i, "type", ("consultation", "follow_up", "preventive")), "status": _pick(key, "appointments", i, "status", ("completed", "completed", "no_show", "scheduled")), "updated_at": _timestamp(i, 43)})
    appointments[-1]["appointment_id"] = appointments[-2]["appointment_id"]
    appointments[-3]["patient_id"] = "PT-ORPHAN"
    appointments[-4]["scheduled_end"] = appointments[-4]["scheduled_start"]

    encounters = []
    for i in range(700):
        start = _START + timedelta(minutes=i * 47)
        urgent = i % 10 == 0
        encounters.append({**_common(key, "encounters", i), "encounter_id": f"EN-{i + 1:06d}", "appointment_id": "" if urgent else f"AP-{i % 900 + 1:06d}", "patient_id": f"PT-{i % 240 + 1:05d}", "provider_id": f"PV-{i % 48 + 1:04d}", "encounter_start": start.isoformat().replace("+00:00", "Z"), "encounter_end": (start + timedelta(minutes=40)).isoformat().replace("+00:00", "Z"), "diagnosis_group": _pick(key, "encounters", i, "diagnosis", ("respiratory", "cardiovascular", "musculoskeletal", "preventive")), "procedure_group": _pick(key, "encounters", i, "procedure", ("evaluation", "imaging", "therapy", "screening")), "cost_amount": f"{_number(key, 'encounters', i, 'cost', 50, 1500):.2f}", "disposition": "outpatient", "updated_at": _timestamp(i, 47)})
    encounters[-1]["encounter_id"] = encounters[-2]["encounter_id"]
    encounters[-3]["provider_id"] = "PV-ORPHAN"
    encounters[-4]["cost_amount"] = "-50.00"
    encounters[-5]["encounter_start"] = "not-a-timestamp"
    return {"patients": patients, "providers": providers, "appointments": appointments, "encounters": encounters}


_GENERATORS = {
    "banking": _banking,
    "telecommunications": _telecommunications,
    "retail": _retail,
    "healthcare": _healthcare,
}


def csv_samples(industry: str, participant_key: str) -> dict[str, str]:
    """Return exactly four deterministic, non-PII CSV files for a participant."""
    if industry not in INDUSTRIES:
        raise ValueError("Choose banking, telecommunications, retail, or healthcare")
    if re.fullmatch(r"u_[0-9a-f]{16}", participant_key) is None:
        raise ValueError("participant_key must match u_<16 lowercase hex characters>")
    datasets = _GENERATORS[industry](participant_key)
    return {
        DATASET_SPECS[industry][name]["filename"]: _render(industry, name, rows)
        for name, rows in datasets.items()
    }
