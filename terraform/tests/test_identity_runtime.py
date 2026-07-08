import re
from pathlib import Path


ROOT = Path(__file__).parents[2]


def _resource(source: str, resource_type: str, label: str) -> str:
    marker = f'resource "{resource_type}" "{label}"'
    start = source.index(marker)
    next_resource = source.find('\nresource "', start + len(marker))
    return source[start:] if next_resource == -1 else source[start:next_resource]


def test_provisioner_is_api_only_without_formal_service_user_flag() -> None:
    identity = (ROOT / "terraform/h_oci_identity.tf").read_text(encoding="utf-8")
    provisioner = _resource(identity, "oci_identity_domains_user", "provisioner")

    assert 'user_type     = "Service"' in provisioner
    assert "service_user = true" not in provisioner
    assert "\n  password" not in provisioner
    assert provisioner.count("emails {") == 2
    assert 'type     = "work"' in provisioner
    assert 'type     = "recovery"' in provisioner
    assert 'value    = "aidp-provisioner-${local.suffix}@example.com"' in provisioner
    assert "primary  = true" in provisioner
    assert provisioner.count("verified = true") == 2
    assert "bypass_notification = true" in provisioner
    assert "can_use_api_keys                 = true" in provisioner
    assert "can_use_console                  = false" in provisioner
    assert "can_use_console_password         = false" in provisioner


def test_provisioner_group_and_iam_are_exactly_scoped() -> None:
    identity = (ROOT / "terraform/h_oci_identity.tf").read_text(encoding="utf-8")
    group = _resource(identity, "oci_identity_domains_group", "provisioner")
    policy = _resource(identity, "oci_identity_policy", "provisioner_runtime")

    assert 'display_name  = "aidp-lab-provisioner-${local.suffix}"' in group
    assert "value = oci_identity_domains_user.provisioner.id" in group
    assert "ocid  = oci_identity_domains_user.provisioner.ocid" in group
    assert policy.count("use ai-data-platforms") == 1
    assert policy.count("read buckets") == 1
    assert policy.count("manage objects") == 1
    assert policy.count("target.bucket.name = '${oci_objectstorage_bucket.data.name}'") == 2
    assert "manage ai-data-platforms" not in policy


def test_provisioner_uses_api_signing_without_oauth_or_vault() -> None:
    identity = (ROOT / "terraform/h_oci_identity.tf").read_text(encoding="utf-8")
    compute = (ROOT / "terraform/g_oci_core_instance.tf").read_text(encoding="utf-8")
    grant = _resource(identity, "oci_identity_domains_grant", "provisioner_user_admin")

    assert 'grant_mechanism = "ADMINISTRATOR_TO_USER"' in grant
    assert 'type  = "User"' in grant
    assert "value = oci_identity_domains_user.provisioner.id" in grant
    assert 'resource "oci_identity_domains_app"' not in identity
    assert 'resource "oci_kms_' not in identity
    assert 'resource "oci_vault_' not in identity
    assert 'resource "oci_identity_policy" "vm_secret"' not in compute
    assert "secret-bundles" not in compute


def test_vm_generates_and_mounts_only_local_api_credentials() -> None:
    compute = (ROOT / "terraform/g_oci_core_instance.tf").read_text(encoding="utf-8")
    cloud_init = (ROOT / "terraform/templatefile/user_data.sh").read_text(encoding="utf-8")

    assert "vm_aidp_runtime" not in compute
    assert "manage datalake" not in compute
    assert re.search(
        r"provisioner_user_ocid\s*=\s*oci_identity_domains_user\.provisioner\.ocid",
        compute,
    )
    assert 'OCI_DIR="/opt/aidp-lab/.oci"' in cloud_init
    assert 'install -d -m 0700 "$TLS_DIR" "$STATE_DIR" "$OCI_DIR"' in cloud_init
    assert 'openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048' in cloud_init
    assert 'chmod 0600 "$OCI_DIR/key.pem" "$OCI_DIR/config"' in cloud_init
    assert 'OCI_CONFIG_FILE=/etc/aidp-lab/oci/config' in cloud_init
    assert 'cat > /opt/aidp-lab/.env' in cloud_init
    assert '--env-file /opt/aidp-lab/.env' in cloud_init
    assert "IDENTITY_OAUTH" not in cloud_init
    assert "OAUTH_SECRET" not in cloud_init
    assert '"$OCI_DIR:/etc/aidp-lab/oci:ro,Z"' in cloud_init
    assert '[ "$HEALTH_STATUS" = "200" ] || [ "$HEALTH_STATUS" = "503" ]' in cloud_init


def test_run_command_can_retrieve_only_public_material() -> None:
    compute = (ROOT / "terraform/g_oci_core_instance.tf").read_text(encoding="utf-8")
    cloud_init = (ROOT / "terraform/templatefile/user_data.sh").read_text(encoding="utf-8")

    assert "manage instance-agent-command-family" in compute
    assert "use instance-agent-command-execution-family" in compute
    helper = cloud_init.split("cat >/usr/local/sbin/aidp-lab-public-key <<'EOF'", 1)[1].split("\nEOF", 1)[0]
    assert "key_public.pem" in helper
    assert "FINGERPRINT=" in helper
    assert "key.pem" not in helper
    assert "config" not in helper
    assert "ocarun ALL=(root) NOPASSWD: /usr/local/sbin/aidp-lab-public-key" in cloud_init


def test_required_aidp_policy_has_no_optional_or_search_resources() -> None:
    aidp = (ROOT / "terraform/i_oci_ai_data_platform.tf").read_text(encoding="utf-8")

    policy = _resource(aidp, "oci_identity_policy", "aidp_service")
    assert policy.count('"Allow any-user') == 9
    assert "manage vnics" not in policy
    assert "use subnets" not in policy
    assert "use network-security-groups" not in policy
    assert "Allow service objectstorage-" not in policy
    assert "manage object-family" not in policy
    assert "opensearch" not in aidp.lower()
    assert "external_volume" not in aidp.lower()
