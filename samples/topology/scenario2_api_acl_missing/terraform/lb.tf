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
