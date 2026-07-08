mock_provider "oci" {}
mock_provider "oci" {
  alias = "home"
}
mock_provider "random" {}

override_resource {
  target = random_string.suffix
  values = { result = "test1234" }
}

override_resource {
  target          = oci_ai_data_platform_ai_data_platform.lab
  override_during = plan
  values          = { web_socket_endpoint = null }
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
  target = data.oci_identity_tenancy.current
  values = { name = "oci-deploy-1" }
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
    condition     = local.aidp_web_socket_endpoint == ""
    error_message = "A null AIDP WebSocket endpoint must not fail the apply."
  }

  assert {
    condition     = local.medallion_prefixes == ["01_landing/", "02_bronze/", "03_silver/", "04_gold/"]
    error_message = "The four logical medallion prefixes must remain stable."
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
      for statement in oci_identity_policy.developer_console.statements :
      strcontains(statement, "read buckets") && strcontains(statement, "target.bucket.name = 'aidp-data-test1234'")
    ])
    error_message = "Developer bucket metadata access must be limited to the single lab bucket."
  }

  assert {
    condition = anytrue([
      for statement in oci_identity_policy.developer_console.statements :
      strcontains(statement, "Allow group Administrators to manage ai-data-platforms in compartment id ocid1.compartment.oc1..test")
    ])
    error_message = "The deployment operator needs scoped AIDP administration for catalog reconciliation."
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
    condition = alltrue([
      for statement in oci_identity_policy.aidp_service.statements :
      !strcontains(statement, "manage vnics") &&
      !strcontains(statement, "use subnets") &&
      !strcontains(statement, "use network-security-groups") &&
      !strcontains(statement, "Allow service objectstorage-") &&
      !strcontains(statement, "to manage object-family")
    ])
    error_message = "Optional private-network and Object Storage deletion permissions must not be part of the required AIDP policy."
  }

  assert {
    condition     = length(oci_identity_policy.aidp_service.statements) == 9
    error_message = "The stable nine-statement required AIDP Advanced policy must remain complete."
  }

  assert {
    condition = anytrue([
      for statement in oci_identity_policy.vm_run_command.statements :
      strcontains(statement, "Allow dynamic-group aidp-lab-test1234-vm to use instance-agent-command-execution-family") &&
      strcontains(statement, "use instance-agent-command-execution-family") &&
      strcontains(statement, "compartment id ocid1.compartment.oc1..test") &&
      strcontains(statement, "request.instance.id=target.instance.id")
    ])
    error_message = "VM Run Command must be restricted to the lab VM in its compartment."
  }

  assert {
    condition     = length(oci_identity_policy.vm_run_command.statements) == 2
    error_message = "The deployment operator needs scoped command submission access to retrieve the public key."
  }

  assert {
    condition     = oci_core_instance.lab.shape == "VM.Standard.E5.Flex"
    error_message = "The APPLY must use the explicitly requested shape without pretending to fall back."
  }

  assert {
    condition = (
      oci_identity_domains_user.provisioner.user_type == "Service" &&
      oci_identity_domains_user.provisioner.active &&
      length(oci_identity_domains_user.provisioner.urnietfparamsscimschemasoracleidcsextensioncapabilities_user) == 1 &&
      oci_identity_domains_user.provisioner.urnietfparamsscimschemasoracleidcsextensioncapabilities_user[0].can_use_api_keys &&
      !oci_identity_domains_user.provisioner.urnietfparamsscimschemasoracleidcsextensioncapabilities_user[0].can_use_console &&
      !oci_identity_domains_user.provisioner.urnietfparamsscimschemasoracleidcsextensioncapabilities_user[0].can_use_console_password &&
      length(oci_identity_domains_user.provisioner.urnietfparamsscimschemasoracleidcsextensionuser_user) == 1 &&
      oci_identity_domains_user.provisioner.urnietfparamsscimschemasoracleidcsextensionuser_user[0].bypass_notification
    )
    error_message = "The technical provisioner must be API-only and non-interactive."
  }

  assert {
    condition = (
      oci_identity_domains_group.provisioner.display_name == "aidp-lab-provisioner-test1234" &&
      length(oci_identity_domains_group.provisioner.members) == 1 &&
      alltrue([
        for member in oci_identity_domains_group.provisioner.members :
        member.type == "User"
      ])
    )
    error_message = "The dedicated provisioner group must contain exactly the technical user."
  }

  assert {
    condition = (
      oci_identity_domains_grant.provisioner_user_admin.grant_mechanism == "ADMINISTRATOR_TO_USER" &&
      length(oci_identity_domains_grant.provisioner_user_admin.grantee) == 1 &&
      oci_identity_domains_grant.provisioner_user_admin.grantee[0].type == "User"
    )
    error_message = "The API-key provisioner must receive User Administrator directly, without an OAuth app."
  }

  assert {
    condition = (
      length(oci_identity_policy.provisioner_runtime.statements) == 3 &&
      anytrue([
        for statement in oci_identity_policy.provisioner_runtime.statements :
        strcontains(statement, "use ai-data-platforms")
      ]) &&
      alltrue([
        for statement in oci_identity_policy.provisioner_runtime.statements :
        !strcontains(statement, "objects") || strcontains(statement, "target.bucket.name = 'aidp-data-test1234'")
      ])
    )
    error_message = "Provisioner IAM must be limited to AIDP use and the exact lab bucket."
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

run "authorized_e3_fallback_contract" {
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
    preferred_vm_shape      = "VM.Standard.E3.Flex"
  }

  assert {
    condition     = oci_core_instance.lab.shape == "VM.Standard.E3.Flex"
    error_message = "The authorized fallback can use E3 with the same flexible configuration."
  }
}
