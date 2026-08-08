# One Lightsail instance running the whole stack.
#
# Lightsail rather than EC2 because the price includes the public IPv4 address
# and 2 TB of transfer. The same machine on EC2 is $6.13 of compute plus $3.65
# for the address plus metered egress — $10.58 for less disk.
#
# What this deliberately does not create: no load balancer, no RDS, no
# ElastiCache, no NAT gateway. Each is a real improvement and each costs more
# per month than the entire instance. See deploy/README.md for when to add them.

# Suffixed, because Lightsail resource names share one namespace across types:
# a key pair called "basivo-beta" makes that name unavailable for the instance,
# and the error ("Some names are already in use") does not say which resource
# is holding it.
resource "aws_lightsail_key_pair" "deploy" {
  name       = "basivo-beta-key"
  public_key = file(pathexpand(var.ssh_public_key_path))
}

resource "aws_lightsail_static_ip" "app" {
  name = "basivo-beta-ip"
}

resource "aws_lightsail_instance" "app" {
  name              = "basivo-beta"
  availability_zone = "${var.region}a"
  blueprint_id      = var.blueprint_id
  bundle_id         = var.bundle_id
  key_pair_name     = aws_lightsail_key_pair.deploy.name

  user_data = templatefile("${path.module}/user_data.sh", {
    repo_url   = var.repo_url
    repo_ref   = var.repo_ref
    site_host  = var.site_host
    acme_email = var.acme_email
    # Passed in rather than derived in the script: `$${site_host#*.}` is bash
    # parameter expansion, and templatefile would try to interpolate it as a
    # Terraform expression and fail before the instance ever boots.
    mail_domain = var.domain
  })

  tags = {
    Name = "basivo-beta"
  }
}

resource "aws_lightsail_static_ip_attachment" "app" {
  static_ip_name = aws_lightsail_static_ip.app.name
  instance_name  = aws_lightsail_instance.app.name
}

resource "aws_lightsail_instance_public_ports" "app" {
  instance_name = aws_lightsail_instance.app.name

  # HTTP: needed permanently, not just for the ACME challenge. Caddy redirects
  # it to HTTPS, and closing it would break certificate renewal.
  port_info {
    protocol  = "tcp"
    from_port = 80
    to_port   = 80
    cidrs     = ["0.0.0.0/0"]
  }

  port_info {
    protocol  = "tcp"
    from_port = 443
    to_port   = 443
    cidrs     = ["0.0.0.0/0"]
  }

  port_info {
    protocol  = "tcp"
    from_port = 22
    to_port   = 22
    cidrs     = [var.ssh_allowed_cidr]
  }
}

# ---------------------------------------------------------------------------
# Email
#
# Not optional. Registration issues a verification link, so an account cannot
# be activated until mail actually leaves the building. SES costs $0.10 per
# thousand messages, which at beta volume rounds to nothing.
#
# Two things to know:
#   * DKIM needs three CNAME records at GoDaddy. Until they resolve, SES will
#     not send as this domain at all.
#   * A new SES account is in the sandbox: it will only deliver to addresses
#     you have individually verified. Request production access to lift that;
#     it is usually granted within a day.
# ---------------------------------------------------------------------------

resource "aws_ses_domain_identity" "app" {
  domain = var.domain
}

resource "aws_ses_domain_dkim" "app" {
  domain = aws_ses_domain_identity.app.domain
}

# A dedicated user that can do exactly one thing: send mail as this domain.
# Its credentials live on the instance, so the blast radius of that box being
# compromised is "can send email", not "can touch the account".
resource "aws_iam_user" "smtp" {
  name = "basivo-beta-smtp"
  path = "/service/"
}

resource "aws_iam_user_policy" "smtp" {
  name = "ses-send-only"
  user = aws_iam_user.smtp.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ses:SendRawEmail", "ses:SendEmail"]
      Resource = "*"
      Condition = {
        StringEquals = {
          "ses:FromAddress" = "no-reply@${var.domain}"
        }
      }
    }]
  })
}

resource "aws_iam_access_key" "smtp" {
  user = aws_iam_user.smtp.name
}
