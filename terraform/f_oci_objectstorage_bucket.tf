# OCI Provider 8.21 has no force_destroy argument; its native delete refuses a non-empty bucket.
resource "oci_objectstorage_bucket" "data" {
  compartment_id = local.target_compartment
  namespace      = var.objectstorage_namespace
  name           = "aidp-data-${local.suffix}"
  access_type    = "NoPublicAccess"
  storage_tier   = "Standard"
  versioning     = "Disabled"
  auto_tiering   = "Disabled"

  freeform_tags = {
    managed-by = "deploy-studio"
    data-model = "medallion"
  }
}

# ponytail: prefixes stay virtual until AIDP's first write; add markers only when OCI exposes write readiness.
