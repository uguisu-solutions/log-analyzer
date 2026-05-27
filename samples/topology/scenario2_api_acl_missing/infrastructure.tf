# scenario2_api_acl_missing 用のサンプル Terraform 定義。
# UI の「Terraform 一括取込」ボタンからアップロードして、各ノードに
# configs を一括割当できる動作確認に使う。
#
# resource ラベルがノード id (fw-01 / lb-01 / web-01 / api-01) と
# `-` ↔ `_` 正規化で一致するように命名している。

# ─── FW: ACL コメントアウト漏れの本丸 ─────────────────
resource "aws_security_group" "fw_01" {
  name        = "fw-01"
  description = "Edge firewall (formerly ASA-style ACL)"

  ingress {
    description = "LB → web-01 (permit)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["10.1.1.10/32"]
  }

  # NOTE (2026-05-25, ops02):
  #   一時的に api-backends 宛て permit ルールを無効化した（インシデント調査用）。
  #   調査完了後に必ず復旧すること。本日中に元に戻す予定。
  #
  # ingress {
  #   description = "LB → api-01 (permit) — TEMP DISABLED"
  #   from_port   = 443
  #   to_port     = 443
  #   protocol    = "tcp"
  #   cidr_blocks = ["10.1.1.10/32"]
  # }

  egress {
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

# ─── LB: HAProxy 相当 (定義のみ、稼働は別途) ─────────
resource "aws_lb" "lb_01" {
  name               = "lb-01"
  load_balancer_type = "application"
  internal           = true
  subnets            = ["subnet-internal-a", "subnet-internal-b"]
  tags = {
    Name = "lb-01"
    Role = "LB"
  }
}

resource "aws_lb_target_group" "web_pool" {
  name     = "web-pool"
  port     = 443
  protocol = "HTTPS"
  health_check {
    interval = 5
    path     = "/healthz"
  }
}

resource "aws_lb_target_group" "api_pool" {
  name     = "api-pool"
  port     = 443
  protocol = "HTTPS"
  health_check {
    interval = 5
    path     = "/healthz"
  }
}

# ─── Web Server (健全側) ────────────────────────────
resource "aws_instance" "web_01" {
  ami           = "ami-0123456789abcdef0"
  instance_type = "t3.medium"
  subnet_id     = "subnet-internal-a"

  vpc_security_group_ids = [aws_security_group.fw_01.id]

  tags = {
    Name = "web-01"
    Role = "Server"
    App  = "nginx"
  }
}

# ─── API Server (FW で塞がれている側) ────────────────
resource "aws_instance" "api_01" {
  ami           = "ami-0123456789abcdef0"
  instance_type = "t3.medium"
  subnet_id     = "subnet-internal-b"

  vpc_security_group_ids = [aws_security_group.fw_01.id]

  tags = {
    Name = "api-01"
    Role = "Server"
    App  = "gunicorn"
  }
}
