output "account_id" {
  description = "AWS account containing the lab."
  value       = data.aws_caller_identity.current.account_id
}

output "clusters" {
  description = "Identifiers needed to configure kubectl and later Kubernetes work."
  value = {
    c1 = {
      name                      = module.c1_eks.cluster_name
      region                    = var.c1_region
      endpoint                  = module.c1_eks.cluster_endpoint
      oidc_provider_arn         = module.c1_eks.oidc_provider_arn
      node_security_group_id    = module.c1_eks.node_security_group_id
      cluster_security_group_id = module.c1_eks.cluster_security_group_id
    }
    c2 = {
      name                      = module.c2_eks.cluster_name
      region                    = var.c2_region
      endpoint                  = module.c2_eks.cluster_endpoint
      oidc_provider_arn         = module.c2_eks.oidc_provider_arn
      node_security_group_id    = module.c2_eks.node_security_group_id
      cluster_security_group_id = module.c2_eks.cluster_security_group_id
    }
  }
}
output "networking" {
  description = "VPC, subnet, routing, and NACL identifiers used by deployment and verification."
  value = {
    c1 = {
      vpc_id                  = module.c1_vpc.vpc_id
      cidr                    = var.c1_vpc_cidr
      private_subnet_ids      = module.c1_vpc.private_subnets
      public_subnet_ids       = module.c1_vpc.public_subnets
      private_route_table_ids = module.c1_vpc.private_route_table_ids
      nat_gateway_ids         = module.c1_vpc.natgw_ids
    }
    c2 = {
      vpc_id                  = module.c2_vpc.vpc_id
      cidr                    = var.c2_vpc_cidr
      private_subnet_ids      = module.c2_vpc.private_subnets
      public_subnet_ids       = module.c2_vpc.public_subnets
      private_route_table_ids = module.c2_vpc.private_route_table_ids
      nat_gateway_ids         = module.c2_vpc.natgw_ids
      private_nacl_ids        = [aws_network_acl.c2_private.id]
    }
    peering_connection_id = aws_vpc_peering_connection.c1_to_c2.id
  }
}

output "security" {
  description = "Security resources consumed by Kubernetes manifests and the verifier."
  value = {
    c1_alb_security_group_id      = aws_security_group.c1_alb.id
    c2_cart_nlb_security_group_id = aws_security_group.c2_cart_nlb.id
    waf_web_acl_arn               = aws_wafv2_web_acl.c1.arn
    fault_nacl_rule_number        = 50
  }
}

output "iam_roles" {
  description = "IRSA roles to annotate controller service accounts with."
  value = {
    c1_load_balancer_controller = module.c1_load_balancer_controller_irsa.arn
    c2_load_balancer_controller = module.c2_load_balancer_controller_irsa.arn
    vpc_flow_logs               = aws_iam_role.flow_logs.arn
  }
}

output "log_groups" {
  description = "CloudWatch log groups used during incident investigation."
  value = {
    c1_vpc_rejects = aws_cloudwatch_log_group.c1_flow_logs.name
    c2_vpc_rejects = aws_cloudwatch_log_group.c2_flow_logs.name
    c1_waf         = aws_cloudwatch_log_group.waf.name
  }
}

output "kubeconfig_commands" {
  description = "Commands that create distinct local kubeconfig contexts after apply."
  value = {
    c1 = "aws eks update-kubeconfig --region ${var.c1_region} --name ${module.c1_eks.cluster_name} --alias ${local.c1_cluster_name}"
    c2 = "aws eks update-kubeconfig --region ${var.c2_region} --name ${module.c2_eks.cluster_name} --alias ${local.c2_cluster_name}"
  }
}
