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
    assert [field["name"] for field in manifest["preflight"]["runtime_fields"]] == ["home_region", "preferred_vm_shape", "availability_domain_index"]
    assert manifest["preflight"]["output_inputs"] == ["home_region", "preferred_vm_shape", "availability_domain_index"]
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
    cloud_init = (root / "infra/terraform/templatefile/user_data.sh").read_text(encoding="utf-8")
    variables = (root / "infra/terraform/variables.tf").read_text(encoding="utf-8")
    providers = (root / "infra/terraform/a_main.tf").read_text(encoding="utf-8")
    compute = (root / "infra/terraform/c_oci_core_instance.tf").read_text(encoding="utf-8")
    identity = (root / "infra/terraform/d_oci_identity.tf").read_text(encoding="utf-8")
    aidp = (root / "infra/terraform/e_oci_ai_data_platform.tf").read_text(encoding="utf-8")
    storage = (root / "infra/terraform/b_oci_objectstorage_bucket.tf").read_text(encoding="utf-8")
    assert "$proxy_add_x_forwarded_for" not in nginx
    assert nginx.count("X-Forwarded-For $remote_addr") == 2
    assert "limit_req_status 429" in nginx
    assert 'return 429 \'{"detail":"Too many registration attempts"}\'' in nginx
    assert 'chmod 0600 "$TLS_DIR/tls.key" 2>/dev/null || true' in entrypoint
    assert "firewall-offline-cmd --zone=public --add-service=http" in cloud_init
    assert "firewall-offline-cmd --zone=public --add-service=https" in cloud_init
    assert "firewall-cmd" not in cloud_init
    assert "download.docker.com/linux/centos/docker-ce.repo" in cloud_init
    assert "public.ecr.aws/docker/library/node" in cloud_init
    assert "public.ecr.aws/docker/library/python" in cloud_init
    assert "retry 5 docker build" in cloud_init
    assert "metadata_public_ip()" in cloud_init
    assert "http://169.254.169.254/opc/v2/vnics/" in cloud_init
    assert 'Authorization: Bearer Oracle' in cloud_init
    assert "PUBLIC_IP=$(metadata_public_ip" in cloud_init
    assert "tr -cd 'A-Za-z0-9.-'" in cloud_init
    assert "IP.1 = $PUBLIC_IP" in cloud_init
    assert "DNS.1 = $FQDN" in cloud_init
    assert "-addext" not in cloud_init
    assert "touch /var/local/userdata.done" in cloud_init
    assert '"$TLS_DIR:/etc/aidp-lab/tls:ro,Z"' in cloud_init
    assert '"$STATE_DIR:/var/lib/aidp-lab:Z"' in cloud_init
    assert 'alias  = "home"' in providers
    assert "region = var.home_region" in providers
    assert "shape_candidates" not in compute
    assert "oci_core_shapes" not in compute
    assert compute.count("var.preferred_vm_shape") == 2
    assert 'operating_system_version = "8"' in compute
    assert "var._oci_instance.shape.ocpus" in compute
    assert "var._oci_instance.shape.memory_in_gbs" in compute
    assert 'name          = "Compute Instance Run Command"' in compute
    assert 'desired_state = "ENABLED"' in compute
    assert 'variable "vm_ocpus"' not in variables
    assert 'variable "vm_memory_gbs"' not in variables
    assert identity.count("oci.home") == 7
    assert compute.count("oci.home") == 3
    assert aidp.count("oci.home") == 1
    assert 'resource "time_sleep" "kms_endpoint"' in identity
    assert 'create_duration = "120s"' in identity
    assert "depends_on          = [time_sleep.kms_endpoint]" in identity
    network = (root / "infra/terraform/b_oci_core_vcn.tf").read_text(encoding="utf-8")
    assert 'resource "oci_core_security_list" "web"' in network
    assert "security_list_ids          = [oci_core_security_list.web.id]" in network
    assert 'dns_label      = "aidplab"' in network
    assert 'dns_label                  = "public"' in network
    assert 'ingress_tcp_ports = [80, 443]' in variables
    assert "oci_core_network_security_group" not in network
    source_sha_block = variables.split('variable "source_commit_sha"', 1)[1]
    assert 'default     = "main"' not in source_sha_block
    assert 'regex("^[0-9a-f]{40}$"' in source_sha_block
    assert "force_destroy" in storage
    assert "prevent_destroy" not in storage


def test_terraform_files_follow_select_ai_order() -> None:
    root = Path(__file__).parents[3] / "infra/terraform"
    expected = {
        "a_main.tf",
        "b_oci_core_vcn.tf",
        "b_oci_objectstorage_bucket.tf",
        "c_oci_core_instance.tf",
        "d_oci_identity.tf",
        "e_oci_ai_data_platform.tf",
        "f_outputs.tf",
        "naming.tf",
        "variables.tf",
        "versions.tf",
    }
    assert expected.issubset({path.name for path in root.glob("*.tf")})
    assert not {"main.tf", "network.tf", "compute.tf", "storage.tf", "identity.tf", "aidp.tf", "outputs.tf", "providers.tf"} & {
        path.name for path in root.glob("*.tf")
    }
