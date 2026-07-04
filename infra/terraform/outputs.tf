output "application_url" {
  description = "Self-signed HTTPS registration application."
  value       = "https://${data.oci_core_vnic.lab.public_ip_address}"
}

output "admin_url" {
  description = "Administrator users page."
  value       = "https://${data.oci_core_vnic.lab.public_ip_address}/admin/users"
}

output "aidp_console_url" {
  description = "OCI Console deep link for the AIDP platform."
  value       = "https://cloud.oracle.com/ai-data-platform/ai-data-platforms/${oci_ai_data_platform_ai_data_platform.lab.id}?region=${var.region}"
}

output "compartment_ocid" {
  value = local.target_compartment
}

output "bucket_name" {
  value = oci_objectstorage_bucket.data.name
}

output "objectstorage_namespace" {
  value = var.objectstorage_namespace
}

output "medallion_prefixes" {
  value = local.medallion_prefixes
}

output "ai_data_platform_id" {
  value = oci_ai_data_platform_ai_data_platform.lab.id
}

output "default_workspace_name" {
  value = oci_ai_data_platform_ai_data_platform.lab.default_workspace_name
}

output "developer_group_ocid" {
  value = oci_identity_domains_group.developers.ocid
}

output "pending_group_ocid" {
  value = oci_identity_domains_group.pending.ocid
}

output "identity_domain_url" {
  value = local.default_domain.url
}

output "instance_id" {
  value = oci_core_instance.lab.id
}

output "public_ip" {
  value = data.oci_core_vnic.lab.public_ip_address
}

output "vm_shape" {
  description = "Explicit shape used by this APPLY."
  value       = oci_core_instance.lab.shape
}
