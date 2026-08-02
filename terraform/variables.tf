variable "project_name" {
  description = "Prefix used for resource names and discovery tags."
  type        = string
  default     = "eks-observability-lab"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,31}$", var.project_name))
    error_message = "project_name must be 3-32 lowercase letters, digits, or hyphens and start with a letter."
  }
}

variable "aws_profile" {
  description = "Optional shared AWS CLI profile. Leave null to use the normal AWS credential chain."
  type        = string
  default     = null
  nullable    = true
}

variable "c1_region" {
  description = "AWS region for the user-facing cluster."
  type        = string
  default     = "us-east-1"
}

variable "c2_region" {
  description = "AWS region for the cart cluster."
  type        = string
  default     = "us-west-2"

  validation {
    condition     = var.c2_region != var.c1_region
    error_message = "c1_region and c2_region must be different for this lab."
  }
}

variable "c1_vpc_cidr" {
  description = "CIDR for the C1 VPC."
  type        = string
  default     = "10.10.0.0/16"
}

variable "c2_vpc_cidr" {
  description = "CIDR for the C2 VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "operator_cidr" {
  description = "Public operator IPv4 CIDR allowed to reach both EKS public API endpoints, normally x.x.x.x/32."
  type        = string

  validation {
    condition     = can(cidrnetmask(var.operator_cidr)) && tonumber(split("/", var.operator_cidr)[1]) >= 24
    error_message = "operator_cidr must be a valid, narrow IPv4 CIDR (/24 through /32); use /32 for one workstation."
  }
}

variable "kubernetes_version" {
  description = "EKS Kubernetes minor version."
  type        = string
  default     = "1.35"
}

variable "c1_node_instance_types" {
  description = "EC2 instance types used by the C1 managed node group."
  type        = list(string)
  default     = ["t3a.large"]
}

variable "c2_node_instance_types" {
  description = "EC2 instance types used by the C2 managed node group."
  type        = list(string)
  default     = ["t3a.medium"]
}

variable "c1_node_group" {
  description = "C1 managed node group capacity."
  type = object({
    min_size     = number
    max_size     = number
    desired_size = number
  })
  default = {
    min_size     = 1
    max_size     = 1
    desired_size = 1
  }
}

variable "c2_node_group" {
  description = "C2 managed node group capacity."
  type = object({
    min_size     = number
    max_size     = number
    desired_size = number
  })
  default = {
    min_size     = 1
    max_size     = 1
    desired_size = 1
  }
}

variable "alb_ingress_cidrs" {
  description = "IPv4 CIDRs allowed to reach the public ALB security group."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "waf_rate_limit" {
  description = "Maximum requests per five-minute WAF rate window from one source IP."
  type        = number
  default     = 2000

  validation {
    condition     = var.waf_rate_limit >= 100
    error_message = "waf_rate_limit must be at least 100."
  }
}

variable "tags" {
  description = "Additional tags applied to all resources."
  type        = map(string)
  default     = {}
}
