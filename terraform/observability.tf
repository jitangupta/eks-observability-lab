resource "aws_cloudwatch_log_group" "c1_flow_logs" {
  provider = aws.c1

  name              = "/aws/vpc/${var.project_name}/c1/reject"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "c2_flow_logs" {
  provider = aws.c2

  name              = "/aws/vpc/${var.project_name}/c2/reject"
  retention_in_days = 7
}

data "aws_iam_policy_document" "flow_logs_assume_role" {
  provider = aws.c1

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "flow_logs" {
  provider = aws.c1

  name               = "${var.project_name}-vpc-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume_role.json
}

data "aws_iam_policy_document" "flow_logs" {
  provider = aws.c1

  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
    ]
    resources = [
      "${aws_cloudwatch_log_group.c1_flow_logs.arn}:*",
      "${aws_cloudwatch_log_group.c2_flow_logs.arn}:*",
    ]
  }
}

resource "aws_iam_role_policy" "flow_logs" {
  provider = aws.c1

  name   = "write-vpc-flow-logs"
  role   = aws_iam_role.flow_logs.id
  policy = data.aws_iam_policy_document.flow_logs.json
}

resource "aws_flow_log" "c1" {
  provider = aws.c1

  iam_role_arn             = aws_iam_role.flow_logs.arn
  log_destination          = aws_cloudwatch_log_group.c1_flow_logs.arn
  log_destination_type     = "cloud-watch-logs"
  traffic_type             = "REJECT"
  vpc_id                   = module.c1_vpc.vpc_id
  max_aggregation_interval = 60

  depends_on = [aws_iam_role_policy.flow_logs]
}

resource "aws_flow_log" "c2" {
  provider = aws.c2

  iam_role_arn             = aws_iam_role.flow_logs.arn
  log_destination          = aws_cloudwatch_log_group.c2_flow_logs.arn
  log_destination_type     = "cloud-watch-logs"
  traffic_type             = "REJECT"
  vpc_id                   = module.c2_vpc.vpc_id
  max_aggregation_interval = 60

  depends_on = [aws_iam_role_policy.flow_logs]
}
