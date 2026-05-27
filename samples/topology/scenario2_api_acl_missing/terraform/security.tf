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
