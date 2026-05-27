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
