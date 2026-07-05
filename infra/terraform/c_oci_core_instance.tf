data "oci_identity_availability_domains" "lab" {
  compartment_id = var.tenancy_ocid
}

locals {
  availability_domain = data.oci_identity_availability_domains.lab.availability_domains[var.availability_domain_index].name
}

resource "terraform_data" "vm_release" {
  input = var.source_commit_sha
}

data "oci_core_images" "oracle_linux" {
  compartment_id   = local.target_compartment
  operating_system = "Oracle Linux"
  # ponytail: OL9.7 images currently stall in initramfs on fresh E5 launches; return to OL9 after OCI publishes a bootable image.
  operating_system_version = "8"
  shape                    = var.preferred_vm_shape
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

resource "oci_core_instance" "lab" {
  compartment_id      = local.target_compartment
  availability_domain = local.availability_domain
  display_name        = "${local.name_prefix}-vm"
  shape               = var.preferred_vm_shape

  shape_config {
    ocpus         = var._oci_instance.shape.ocpus
    memory_in_gbs = var._oci_instance.shape.memory_in_gbs
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.public.id
    assign_public_ip = true
  }

  source_details {
    source_type = "image"
    source_id   = data.oci_core_images.oracle_linux.images[0].id
  }

  metadata = {
    user_data = base64encode(templatefile("${path.module}/templatefile/user_data.sh", {
      admin_username           = var.admin_username
      admin_password_hash      = var.admin_password_hash
      registration_code_hash   = var.registration_code_hash
      identity_domain_url      = local.default_domain.url
      identity_oauth_client_id = oci_identity_domains_app.registration.name
      oauth_secret_ocid        = oci_vault_secret.oauth_client.id
      developer_group_id       = oci_identity_domains_group.developers.id
      pending_group_id         = oci_identity_domains_group.pending.id
      aidp_console_url         = "https://cloud.oracle.com/ai-data-platform/ai-data-platforms/${oci_ai_data_platform_ai_data_platform.lab.id}?region=${var.region}"
      lab_marker               = local.name_prefix
      source_repo_url          = var.source_repository_url
      source_commit_sha        = var.source_commit_sha
    }))
  }

  preserve_boot_volume = false

  lifecycle {
    replace_triggered_by = [terraform_data.vm_release]

    ignore_changes = [
      source_details[0].source_id,
    ]

    precondition {
      condition     = var.availability_domain_index >= 0 && var.availability_domain_index < length(data.oci_identity_availability_domains.lab.availability_domains)
      error_message = "availability_domain_index is outside the region's available domains."
    }
  }
}

resource "oci_identity_dynamic_group" "vm" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-vm"
  description    = "Instance principal for the AIDP lab registration VM"
  matching_rule  = "ALL {instance.id = '${oci_core_instance.lab.id}'}"
}

resource "oci_identity_policy" "vm_secret" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-vm-secret"
  description    = "Allow only the lab VM to read its OAuth secret"
  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.vm.name} to read secret-bundles in compartment id ${local.target_compartment} where target.secret.id = '${oci_vault_secret.oauth_client.id}'"
  ]
}

resource "oci_identity_policy" "vm_run_command" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-vm-run-command"
  description    = "Allow an instance principal to execute commands only on itself"
  statements = [
    "Allow any-user to use instance-agent-command-execution-family in compartment id ${local.target_compartment} where request.instance.id=target.instance.id"
  ]
}

data "oci_core_vnic_attachments" "lab" {
  compartment_id = local.target_compartment
  instance_id    = oci_core_instance.lab.id
}

data "oci_core_vnic" "lab" {
  vnic_id = data.oci_core_vnic_attachments.lab.vnic_attachments[0].vnic_id
}
