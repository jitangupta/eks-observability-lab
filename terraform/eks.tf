module "c1_eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "21.24.0"

  providers = {
    aws = aws.c1
  }

  name               = local.c1_cluster_name
  kubernetes_version = var.kubernetes_version

  endpoint_private_access      = true
  endpoint_public_access       = true
  endpoint_public_access_cidrs = [var.operator_cidr]

  authentication_mode                      = "API_AND_CONFIG_MAP"
  enable_cluster_creator_admin_permissions = true
  enable_irsa                              = true

  enabled_log_types                      = ["api", "audit", "authenticator", "controllerManager", "scheduler"]
  create_cloudwatch_log_group            = true
  cloudwatch_log_group_retention_in_days = 7

  vpc_id     = module.c1_vpc.vpc_id
  subnet_ids = module.c1_vpc.private_subnets

  addons = {
    coredns = {
      most_recent = true
    }
    kube-proxy = {
      most_recent = true
    }
    vpc-cni = {
      before_compute = true
      most_recent    = true
      configuration_values = jsonencode({
        enableNetworkPolicy = "true"
      })
    }
  }

  eks_managed_node_groups = {
    application = {
      name           = "application"
      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = var.c1_node_instance_types
      capacity_type  = "ON_DEMAND"

      min_size     = var.c1_node_group.min_size
      max_size     = var.c1_node_group.max_size
      desired_size = var.c1_node_group.desired_size

      disk_size = 30

      labels = {
        "lab.openai.com/cluster" = "c1"
      }
    }
  }

  node_security_group_additional_rules = {
    ingress_from_public_alb = {
      description              = "Application traffic from the public ALB"
      protocol                 = "tcp"
      from_port                = 1024
      to_port                  = 65535
      type                     = "ingress"
      source_security_group_id = aws_security_group.c1_alb.id
    }
  }

  tags = {
    Cluster = "c1"
  }
}

module "c2_eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "21.24.0"

  providers = {
    aws = aws.c2
  }

  name               = local.c2_cluster_name
  kubernetes_version = var.kubernetes_version

  endpoint_private_access      = true
  endpoint_public_access       = true
  endpoint_public_access_cidrs = [var.operator_cidr]

  authentication_mode                      = "API_AND_CONFIG_MAP"
  enable_cluster_creator_admin_permissions = true
  enable_irsa                              = true

  enabled_log_types                      = ["api", "audit", "authenticator", "controllerManager", "scheduler"]
  create_cloudwatch_log_group            = true
  cloudwatch_log_group_retention_in_days = 7

  vpc_id     = module.c2_vpc.vpc_id
  subnet_ids = module.c2_vpc.private_subnets

  addons = {
    coredns = {
      most_recent = true
    }
    kube-proxy = {
      most_recent = true
    }
    vpc-cni = {
      before_compute = true
      most_recent    = true
      configuration_values = jsonencode({
        enableNetworkPolicy = "true"
      })
    }
  }

  eks_managed_node_groups = {
    application = {
      name           = "application"
      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = var.c2_node_instance_types
      capacity_type  = "ON_DEMAND"

      min_size     = var.c2_node_group.min_size
      max_size     = var.c2_node_group.max_size
      desired_size = var.c2_node_group.desired_size

      disk_size = 30

      labels = {
        "lab.openai.com/cluster" = "c2"
      }
    }
  }

  node_security_group_additional_rules = {
    ingress_from_cart_nlb = {
      description              = "Cart gRPC traffic from the internal NLB"
      protocol                 = "tcp"
      from_port                = 7070
      to_port                  = 7070
      type                     = "ingress"
      source_security_group_id = aws_security_group.c2_cart_nlb.id
    }
  }

  tags = {
    Cluster = "c2"
  }
}

module "c1_load_balancer_controller_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts"
  version = "6.6.1"

  providers = {
    aws = aws.c1
  }

  name                                   = "${var.project_name}-c1-lbc"
  attach_load_balancer_controller_policy = true

  oidc_providers = {
    c1 = {
      provider_arn               = module.c1_eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:aws-load-balancer-controller"]
    }
  }
}

module "c2_load_balancer_controller_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts"
  version = "6.6.1"

  providers = {
    aws = aws.c2
  }

  name                                   = "${var.project_name}-c2-lbc"
  attach_load_balancer_controller_policy = true

  oidc_providers = {
    c2 = {
      provider_arn               = module.c2_eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:aws-load-balancer-controller"]
    }
  }
}
