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
