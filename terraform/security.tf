resource "aws_security_group" "c1_alb" {
  provider = aws.c1

  name_prefix = "${var.project_name}-c1-alb-"
  description = "Public ALB for Online Boutique; the only public application path"
  vpc_id      = module.c1_vpc.vpc_id

  tags = {
    Name = "${var.project_name}-c1-alb"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "c1_alb_http" {
  provider = aws.c1
  for_each = toset(var.alb_ingress_cidrs)

  security_group_id = aws_security_group.c1_alb.id
  description       = "HTTP demo ingress"
  cidr_ipv4         = each.value
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "c1_alb_https" {
  provider = aws.c1
  for_each = toset(var.alb_ingress_cidrs)

  security_group_id = aws_security_group.c1_alb.id
  description       = "HTTPS ingress when ACM is configured"
  cidr_ipv4         = each.value
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "c1_alb_to_vpc" {
  provider = aws.c1

  security_group_id = aws_security_group.c1_alb.id
  description       = "ALB traffic to private targets"
  cidr_ipv4         = var.c1_vpc_cidr
  ip_protocol       = "-1"
}

resource "aws_security_group" "c2_cart_nlb" {
  provider = aws.c2

  name_prefix = "${var.project_name}-c2-cart-nlb-"
  description = "Internal cart NLB; gRPC is accepted only from C1"
  vpc_id      = module.c2_vpc.vpc_id

  tags = {
    Name = "${var.project_name}-c2-cart-nlb"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "c2_cart_from_c1" {
  provider = aws.c2

  security_group_id = aws_security_group.c2_cart_nlb.id
  description       = "Cart gRPC from C1 private addresses"
  cidr_ipv4         = var.c1_vpc_cidr
  from_port         = 7070
  to_port           = 7070
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "c2_cart_to_targets" {
  provider = aws.c2

  security_group_id = aws_security_group.c2_cart_nlb.id
  description       = "Cart gRPC to C2 targets"
  cidr_ipv4         = var.c2_vpc_cidr
  from_port         = 7070
  to_port           = 7070
  ip_protocol       = "tcp"
}

resource "aws_wafv2_web_acl" "c1" {
  provider = aws.c1

  name        = "${var.project_name}-c1"
  description = "Managed protection and per-IP rate limiting for the public ALB"
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "aws-common-rule-set"
    priority = 10

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-common-rules"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "per-ip-rate-limit"
    priority = 20

    action {
      block {}
    }

    statement {
      rate_based_statement {
        aggregate_key_type = "IP"
        limit              = var.waf_rate_limit
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.project_name}-c1-web-acl"
    sampled_requests_enabled   = true
  }
}

resource "aws_cloudwatch_log_group" "waf" {
  provider = aws.c1

  name              = "aws-waf-logs-${var.project_name}-c1"
  retention_in_days = 7
}

resource "aws_wafv2_web_acl_logging_configuration" "c1" {
  provider = aws.c1

  resource_arn            = aws_wafv2_web_acl.c1.arn
  log_destination_configs = [aws_cloudwatch_log_group.waf.arn]
}
