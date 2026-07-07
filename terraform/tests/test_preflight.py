from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


MODULE_PATH = Path(__file__).parents[1] / "k_preflight.py"
SPEC = importlib.util.spec_from_file_location("aidp_deploy_preflight", MODULE_PATH)
assert SPEC and SPEC.loader
preflight = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


class Identity:
    def get_tenancy(self, _tenancy_id: str) -> Any:
        return SimpleNamespace(data=SimpleNamespace(home_region_key="IAD"))

    def list_region_subscriptions(self, _tenancy_id: str) -> Any:
        return SimpleNamespace(
            data=[
                SimpleNamespace(region_key="ORD", region_name="us-chicago-1", is_home_region=False),
                SimpleNamespace(region_key="IAD", region_name="us-ashburn-1", is_home_region=True),
            ]
        )

    def list_availability_domains(self, _tenancy_id: str) -> Any:
        return SimpleNamespace(data=[SimpleNamespace(name="AD-1")])

    def list_domains(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(data=[SimpleNamespace(url="https://identity.example")])


class IdentityDomains:
    def __init__(self, signing_cert_public_access: bool = True) -> None:
        self.signing_cert_public_access = signing_cert_public_access

    def list_settings(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            data=SimpleNamespace(
                resources=[SimpleNamespace(signing_cert_public_access=self.signing_cert_public_access)]
            )
        )


class Compute:
    def __init__(self, statuses: dict[str, tuple[str, str]]) -> None:
        self.statuses = statuses
        self.details: Any = None

    def create_compute_capacity_report(self, details: Any) -> Any:
        self.details = details
        return SimpleNamespace(
            data=SimpleNamespace(
                shape_availabilities=[
                    SimpleNamespace(instance_shape=shape, availability_status=status, available_count=count)
                    for shape, (status, count) in self.statuses.items()
                ]
            )
        )


def _select(statuses: dict[str, tuple[str, str]]) -> tuple[dict[str, Any], Compute]:
    compute = Compute(statuses)
    result = preflight.select_inputs(
        {
            "region": "us-chicago-1",
            "inputs": {},
        },
        {"tenancy": "ocid1.tenancy.oc1..test", "region": "us-chicago-1"},
        identity_factory=lambda _config: Identity(),
        compute_factory=lambda _config: compute,
        identity_domains_factory=lambda *_args, **_kwargs: IdentityDomains(),
    )
    return result, compute


def test_preflight_selects_e5_and_discovers_home_region() -> None:
    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    result, compute = _select({preflight.E5_SHAPE: (available, "1"), preflight.E4_SHAPE: (available, "2")})
    assert result["inputs"] == {
        "home_region": "us-ashburn-1",
        "preferred_vm_shape": preflight.E5_SHAPE,
        "availability_domain_index": 0,
    }
    assert compute.details.compartment_id == "ocid1.tenancy.oc1..test"
    assert compute.details.availability_domain == "AD-1"
    assert [item.instance_shape for item in compute.details.shape_availabilities] == list(preflight.SUPPORTED_SHAPES)
    assert compute.details.shape_availabilities[0].instance_shape_config.ocpus == 2
    assert compute.details.shape_availabilities[0].instance_shape_config.memory_in_gbs == 16
    assert any(event["name"] == "Identity Domain signing certificate access" for event in result["events"])


def test_preflight_requires_public_identity_domain_signing_certificate() -> None:
    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    with pytest.raises(RuntimeError, match="Access Signing Certificate"):
        preflight.select_inputs(
            {"region": "us-chicago-1", "inputs": {}},
            {"tenancy": "ocid1.tenancy.oc1..test", "region": "us-chicago-1"},
            identity_factory=lambda _config: Identity(),
            compute_factory=lambda _config: Compute({preflight.E5_SHAPE: (available, "1")}),
            identity_domains_factory=lambda *_args, **_kwargs: IdentityDomains(False),
        )


def test_preflight_finds_default_domain_by_stable_type() -> None:
    filters: dict[str, Any] = {}

    class RecordingIdentity(Identity):
        def list_domains(self, **kwargs: Any) -> Any:
            filters.update(kwargs)
            return super().list_domains(**kwargs)

    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    preflight.select_inputs(
        {"region": "us-chicago-1", "inputs": {}},
        {"tenancy": "ocid1.tenancy.oc1..test", "region": "us-chicago-1"},
        identity_factory=lambda _config: RecordingIdentity(),
        compute_factory=lambda _config: Compute({preflight.E5_SHAPE: (available, "1")}),
        identity_domains_factory=lambda *_args, **_kwargs: IdentityDomains(),
    )

    assert filters["type"] == "DEFAULT"
    assert "display_name" not in filters


def test_preflight_accepts_available_status_without_a_count() -> None:
    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    result, _ = _select({preflight.E5_SHAPE: (available, None)})
    assert result["inputs"]["preferred_vm_shape"] == preflight.E5_SHAPE


def test_preflight_selects_e4_when_e5_is_not_available() -> None:
    model = preflight.oci.core.models.CapacityReportShapeAvailability
    result, _ = _select(
        {
            preflight.E5_SHAPE: (model.AVAILABILITY_STATUS_OUT_OF_HOST_CAPACITY, "0"),
            preflight.E4_SHAPE: (model.AVAILABILITY_STATUS_AVAILABLE, "1"),
        }
    )
    assert result["inputs"]["preferred_vm_shape"] == preflight.E4_SHAPE


def test_preflight_falls_back_to_e4_without_user_shape_input() -> None:
    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    result, compute = _select({preflight.E4_SHAPE: (available, "1")})
    assert result["inputs"]["preferred_vm_shape"] == preflight.E4_SHAPE
    assert [item.instance_shape for item in compute.details.shape_availabilities] == list(preflight.SUPPORTED_SHAPES)


def test_preflight_falls_back_to_authorized_e3_when_e5_and_e4_are_unavailable() -> None:
    model = preflight.oci.core.models.CapacityReportShapeAvailability
    result, _ = _select(
        {
            preflight.E5_SHAPE: (model.AVAILABILITY_STATUS_OUT_OF_HOST_CAPACITY, "0"),
            preflight.E4_SHAPE: (model.AVAILABILITY_STATUS_OUT_OF_HOST_CAPACITY, "0"),
            preflight.E3_SHAPE: (model.AVAILABILITY_STATUS_AVAILABLE, "1"),
        }
    )
    assert result["inputs"]["preferred_vm_shape"] == preflight.E3_SHAPE


def test_preflight_checks_all_availability_domains_for_standard_shape() -> None:
    model = preflight.oci.core.models.CapacityReportShapeAvailability

    class MultiAdIdentity(Identity):
        def list_availability_domains(self, _tenancy_id: str) -> Any:
            return SimpleNamespace(data=[SimpleNamespace(name="AD-1"), SimpleNamespace(name="AD-2")])

    class MultiAdCompute:
        def create_compute_capacity_report(self, details: Any) -> Any:
            available = details.availability_domain == "AD-2"
            return SimpleNamespace(
                data=SimpleNamespace(
                    shape_availabilities=[
                        SimpleNamespace(
                            instance_shape=preflight.E5_SHAPE,
                            availability_status=(model.AVAILABILITY_STATUS_AVAILABLE if available else model.AVAILABILITY_STATUS_OUT_OF_HOST_CAPACITY),
                            available_count=1 if available else 0,
                        )
                    ]
                )
            )

    result = preflight.select_inputs(
        {"region": "us-chicago-1", "inputs": {}},
        {"tenancy": "ocid1.tenancy.oc1..test", "region": "us-chicago-1"},
        identity_factory=lambda _config: MultiAdIdentity(),
        compute_factory=lambda _config: MultiAdCompute(),
        identity_domains_factory=lambda *_args, **_kwargs: IdentityDomains(),
    )
    assert result["inputs"]["availability_domain_index"] == 1


def test_preflight_rejects_zero_or_missing_capacity() -> None:
    available = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
    unsupported = preflight.oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_HARDWARE_NOT_SUPPORTED
    with pytest.raises(RuntimeError, match="no capacity"):
        _select({preflight.E5_SHAPE: (available, "0"), preflight.E4_SHAPE: (unsupported, "0")})


def test_preflight_accepts_any_available_fault_domain() -> None:
    model = preflight.oci.core.models.CapacityReportShapeAvailability

    class FaultDomainCompute:
        @staticmethod
        def create_compute_capacity_report(_details: Any) -> Any:
            return SimpleNamespace(
                data=SimpleNamespace(
                    shape_availabilities=[
                        SimpleNamespace(
                            instance_shape=preflight.E5_SHAPE,
                            availability_status=model.AVAILABILITY_STATUS_OUT_OF_HOST_CAPACITY,
                            available_count=0,
                        ),
                        SimpleNamespace(
                            instance_shape=preflight.E5_SHAPE,
                            availability_status=model.AVAILABILITY_STATUS_AVAILABLE,
                            available_count=1,
                        ),
                    ]
                )
            )

    result = preflight.select_inputs(
        {"region": "us-chicago-1", "inputs": {}},
        {"tenancy": "ocid1.tenancy.oc1..test", "region": "us-chicago-1"},
        identity_factory=lambda _config: Identity(),
        compute_factory=lambda _config: FaultDomainCompute(),
        identity_domains_factory=lambda *_args, **_kwargs: IdentityDomains(),
    )
    assert result["inputs"]["preferred_vm_shape"] == preflight.E5_SHAPE


def test_preflight_uses_runner_private_key_path(monkeypatch) -> None:
    config = {"tenancy": "ocid1.tenancy.oc1..test", "region": "us-chicago-1", "key_file": "stale"}
    monkeypatch.setenv("DEPLOY_STUDIO_OCI_CONFIG", "uploaded-config")
    monkeypatch.setenv("DEPLOY_STUDIO_OCI_KEY", "private-uploaded-key.pem")
    monkeypatch.setattr(preflight.oci.config, "from_file", lambda path, profile: dict(config))
    monkeypatch.setattr(preflight.oci.config, "validate_config", lambda value: None)
    loaded = preflight._load_sdk_config()
    assert loaded["key_file"] == "private-uploaded-key.pem"


def test_safe_error_message_keeps_capacity_and_oci_errors_actionable() -> None:
    assert preflight._safe_error_message(RuntimeError("OCI reports no capacity")) == "OCI reports no capacity"
    error = preflight.oci.exceptions.ServiceError(status=429, code="TooManyRequests", headers={}, message="hidden detail")
    assert preflight._safe_error_message(error) == "OCI 429 TooManyRequests"
