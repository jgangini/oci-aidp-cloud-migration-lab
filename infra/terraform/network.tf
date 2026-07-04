resource "oci_core_vcn" "lab" {
  compartment_id = local.target_compartment
  cidr_blocks    = ["10.42.0.0/16"]
  display_name   = "${local.name_prefix}-vcn"
  dns_label      = "aidplab"
}

resource "oci_core_internet_gateway" "lab" {
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-igw"
  enabled        = true
}

resource "oci_core_route_table" "public" {
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-public-routes"

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.lab.id
  }
}

resource "oci_core_subnet" "public" {
  compartment_id             = local.target_compartment
  vcn_id                     = oci_core_vcn.lab.id
  cidr_block                 = "10.42.10.0/24"
  display_name               = "${local.name_prefix}-public-subnet"
  dns_label                  = "public"
  route_table_id             = oci_core_route_table.public.id
  security_list_ids          = [oci_core_vcn.lab.default_security_list_id]
  prohibit_public_ip_on_vnic = false
}

resource "oci_core_network_security_group" "web" {
  compartment_id = local.target_compartment
  vcn_id         = oci_core_vcn.lab.id
  display_name   = "${local.name_prefix}-web-nsg"
}

resource "oci_core_network_security_group_security_rule" "https" {
  network_security_group_id = oci_core_network_security_group.web.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = "0.0.0.0/0"
  source_type               = "CIDR_BLOCK"
  tcp_options {
    destination_port_range {
      min = 443
      max = 443
    }
  }
}

resource "oci_core_network_security_group_security_rule" "http" {
  network_security_group_id = oci_core_network_security_group.web.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = "0.0.0.0/0"
  source_type               = "CIDR_BLOCK"
  tcp_options {
    destination_port_range {
      min = 80
      max = 80
    }
  }
}

resource "oci_core_network_security_group_security_rule" "ssh" {
  count                     = var.ssh_allowed_cidr == "" ? 0 : 1
  network_security_group_id = oci_core_network_security_group.web.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = var.ssh_allowed_cidr
  source_type               = "CIDR_BLOCK"
  tcp_options {
    destination_port_range {
      min = 22
      max = 22
    }
  }
}

resource "oci_core_network_security_group_security_rule" "egress" {
  network_security_group_id = oci_core_network_security_group.web.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
}
