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
