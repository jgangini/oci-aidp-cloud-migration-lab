data "oci_identity_availability_domains" "lab" {
  compartment_id = local.target_compartment
}

locals {
  availability_domain = data.oci_identity_availability_domains.lab.availability_domains[var.availability_domain_index].name
}

data "oci_core_images" "oracle_linux" {
  compartment_id           = local.target_compartment
  operating_system         = "Oracle Linux"
  operating_system_version = "9"
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
    ocpus         = var.vm_ocpus
    memory_in_gbs = var.vm_memory_gbs
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.public.id
    assign_public_ip = true
    display_name     = "${local.name_prefix}-vnic"
    hostname_label   = "aidplab"
    nsg_ids          = [oci_core_network_security_group.web.id]
  }

  source_details {
    source_type = "image"
    source_id   = data.oci_core_images.oracle_linux.images[0].id
  }

  metadata = merge(
    {
      user_data = base64encode(templatefile("${path.module}/templates/cloud-init.yaml.tftpl", {
        admin_username           = var.admin_username
        admin_password_hash      = var.admin_password_hash
        registration_code_hash   = var.registration_code_hash
        identity_domain_url      = local.default_domain.url
        identity_oauth_client_id = oci_identity_domains_app.registration.name
        oauth_secret_ocid        = oci_vault_secret.oauth_client.id
        developer_group_id       = oci_identity_domains_group.developers.id
        pending_group_id         = oci_identity_domains_group.pending.id
        lab_marker               = local.name_prefix
        repository_url           = var.source_repository_url
        source_commit_sha        = var.source_commit_sha
      }))
    },
    var.ssh_public_key == "" ? {} : { ssh_authorized_keys = var.ssh_public_key }
  )

  preserve_boot_volume = false

  lifecycle {
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

data "oci_core_vnic_attachments" "lab" {
  compartment_id = local.target_compartment
  instance_id    = oci_core_instance.lab.id
}

data "oci_core_vnic" "lab" {
  vnic_id = data.oci_core_vnic_attachments.lab.vnic_attachments[0].vnic_id
}
