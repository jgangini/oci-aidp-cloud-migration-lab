import base64
import importlib.util
import json
import sys
from pathlib import Path


def test_deploy_studio_manifest_contract() -> None:
    root = Path(__file__).parents[3]
    manifest = json.loads((root / "deploy-studio.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["project_id"] == "oci-aidp-cloud-migration-lab"
    assert manifest["terraform"] == {"path": "infra/terraform", "package_oci_credentials": False}
    assert manifest["capabilities"]["database_profile"] == "none"
    assert manifest["post_apply"]["requires_oci_credentials"] is True
    assert manifest["post_apply"]["entrypoint"] == "infra/terraform/hooks/post_apply.py"
    assert manifest["post_apply"]["timeout_seconds"] == 3600
    assert (root / manifest["post_apply"]["entrypoint"]).is_file()
    fields = {field["name"]: field for field in manifest["form"]["fields"]}
    assert "home_region" not in fields
    assert "preferred_vm_shape" not in fields
    assert "vm_ocpus" not in fields
    assert "vm_memory_gbs" not in fields
    assert "ssh_allowed_cidr" not in fields
    assert fields["admin_password"]["transform"] == "pbkdf2_sha256"
    assert fields["registration_code"]["pattern"] == "^[A-Z]{4}-[0-9]{4}$"
    assert fields["registration_code"]["transform"] == "uppercase_pbkdf2_sha256"
    assert manifest["presentation"]["title"] == "OCI AIDP Cloud Migration Lab"
    assert manifest["presentation"]["tags"] == ["AIDP Workbench", "Medallion Storage", "Identity Domains", "Object Storage", "HTTPS VM"]
    assert [field["name"] for field in manifest["preflight"]["runtime_fields"]] == ["home_region", "preferred_vm_shape"]
    assert manifest["preflight"]["output_inputs"] == ["home_region", "preferred_vm_shape"]
    assert (root / manifest["preflight"]["entrypoint"]).is_file()


def test_hook_result_matches_runner_and_manifest_contract() -> None:
    root = Path(__file__).parents[3]
    manifest = json.loads((root / "deploy-studio.json").read_text(encoding="utf-8"))
    module_path = root / manifest["post_apply"]["entrypoint"]
    spec = importlib.util.spec_from_file_location("post_apply_manifest_contract", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    context = {
        "deployment_id": "deployment-test",
        "source": {"repository": "owner/repo", "ref": "v1.0.0", "commit_sha": "0" * 40},
    }
    result = module.build_success_result(context, {"catalog_key": "catalog"}, ["ready"])
    assert set(result) == {"events", "artifacts", "outputs"}
    assert result["outputs"] == {}
    assert {item["name"] for item in result["artifacts"]} == set(manifest["artifacts"])
    assert set(result["outputs"]).issubset(manifest["outputs"])
    artifact = json.loads(base64.b64decode(result["artifacts"][0]["content_b64"]))
    assert artifact["resources"] == {"catalog_key": "catalog"}


def test_runtime_security_contracts() -> None:
    root = Path(__file__).parents[3]
    nginx = (root / "docker/nginx.conf").read_text(encoding="utf-8")
    entrypoint = (root / "docker/entrypoint.sh").read_text(encoding="utf-8")
    cloud_init = (root / "infra/terraform/templates/cloud-init.yaml.tftpl").read_text(encoding="utf-8")
    variables = (root / "infra/terraform/variables.tf").read_text(encoding="utf-8")
    providers = (root / "infra/terraform/providers.tf").read_text(encoding="utf-8")
    compute = (root / "infra/terraform/compute.tf").read_text(encoding="utf-8")
    identity = (root / "infra/terraform/identity.tf").read_text(encoding="utf-8")
    aidp = (root / "infra/terraform/aidp.tf").read_text(encoding="utf-8")
    storage = (root / "infra/terraform/storage.tf").read_text(encoding="utf-8")
    assert "$proxy_add_x_forwarded_for" not in nginx
    assert nginx.count("X-Forwarded-For $remote_addr") == 2
    assert "limit_req_status 429" in nginx
    assert 'return 429 \'{"detail":"Too many registration attempts"}\'' in nginx
    assert 'chmod 0600 "$TLS_DIR/tls.key" 2>/dev/null || true' in entrypoint
    assert "firewall-cmd --add-service=http --permanent" in cloud_init
    assert "firewall-cmd --add-service=https --permanent" in cloud_init
    assert "/opt/aidp-lab/tls:/etc/aidp-lab/tls:ro,Z" in cloud_init
    assert "/opt/aidp-lab/state:/var/lib/aidp-lab:Z" in cloud_init
    assert 'alias  = "home"' in providers
    assert "region = var.home_region" in providers
    assert "shape_candidates" not in compute
    assert "oci_core_shapes" not in compute
    assert compute.count("var.preferred_vm_shape") == 2
    assert identity.count("oci.home") == 7
    assert compute.count("oci.home") == 2
    assert aidp.count("oci.home") == 1
    source_sha_block = variables.split('variable "source_commit_sha"', 1)[1]
    assert 'default     = "main"' not in source_sha_block
    assert 'regex("^[0-9a-f]{40}$"' in source_sha_block
    assert "force_destroy" in storage
    assert "prevent_destroy" not in storage
