from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import oci

from release_gate import compartment_target, validate_context, validate_source


E5_SHAPE = "VM.Standard.E5.Flex"
E4_SHAPE = "VM.Standard.E4.Flex"
E3_SHAPE = "VM.Standard.E3.Flex"
SUPPORTED_SHAPES = (E5_SHAPE, E4_SHAPE, E3_SHAPE)
ACTIVE_WORK_REQUEST_STATES = {"ACCEPTED", "IN_PROGRESS", "WAITING", "NEEDS_ATTENTION", "CANCELING"}


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
        raise ValueError("preferred_vm_shape must be E5 Flex, E4 Flex, or E3 Flex")
    return list(SUPPORTED_SHAPES[SUPPORTED_SHAPES.index(preferred) :])


def _list_all(call: Callable[..., Any], **kwargs: Any) -> list[Any]:
    items: list[Any] = []
    while True:
        response = call(**kwargs)
        data = response.data
        items.extend(data if isinstance(data, list) else (getattr(data, "items", None) or []))
        page = (getattr(response, "headers", None) or {}).get("opc-next-page")
        if not page:
            return items
        kwargs["page"] = page


def _has_active_aidp_work_request(aidp: Any, compartment_ids: set[str]) -> bool:
    return any(
        str(getattr(item, "compartment_id", "")) in compartment_ids
        and str(getattr(item, "status", "")).upper() in ACTIVE_WORK_REQUEST_STATES
        for item in _list_all(aidp.list_work_requests)
    )


def _require_compartment_target(identity: Any, aidp: Any, tenancy_id: str, target: str, mode: str) -> str:
    compartments = _list_all(
        identity.list_compartments,
        compartment_id=tenancy_id,
        compartment_id_in_subtree=True,
        access_level="ANY",
    )
    matches = [
        item
        for item in compartments
        if str(getattr(item, "name", "")).casefold() == target.casefold()
    ]
    active = [item for item in matches if str(getattr(item, "lifecycle_state", "")).upper() == "ACTIVE"]
    if mode == "existing":
        if len(active) != 1:
            raise RuntimeError(f"existing compartment {target} was not found or is ambiguous")
        return f"{target} exists and is ACTIVE"
    occupied = [item for item in matches if str(getattr(item, "lifecycle_state", "")).upper() != "DELETED"]
    if occupied:
        raise RuntimeError(f"compartment {target} is not available to create")
    deleted_ids = {str(item.id) for item in matches if getattr(item, "id", None)}
    if deleted_ids and _has_active_aidp_work_request(aidp, deleted_ids):
        raise RuntimeError(f"a previous AIDP work request is still active for {target}")
    return f"{target} is available to create"


def _require_public_signing_certificate(
    sdk_config: dict[str, Any],
    tenancy_id: str,
    home_region: str,
    identity_factory: Callable[[dict[str, Any]], Any],
    identity_domains_factory: Callable[..., Any],
) -> None:
    home_config = dict(sdk_config)
    home_config["region"] = home_region
    domains = identity_factory(home_config).list_domains(
        compartment_id=tenancy_id,
        type="DEFAULT",
        lifecycle_state="ACTIVE",
    ).data
    if len(domains) != 1 or not getattr(domains[0], "url", None):
        raise RuntimeError("OCI did not return one active Default Identity Domain")
    domain_state = str(getattr(domains[0], "lifecycle_state", getattr(domains[0], "state", "ACTIVE"))).upper()
    if domain_state != "ACTIVE":
        raise RuntimeError("Default Identity Domain must be ACTIVE")
    settings = identity_domains_factory(home_config, service_endpoint=str(domains[0].url)).list_settings(
        limit=2,
        attributes="id,signingCertPublicAccess",
    ).data.resources
    if len(settings or []) != 1 or settings[0].signing_cert_public_access is not True:
        raise RuntimeError("Default Identity Domain must enable Access Signing Certificate for AIDP")


def select_inputs(
    context: dict[str, Any],
    sdk_config: dict[str, Any],
    identity_factory: Callable[[dict[str, Any]], Any] = oci.identity.IdentityClient,
    compute_factory: Callable[[dict[str, Any]], Any] = oci.core.ComputeClient,
    identity_domains_factory: Callable[..., Any] = oci.identity_domains.IdentityDomainsClient,
    aidp_factory: Callable[[dict[str, Any]], Any] = oci.ai_data_platform.AiDataPlatformClient,
) -> dict[str, Any]:
    target, mode = compartment_target(context)
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
    compartment_message = _require_compartment_target(
        identity,
        aidp_factory(regional_config),
        tenancy_id,
        target,
        mode,
    )
    home_region = _home_region(identity, tenancy_id)
    _require_public_signing_certificate(
        sdk_config,
        tenancy_id,
        home_region,
        identity_factory,
        identity_domains_factory,
    )
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
                    and (item.available_count is None or int(item.available_count) >= 1)
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
                    {
                        "name": "Immutable v1.0.0 source",
                        "status": "passed",
                        "message": "v1.0.0 source context and deployment source passed",
                    },
                    {
                        "name": "Compartment availability",
                        "status": "passed",
                        "message": compartment_message,
                    },
                    {"name": "OCI tenancy home region", "status": "passed", "message": home_region},
                    {
                        "name": "Identity Domain signing certificate access",
                        "status": "passed",
                        "message": "Default domain publishes its public JWK",
                    },
                    {
                        "name": "Compute capacity preflight",
                        "status": "passed",
                        "message": f"{selected} available in {availability_domain}",
                    },
                ],
            }
    raise RuntimeError("OCI reports no capacity for the supported E5/E4/E3 Flex shapes in any Availability Domain")


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
        context = _read_json_env("DEPLOY_STUDIO_CONTEXT")
        validate_context(context)
        validate_source(Path(__file__).parent)
        _write_result(select_inputs(context, _load_sdk_config()))
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
