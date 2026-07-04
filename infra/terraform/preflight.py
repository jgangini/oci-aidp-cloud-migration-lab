from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import oci


E5_SHAPE = "VM.Standard.E5.Flex"
E4_SHAPE = "VM.Standard.E4.Flex"
SUPPORTED_SHAPES = (E5_SHAPE, E4_SHAPE)


def _safe_error_message(exc: Exception) -> str:
    if isinstance(exc, oci.exceptions.ServiceError):
        return f"OCI {exc.status} {exc.code}"
    if isinstance(exc, (RuntimeError, ValueError)):
        return str(exc)[:256]
    return type(exc).__name__


def _home_region(identity: Any, tenancy_id: str) -> str:
    tenancy = identity.get_tenancy(tenancy_id).data
    home_key = str(tenancy.home_region_key).upper()
    subscriptions = identity.list_region_subscriptions(tenancy_id).data
    matches = [item for item in subscriptions if str(item.region_key).upper() == home_key]
    if len(matches) != 1 or not matches[0].region_name:
        raise RuntimeError("OCI did not return one unambiguous tenancy home region")
    return str(matches[0].region_name)


def _candidate_shapes(preferred: str) -> list[str]:
    if preferred not in SUPPORTED_SHAPES:
        raise ValueError("preferred_vm_shape must be E5 Flex or E4 Flex")
    return [preferred, E4_SHAPE] if preferred == E5_SHAPE else [E4_SHAPE]


def select_inputs(
    context: dict[str, Any],
    sdk_config: dict[str, Any],
    identity_factory: Callable[[dict[str, Any]], Any] = oci.identity.IdentityClient,
    compute_factory: Callable[[dict[str, Any]], Any] = oci.core.ComputeClient,
) -> dict[str, Any]:
    inputs = context.get("inputs") or {}
    region = str(context.get("region") or sdk_config.get("region") or "").strip()
    tenancy_id = str(sdk_config.get("tenancy") or "").strip()
    if not region or not tenancy_id:
        raise ValueError("preflight requires deployment region and tenancy OCID")

    candidates = _candidate_shapes(E5_SHAPE)
    ocpus = 2.0
    memory = 16.0
    regional_config = dict(sdk_config)
    regional_config["region"] = region
    identity = identity_factory(regional_config)
    home_region = _home_region(identity, tenancy_id)
    availability_domains = identity.list_availability_domains(tenancy_id).data
    compute = compute_factory(regional_config)
    for availability_domain_index, domain in enumerate(availability_domains):
        availability_domain = str(domain.name)
        details = oci.core.models.CreateComputeCapacityReportDetails(
            compartment_id=tenancy_id,
            availability_domain=availability_domain,
            shape_availabilities=[
                oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                    instance_shape=shape,
                    instance_shape_config=oci.core.models.CapacityReportInstanceShapeConfig(
                        ocpus=ocpus,
                        memory_in_gbs=memory,
                    ),
                )
                for shape in SUPPORTED_SHAPES
            ],
        )
        report = compute.create_compute_capacity_report(details).data
        selected = next(
            (
                shape
                for shape in candidates
                if any(
                    str(item.instance_shape) == shape
                    and item.availability_status
                    == oci.core.models.CapacityReportShapeAvailability.AVAILABILITY_STATUS_AVAILABLE
                    and int(item.available_count or 0) >= 1
                    for item in report.shape_availabilities
                )
            ),
            None,
        )
        if selected:
            return {
                "inputs": {
                    "home_region": home_region,
                    "preferred_vm_shape": selected,
                    "availability_domain_index": availability_domain_index,
                },
                "events": [
                    {"name": "OCI tenancy home region", "status": "passed", "message": home_region},
                    {
                        "name": "Compute capacity preflight",
                        "status": "passed",
                        "message": f"{selected} available in {availability_domain}",
                    },
                ],
            }
    raise RuntimeError("OCI reports no capacity for the supported E5/E4 Flex shapes in any Availability Domain")


def _read_json_env(name: str) -> dict[str, Any]:
    return json.loads(Path(os.environ[name]).read_text(encoding="utf-8"))


def _write_result(payload: dict[str, Any]) -> None:
    path = Path(os.environ["DEPLOY_STUDIO_RESULT"])
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    path.chmod(0o600)


def _load_sdk_config() -> dict[str, Any]:
    config = oci.config.from_file(os.environ["DEPLOY_STUDIO_OCI_CONFIG"], "DEFAULT")
    config["key_file"] = os.environ["DEPLOY_STUDIO_OCI_KEY"]
    oci.config.validate_config(config)
    return config


def main() -> int:
    try:
        _write_result(select_inputs(_read_json_env("DEPLOY_STUDIO_CONTEXT"), _load_sdk_config()))
        return 0
    except Exception as exc:  # The runner receives a bounded, secret-free failure event.
        _write_result(
            {
                "inputs": {},
                "events": [
                    {
                        "name": "OCI deployment preflight",
                        "status": "failed",
                        "message": _safe_error_message(exc),
                    }
                ],
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
