# scenario2_api_acl_missing — main.tf
# Provider / Terraform 設定とトップレベルの参照だけをここに置く。
# 個別リソースは security.tf / network.tf / compute.tf / lb.tf に分割。

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  backend "s3" {
    bucket = "corp-tfstate-prod"
    key    = "scenario2/terraform.tfstate"
    region = "ap-northeast-1"
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project     = "corp-platform"
      Environment = "production"
      ManagedBy   = "terraform"
    }
  }
}
# scenario2_api_acl_missing — variables.tf

variable "region" {
  description = "AWS region"
  type        = string
  default     = "ap-northeast-1"
}

variable "vpc_cidr" {
  description = "CIDR of the production VPC"
  type        = string
  default     = "10.1.0.0/16"
}

variable "internal_subnets" {
  description = "Subnet CIDRs for the internal tier (LB, backends, DB)"
  type        = map(string)
  default = {
    inside_a = "10.1.1.0/24" # LB tier (lb-01)
    inside_b = "10.1.2.0/24" # backend tier (web-01 / api-01)
    inside_c = "10.1.3.0/24" # data tier  (db-01, 将来用)
  }
}

variable "ami_app" {
  description = "AMI ID used for application servers (web/api)"
  type        = string
  default     = "ami-0123456789abcdef0"
}

variable "instance_type_app" {
  description = "Instance type for application servers"
  type        = string
  default     = "t3.medium"
}

variable "ssl_cert_arn" {
  description = "ACM cert ARN for HTTPS listeners"
  type        = string
  default     = "arn:aws:acm:ap-northeast-1:000000000000:certificate/placeholder"
}
# scenario2_api_acl_missing — network.tf
# VPC とサブネットの定義。ノードへの直接マッピングは無いが、後段の SG / EC2 が
# 参照するため一緒に置いておく。

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags = {
    Name = "corp-vpc"
  }
}

resource "aws_subnet" "inside_a" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.internal_subnets["inside_a"]
  availability_zone = "ap-northeast-1a"
  tags = {
    Name = "subnet-internal-a"
    Tier = "lb"
  }
}

resource "aws_subnet" "inside_b" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.internal_subnets["inside_b"]
  availability_zone = "ap-northeast-1c"
  tags = {
    Name = "subnet-internal-b"
    Tier = "backend"
  }
}

resource "aws_internet_gateway" "egress" {
  vpc_id = aws_vpc.main.id
  tags = {
    Name = "corp-igw"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.egress.id
  }
  tags = {
    Name = "rt-public"
  }
}
# scenario2_api_acl_missing — security.tf
# Security Group / ACL の定義。**このファイルに障害の原因が含まれる**。
#
# 障害ストーリ:
#   2026-05-25 18:30 に ops02 が「インシデント調査用」と称して、LB → api-01 行きの
#   ingress ルールをコメントアウトしたまま元に戻し忘れた。
#   結果として LB → api-01 (10.1.2.21:443) の通信は default deny で落ちている。

# ─── エッジ FW: fw-01 ────────────────────────────────
# LLM が config として読むのはこのブロック (TerraformImporter で fw-01 にマッチ)。
resource "aws_security_group" "fw_01" {
  name        = "fw-01"
  description = "Edge firewall ingress policy (formerly ASA-style ACL)"
  vpc_id      = aws_vpc.main.id

  # LB から web-01 への HTTPS は permit (= 正常通信)
  ingress {
    description = "LB -> web-01 (permit)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["10.1.1.10/32"]
  }

  # NOTE (2026-05-25T18:30+09:00, ops02):
  #   一時的に api-backends 宛て permit ルールを無効化した（インシデント調査用）。
  #   調査完了後に必ず復旧すること。本日中に元に戻す予定。 ← 戻し忘れ
  #
  # ingress {
  #   description = "LB -> api-01 (permit) -- TEMP DISABLED"
  #   from_port   = 443
  #   to_port     = 443
  #   protocol    = "tcp"
  #   cidr_blocks = ["10.1.1.10/32"]
  # }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "fw-01"
    Role = "FW"
  }
}

