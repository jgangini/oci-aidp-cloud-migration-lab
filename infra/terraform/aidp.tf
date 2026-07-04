resource "oci_identity_policy" "aidp_service" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-aidp-service"
  description    = "Minimum AIDP Workbench service access for the lab compartment"
  statements = [
    "Allow any-user to {AUTHENTICATION_INSPECT, DOMAIN_INSPECT, DOMAIN_READ, DYNAMIC_GROUP_INSPECT, GROUP_INSPECT, GROUP_MEMBERSHIP_INSPECT, USER_INSPECT, USER_READ} in tenancy where all {request.principal.type='aidataplatform'}",
    "Allow any-user to manage log-groups in compartment id ${local.target_compartment} where all {request.principal.type='aidataplatform'}",
    "Allow any-user to read log-content in compartment id ${local.target_compartment} where all {request.principal.type='aidataplatform'}",
    "Allow any-user to use metrics in compartment id ${local.target_compartment} where all {request.principal.type='aidataplatform', target.metrics.namespace='oracle_aidataplatform'}",
    "Allow any-user to manage buckets in compartment id ${local.target_compartment} where all {request.principal.type='aidataplatform', any {request.permission='BUCKET_CREATE', request.permission='BUCKET_INSPECT', request.permission='BUCKET_READ', request.permission='BUCKET_UPDATE'}}",
    "Allow any-user to {TAG_NAMESPACE_USE} in tenancy where all {request.principal.type='aidataplatform'}",
    "Allow any-user to manage buckets in compartment id ${local.target_compartment} where all {request.principal.id=target.resource.tag.orcl-aidp.governingAidpId, any {request.permission='BUCKET_DELETE', request.permission='PAR_MANAGE', request.permission='RETENTION_RULE_LOCK', request.permission='RETENTION_RULE_MANAGE'}}",
    "Allow any-user to read objectstorage-namespaces in compartment id ${local.target_compartment} where all {request.principal.type='aidataplatform', request.permission='OBJECTSTORAGE_NAMESPACE_READ'}",
    "Allow any-user to manage objects in compartment id ${local.target_compartment} where all {request.principal.id=target.bucket.system-tag.orcl-aidp.governingAidpId}",
    "Allow any-user to manage vnics in compartment id ${local.target_compartment} where all {request.principal.type='aidataplatform'}",
    "Allow any-user to use subnets in compartment id ${local.target_compartment} where all {request.principal.type='aidataplatform'}",
    "Allow any-user to use network-security-groups in compartment id ${local.target_compartment} where all {request.principal.type='aidataplatform'}"
  ]
}

resource "oci_ai_data_platform_ai_data_platform" "lab" {
  compartment_id         = local.target_compartment
  display_name           = local.name_prefix
  default_workspace_name = "aidp-lab-workspace-${local.suffix}"
  ai_data_platform_type  = "STRUCTURED"

  freeform_tags = {
    managed-by = "deploy-studio"
    workload   = "migration-lab"
  }

  depends_on = [oci_identity_policy.aidp_service]
}
