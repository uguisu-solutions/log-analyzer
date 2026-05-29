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
