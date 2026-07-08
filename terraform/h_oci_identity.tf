data "oci_identity_domains" "default" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  type           = "DEFAULT"
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

# A formal Identity Domains serviceUser cannot have OCI API keys. This account uses the
# descriptive Service type instead, with API signing enabled and all interactive access disabled.
resource "oci_identity_domains_user" "provisioner" {
  provider      = oci.home
  idcs_endpoint = local.default_domain.url
  schemas       = ["urn:ietf:params:scim:schemas:core:2.0:User"]
  user_name     = "${local.name_prefix}-provisioner"
  display_name  = "AIDP Lab Provisioner ${local.suffix}"
  description   = "Non-interactive API principal for participant AIDP provisioning"
  external_id   = "${local.name_prefix}:provisioner"
  active        = true
  user_type     = "Service"
  force_delete  = true

  # Identity Domains normally requires one primary email even for a non-interactive
  # API principal. example.com is reserved and notifications stay disabled below.
  emails {
    type     = "work"
    value    = "aidp-provisioner-${local.suffix}@example.com"
    primary  = true
    verified = true
  }

  emails {
    type     = "recovery"
    value    = "aidp-provisioner-${local.suffix}@example.com"
    verified = true
  }

  urnietfparamsscimschemasoracleidcsextensioncapabilities_user {
    can_use_api_keys                 = true
    can_use_auth_tokens              = false
    can_use_console                  = false
    can_use_console_password         = false
    can_use_customer_secret_keys     = false
    can_use_db_credentials           = false
    can_use_oauth2client_credentials = false
    can_use_smtp_credentials         = false
  }

  urnietfparamsscimschemasoracleidcsextensionuser_user {
    bypass_notification = true
  }
}

resource "oci_identity_domains_group" "provisioner" {
  provider      = oci.home
  idcs_endpoint = local.default_domain.url
  schemas       = ["urn:ietf:params:scim:schemas:core:2.0:Group"]
  display_name  = "aidp-lab-provisioner-${local.suffix}"
  external_id   = "${local.name_prefix}:provisioner"
  force_delete  = true

  members {
    type  = "User"
    value = oci_identity_domains_user.provisioner.id
    ocid  = oci_identity_domains_user.provisioner.ocid
  }
}

data "oci_identity_domains_app_roles" "user_administrator" {
  provider        = oci.home
  idcs_endpoint   = local.default_domain.url
  app_role_filter = "displayName eq \"User Administrator\""
  app_role_count  = 2
}

resource "oci_identity_domains_grant" "provisioner_user_admin" {
  provider        = oci.home
  idcs_endpoint   = local.default_domain.url
  schemas         = ["urn:ietf:params:scim:schemas:oracle:idcs:Grant"]
  grant_mechanism = "ADMINISTRATOR_TO_USER"

  grantee {
    type  = "User"
    value = oci_identity_domains_user.provisioner.id
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

resource "oci_identity_policy" "developer_console" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-developer-console"
  description    = "Allow registered lab developers to open AIDP and use only the lab data bucket"
  statements = [
    "Allow group Administrators to manage ai-data-platforms in compartment id ${local.target_compartment}",
    "Allow group '${local.default_domain.display_name}'/'${oci_identity_domains_group.developers.display_name}' to use ai-data-platforms in compartment id ${local.target_compartment}",
    "Allow group '${local.default_domain.display_name}'/'${oci_identity_domains_group.developers.display_name}' to read buckets in compartment id ${local.target_compartment} where target.bucket.name = '${oci_objectstorage_bucket.data.name}'",
    "Allow group '${local.default_domain.display_name}'/'${oci_identity_domains_group.developers.display_name}' to manage objects in compartment id ${local.target_compartment} where target.bucket.name = 'aidp-data-${local.suffix}'"
  ]
}

resource "oci_identity_policy" "provisioner_runtime" {
  provider       = oci.home
  compartment_id = var.tenancy_ocid
  name           = "${local.name_prefix}-provisioner-runtime"
  description    = "Allow only the technical provisioner to use AIDP and the lab data bucket"
  statements = [
    "Allow group '${local.default_domain.display_name}'/'${oci_identity_domains_group.provisioner.display_name}' to use ai-data-platforms in compartment id ${local.target_compartment}",
    "Allow group '${local.default_domain.display_name}'/'${oci_identity_domains_group.provisioner.display_name}' to read buckets in compartment id ${local.target_compartment} where target.bucket.name = '${oci_objectstorage_bucket.data.name}'",
    "Allow group '${local.default_domain.display_name}'/'${oci_identity_domains_group.provisioner.display_name}' to manage objects in compartment id ${local.target_compartment} where target.bucket.name = '${oci_objectstorage_bucket.data.name}'"
  ]
}
