output "static_ip" {
  description = "Point the site's A record here."
  value       = aws_lightsail_static_ip.app.ip_address
}

output "ssh" {
  description = "Shell on the instance."
  value       = "ssh ubuntu@${aws_lightsail_static_ip.app.ip_address}"
}

output "dns_records_to_add" {
  description = "Everything to paste into GoDaddy, in one place."
  value       = <<-EOT

    ── At GoDaddy → basivo.in → DNS → Records ──────────────────────────

    1) The site. Until this resolves, Caddy cannot obtain a certificate
       and the instance will serve nothing over HTTPS.

       Type: A      Name: ${split(".", var.site_host)[0]}      Value: ${aws_lightsail_static_ip.app.ip_address}      TTL: 600

    2) Email. Three CNAMEs proving the domain is yours, so SES will send
       as it and receiving servers will not treat the mail as forged.
       Without these, verification emails are never delivered.

       Type: CNAME  Name: ${aws_ses_domain_dkim.app.dkim_tokens[0]}._domainkey  Value: ${aws_ses_domain_dkim.app.dkim_tokens[0]}.dkim.amazonses.com
       Type: CNAME  Name: ${aws_ses_domain_dkim.app.dkim_tokens[1]}._domainkey  Value: ${aws_ses_domain_dkim.app.dkim_tokens[1]}.dkim.amazonses.com
       Type: CNAME  Name: ${aws_ses_domain_dkim.app.dkim_tokens[2]}._domainkey  Value: ${aws_ses_domain_dkim.app.dkim_tokens[2]}.dkim.amazonses.com

    3) Recommended. Tells receiving servers that only SES may send as
       this domain, which is most of what keeps you out of spam folders.

       Type: TXT    Name: @                Value: "v=spf1 include:amazonses.com ~all"
       Type: TXT    Name: _dmarc           Value: "v=DMARC1; p=none; rua=mailto:${var.acme_email}"

    ────────────────────────────────────────────────────────────────────
  EOT
}

output "smtp_host" {
  description = "SES SMTP endpoint for this region."
  value       = "email-smtp.${var.region}.amazonaws.com"
}

output "smtp_username" {
  description = "SES SMTP username."
  value       = aws_iam_access_key.smtp.id
}

output "smtp_password" {
  description = "SES SMTP password. Read it with `terraform output -raw smtp_password`."
  value       = aws_iam_access_key.smtp.ses_smtp_password_v4
  sensitive   = true
}

output "site_url" {
  value = "https://${var.site_host}"
}