# ─── LB 側の SG: lb-01 ──────────────────────────────
# LB は web/api の両方に向けて HTTPS を出すが、出口の SG だけ見ても OK。
# 実際の deny は fw_01 (上記) で起きている。
resource "aws_security_group" "lb_01" {
  name        = "lb-01"
  description = "Internal LB egress"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "Outbound to backends"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["10.1.2.0/24"]
  }

  tags = {
    Name = "lb-01"
    Role = "LB"
  }
}
# scenario2_api_acl_missing — compute.tf
# Web / API サーバの EC2 定義。

# ─── Web Server (健全側) ────────────────────────────
resource "aws_instance" "web_01" {
  ami           = var.ami_app
  instance_type = var.instance_type_app
  subnet_id     = aws_subnet.inside_b.id
  private_ip    = "10.1.2.20"

  vpc_security_group_ids = [aws_security_group.fw_01.id]

  user_data = <<-EOT
    #!/bin/bash
    systemctl enable --now nginx
  EOT

  tags = {
    Name = "web-01"
    Role = "Server"
    App  = "nginx"
  }
}

# ─── API Server (FW で塞がれている側) ────────────────
resource "aws_instance" "api_01" {
  ami           = var.ami_app
  instance_type = var.instance_type_app
  subnet_id     = aws_subnet.inside_b.id
  private_ip    = "10.1.2.21"

  vpc_security_group_ids = [aws_security_group.fw_01.id]

  user_data = <<-EOT
    #!/bin/bash
    systemctl enable --now api.service
  EOT

  tags = {
    Name = "api-01"
    Role = "Server"
    App  = "gunicorn"
  }
}
# scenario2_api_acl_missing — lb.tf
# 内部 LB (lb-01) と Target Group。実際の HAProxy 設定は lb-01.conf を参照。

resource "aws_lb" "lb_01" {
  name               = "lb-01"
  load_balancer_type = "application"
  internal           = true
  subnets            = [aws_subnet.inside_a.id]
  security_groups    = [aws_security_group.lb_01.id]

  tags = {
    Name = "lb-01"
    Role = "LB"
  }
}

resource "aws_lb_target_group" "web_pool" {
  name        = "web-pool"
  port        = 443
  protocol    = "HTTPS"
  vpc_id      = aws_vpc.main.id
  target_type = "instance"

  health_check {
    enabled             = true
    interval            = 5
    path                = "/healthz"
    matcher             = "200"
    protocol            = "HTTPS"
    timeout             = 3
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_target_group" "api_pool" {
  name        = "api-pool"
  port        = 443
  protocol    = "HTTPS"
  vpc_id      = aws_vpc.main.id
  target_type = "instance"

  health_check {
    enabled             = true
    interval            = 5
    path                = "/healthz"
    matcher             = "200"
    protocol            = "HTTPS"
    timeout             = 3
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_target_group_attachment" "web_01_attach" {
  target_group_arn = aws_lb_target_group.web_pool.arn
  target_id        = aws_instance.web_01.id
  port             = 443
}

resource "aws_lb_target_group_attachment" "api_01_attach" {
  target_group_arn = aws_lb_target_group.api_pool.arn
  target_id        = aws_instance.api_01.id
  port             = 443
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.lb_01.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.ssl_cert_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web_pool.arn
  }
}

resource "aws_lb_listener_rule" "api_path_routing" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api_pool.arn
  }

  condition {
    path_pattern {
      values = ["/api/*"]
    }
  }
}
# scenario2_api_acl_missing — outputs.tf

output "lb_dns_name" {
  description = "Internal LB DNS name"
  value       = aws_lb.lb_01.dns_name
}

output "fw_security_group_id" {
  description = "ID of fw-01 security group (障害発生箇所)"
  value       = aws_security_group.fw_01.id
}

output "backend_instance_ids" {
  description = "EC2 instance IDs for web-01 / api-01"
  value = {
    web_01 = aws_instance.web_01.id
    api_01 = aws_instance.api_01.id
  }
}
