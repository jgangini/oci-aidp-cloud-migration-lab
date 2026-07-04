mock_provider "oci" {}
mock_provider "oci" {
  alias = "home"
}
mock_provider "random" {}

override_resource {
  target = random_string.suffix
  values = { result = "test1234" }
}

override_data {
  target = data.oci_identity_domains.default
  values = {
    domains = [{
      id           = "ocid1.domain.test"
      display_name = "Default"
      url          = "https://identity.example.test"
      state        = "ACTIVE"
    }]
  }
}

override_data {
  target = data.oci_identity_domains_app_roles.user_administrator
  values = {
    app_roles = [{
      id           = "user-admin-role"
      display_name = "User Administrator"
      app          = [{ value = "identity-service-app" }]
    }]
  }
}

override_data {
  target = data.oci_identity_availability_domains.lab
  values = { availability_domains = [{ name = "AD-1" }] }
}

override_data {
  target = data.oci_core_images.oracle_linux
  values = { images = [{ id = "ocid1.image.test" }] }
}

override_data {
  target = data.oci_core_vnic_attachments.lab
  values = { vnic_attachments = [{ vnic_id = "ocid1.vnic.test" }] }
}

override_data {
  target = data.oci_core_vnic.lab
  values = { public_ip_address = "192.0.2.10" }
}

run "resolved_compartment_contract" {
  command = plan

  variables {
    tenancy_ocid            = "ocid1.tenancy.oc1..test"
    home_region             = "us-ashburn-1"
    compartment_ocid        = "ocid1.compartment.oc1..test"
    objectstorage_namespace = "testnamespace"
    deployment_suffix       = "test1234"
    admin_password_hash     = "pbkdf2_sha256$600000$salt$digest"
    registration_code_hash  = "pbkdf2_sha256$600000$salt$digest"
    source_commit_sha       = "0123456789abcdef0123456789abcdef01234567"
  }

  assert {
    condition     = oci_objectstorage_bucket.data.name == "aidp-data-test1234"
    error_message = "The single data bucket must use the canonical name."
  }

  assert {
    condition     = oci_ai_data_platform_ai_data_platform.lab.default_workspace_name == "aidp-lab-workspace-test1234"
    error_message = "The AIDP default workspace must be deterministic."
  }

  assert {
    condition     = length(oci_objectstorage_object.prefixes) == 4
    error_message = "Exactly four medallion marker objects must be planned."
  }

  assert {
    condition = anytrue([
      for statement in oci_identity_policy.developer_console.statements :
      strcontains(statement, "manage objects") && strcontains(statement, "target.bucket.name = 'aidp-data-test1234'")
    ])
    error_message = "Developer object access must be limited to the single lab bucket."
  }

  assert {
    condition = anytrue([
      for statement in oci_identity_policy.aidp_service.statements :
      strcontains(statement, "target.bucket.system-tag.orcl-aidp.governingAidpId")
      ]) && anytrue([
      for statement in oci_identity_policy.aidp_service.statements :
      strcontains(statement, "TAG_NAMESPACE_USE")
    ])
    error_message = "AIDP service access must use the official governing tag conditions."
  }

  assert {
    condition = anytrue([
      for statement in oci_identity_policy.aidp_service.statements :
      strcontains(statement, "read objectstorage-namespaces in compartment id ocid1.compartment.oc1..test")
    ])
    error_message = "AIDP namespace inspection must remain scoped to the target compartment."
  }

  assert {
    condition     = oci_core_instance.lab.shape == "VM.Standard.E5.Flex"
    error_message = "The APPLY must use the explicitly requested shape without pretending to fall back."
  }
}

run "explicit_e4_retry_contract" {
  command = plan

  variables {
    tenancy_ocid            = "ocid1.tenancy.oc1..test"
    home_region             = "us-ashburn-1"
    compartment_ocid        = "ocid1.compartment.oc1..test"
    objectstorage_namespace = "testnamespace"
    deployment_suffix       = "test1234"
    admin_password_hash     = "pbkdf2_sha256$600000$salt$digest"
    registration_code_hash  = "pbkdf2_sha256$600000$salt$digest"
    source_commit_sha       = "0123456789abcdef0123456789abcdef01234567"
    preferred_vm_shape      = "VM.Standard.E4.Flex"
  }

  assert {
    condition     = oci_core_instance.lab.shape == "VM.Standard.E4.Flex"
    error_message = "A retry can switch to E4 using only a non-secret Terraform variable."
  }
}
