module "c1_vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "6.6.1"

  providers = {
    aws = aws.c1
  }

  name = "${var.project_name}-c1"
  cidr = var.c1_vpc_cidr
  azs  = local.c1_azs

  private_subnets = [cidrsubnet(var.c1_vpc_cidr, 4, 0), cidrsubnet(var.c1_vpc_cidr, 4, 1)]
  public_subnets  = [cidrsubnet(var.c1_vpc_cidr, 8, 128), cidrsubnet(var.c1_vpc_cidr, 8, 129)]

  enable_nat_gateway     = true
  single_nat_gateway     = true
  one_nat_gateway_per_az = false

  enable_dns_hostnames = true
  enable_dns_support   = true

  map_public_ip_on_launch = false

  private_subnet_tags = {
    "kubernetes.io/role/internal-elb"                = "1"
    "kubernetes.io/cluster/${local.c1_cluster_name}" = "shared"
  }

  public_subnet_tags = {
    "kubernetes.io/role/elb"                         = "1"
    "kubernetes.io/cluster/${local.c1_cluster_name}" = "shared"
  }
}

module "c2_vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "6.6.1"

  providers = {
    aws = aws.c2
  }

  name = "${var.project_name}-c2"
  cidr = var.c2_vpc_cidr
  azs  = local.c2_azs

  private_subnets = [cidrsubnet(var.c2_vpc_cidr, 4, 0), cidrsubnet(var.c2_vpc_cidr, 4, 1)]
  public_subnets  = [cidrsubnet(var.c2_vpc_cidr, 8, 128), cidrsubnet(var.c2_vpc_cidr, 8, 129)]

  enable_nat_gateway     = true
  single_nat_gateway     = true
  one_nat_gateway_per_az = false

  enable_dns_hostnames = true
  enable_dns_support   = true

  map_public_ip_on_launch = false

  private_subnet_tags = {
    "kubernetes.io/role/internal-elb"                = "1"
    "kubernetes.io/cluster/${local.c2_cluster_name}" = "shared"
  }

  public_subnet_tags = {
    "kubernetes.io/role/elb"                         = "1"
    "kubernetes.io/cluster/${local.c2_cluster_name}" = "shared"
  }
}

resource "aws_vpc_peering_connection" "c1_to_c2" {
  provider = aws.c1

  vpc_id      = module.c1_vpc.vpc_id
  peer_vpc_id = module.c2_vpc.vpc_id
  peer_region = var.c2_region
  auto_accept = false

  tags = {
    Name = "${var.project_name}-c1-c2"
  }
}

resource "aws_vpc_peering_connection_accepter" "c2" {
  provider = aws.c2

  vpc_peering_connection_id = aws_vpc_peering_connection.c1_to_c2.id
  auto_accept               = true

  tags = {
    Name = "${var.project_name}-c1-c2"
  }
}

resource "aws_vpc_peering_connection_options" "c1" {
  provider = aws.c1

  vpc_peering_connection_id = aws_vpc_peering_connection_accepter.c2.id

  requester {
    allow_remote_vpc_dns_resolution = true
  }
}

resource "aws_vpc_peering_connection_options" "c2" {
  provider = aws.c2

  vpc_peering_connection_id = aws_vpc_peering_connection_accepter.c2.id

  accepter {
    allow_remote_vpc_dns_resolution = true
  }
}

resource "aws_route" "c1_private_to_c2" {
  provider = aws.c1
  count    = length(module.c1_vpc.private_route_table_ids)

  route_table_id            = module.c1_vpc.private_route_table_ids[count.index]
  destination_cidr_block    = var.c2_vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection_accepter.c2.id
}

resource "aws_route" "c2_private_to_c1" {
  provider = aws.c2
  count    = length(module.c2_vpc.private_route_table_ids)

  route_table_id            = module.c2_vpc.private_route_table_ids[count.index]
  destination_cidr_block    = var.c1_vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection_accepter.c2.id
}

# A dedicated ACL on every C2 private/NLB subnet gives Fault 1 a deterministic,
# enumerated injection point. SGs and Kubernetes policies provide steady-state
# filtering; this baseline ACL deliberately permits traffic in both directions.
resource "aws_network_acl" "c2_private" {
  provider = aws.c2

  vpc_id     = module.c2_vpc.vpc_id
  subnet_ids = module.c2_vpc.private_subnets

  ingress {
    protocol   = "-1"
    rule_no    = 100
    action     = "allow"
    cidr_block = "0.0.0.0/0"
    from_port  = 0
    to_port    = 0
  }

  egress {
    protocol   = "-1"
    rule_no    = 100
    action     = "allow"
    cidr_block = "0.0.0.0/0"
    from_port  = 0
    to_port    = 0
  }

  tags = {
    Name                    = "${var.project_name}-c2-private"
    FaultInjectionRule      = "50"
    FaultInjectionDirection = "ingress"
  }
}
