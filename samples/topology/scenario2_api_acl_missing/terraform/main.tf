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
