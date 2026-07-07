data "oci_identity_domains" "default" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  display_name   = "Default"
  state          = "ACTIVE"
}

locals {
  default_domain = one(data.oci_identity_domains.default.domains)
}

resource "oci_identity_domains_group" "developers" {
  provider      = oci.home
  idcs_endpoint = local.default_domain.url
  schemas       = ["urn:ietf:params:scim:schemas:core:2.0:Group"]
  display_name  = "aidp-lab-developers-${local.suffix}"
  external_id   = "${local.name_prefix}:developers"
  force_delete  = true
}

resource "oci_identity_domains_group" "pending" {
  provider      = oci.home
  idcs_endpoint = local.default_domain.url
  schemas       = ["urn:ietf:params:scim:schemas:core:2.0:Group"]
  display_name  = "aidp-lab-pending-${local.suffix}"
  external_id   = "${local.name_prefix}:pending"
  force_delete  = true
}

resource "oci_identity_domains_app" "registration" {
  provider      = oci.home
  idcs_endpoint = local.default_domain.url
  schemas       = ["urn:ietf:params:scim:schemas:oracle:idcs:App"]
  display_name  = "${local.name_prefix}-registration"
  description   = "Confidential OAuth client for the AIDP lab registration VM"
  active        = true
  force_delete  = true

  based_on_template {
    value         = "CustomWebAppTemplateId"
    well_known_id = "CustomWebAppTemplateId"
  }

  is_oauth_client = true
  client_type     = "confidential"
  allowed_grants  = ["client_credentials"]
  trust_scope     = "Account"
  bypass_consent  = true
  show_in_my_apps = false

}

data "oci_identity_domains_app_roles" "user_administrator" {
  provider        = oci.home
  idcs_endpoint   = local.default_domain.url
  app_role_filter = "displayName eq \"User Administrator\""
  app_role_count  = 2
}

resource "oci_identity_domains_grant" "registration_user_admin" {
  provider        = oci.home
  idcs_endpoint   = local.default_domain.url
  schemas         = ["urn:ietf:params:scim:schemas:oracle:idcs:Grant"]
  grant_mechanism = "ADMINISTRATOR_TO_APP"

  grantee {
    type  = "App"
    value = oci_identity_domains_app.registration.id
  }

  app {
    value = one(data.oci_identity_domains_app_roles.user_administrator.app_roles).app[0].value
  }

  entitlement {
    attribute_name  = "appRoles"
    attribute_value = one(data.oci_identity_domains_app_roles.user_administrator.app_roles).id
  }

  lifecycle {
    precondition {
      condition     = length(data.oci_identity_domains_app_roles.user_administrator.app_roles) == 1
      error_message = "The Default Identity Domain must expose exactly one User Administrator app role."
    }
  }
}

resource "oci_kms_vault" "lab" {
  compartment_id = local.target_compartment
  display_name   = "${local.name_prefix}-vault"
  vault_type     = "DEFAULT"
}

resource "time_sleep" "kms_endpoint" {
  # ponytail: live Resource Manager DNS was still unavailable after 339s; use 7m until OCI exposes endpoint readiness.
  create_duration = "420s"
  depends_on      = [oci_kms_vault.lab]
  triggers = {
    vault_id = oci_kms_vault.lab.id
  }
}

resource "oci_kms_key" "lab" {
  compartment_id      = local.target_compartment
  display_name        = "${local.name_prefix}-key"
  management_endpoint = oci_kms_vault.lab.management_endpoint
  depends_on          = [time_sleep.kms_endpoint]
  protection_mode     = "SOFTWARE"

  key_shape {
    algorithm = "AES"
    length    = 32
  }
}

resource "oci_vault_secret" "oauth_client" {
  compartment_id = local.target_compartment
  vault_id       = oci_kms_vault.lab.id
  key_id         = oci_kms_key.lab.id
  secret_name    = "${local.name_prefix}-oauth-client"
  description    = "OAuth client secret used only by the AIDP lab VM"

  secret_content {
    content_type = "BASE64"
    content      = base64encode(oci_identity_domains_app.registration.client_secret)
  }

  depends_on = [oci_identity_domains_grant.registration_user_admin]
}

resource "oci_identity_policy" "developer_console" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-developer-console"
  description    = "Allow registered lab developers to open AIDP and use only the lab data bucket"
  statements = [
    "Allow group Administrators to manage ai-data-platforms in compartment id ${local.target_compartment}",
    "Allow group '${local.default_domain.display_name}'/'${oci_identity_domains_group.developers.display_name}' to use ai-data-platforms in compartment id ${local.target_compartment}",
    "Allow group '${local.default_domain.display_name}'/'${oci_identity_domains_group.developers.display_name}' to read buckets in compartment id ${local.target_compartment}",
    "Allow group '${local.default_domain.display_name}'/'${oci_identity_domains_group.developers.display_name}' to manage objects in compartment id ${local.target_compartment} where target.bucket.name = 'aidp-data-${local.suffix}'"
  ]
}
