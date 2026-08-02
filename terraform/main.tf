locals {
  c1_cluster_name = "${var.project_name}-c1"
  c2_cluster_name = "${var.project_name}-c2"

  c1_azs = slice(data.aws_availability_zones.c1.names, 0, 2)
  c2_azs = slice(data.aws_availability_zones.c2.names, 0, 2)

  common_tags = merge(
    {
      Project     = var.project_name
      Environment = "lab"
      ManagedBy   = "Terraform"
    },
    var.tags
  )
}
