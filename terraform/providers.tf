provider "aws" {
  alias   = "c1"
  region  = var.c1_region
  profile = var.aws_profile

  default_tags {
    tags = local.common_tags
  }
}

provider "aws" {
  alias   = "c2"
  region  = var.c2_region
  profile = var.aws_profile

  default_tags {
    tags = local.common_tags
  }
}

data "aws_availability_zones" "c1" {
  provider = aws.c1
  state    = "available"
}

data "aws_availability_zones" "c2" {
  provider = aws.c2
  state    = "available"
}

data "aws_caller_identity" "current" {
  provider = aws.c1
}
