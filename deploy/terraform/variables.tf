variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "profile" {
  description = "AWS CLI profile."
  type        = string
  default     = "default"
}

variable "domain" {
  description = "Apex domain, used for the SES sending identity."
  type        = string
  default     = "basivo.in"
}

variable "site_host" {
  description = "Hostname the application is served on. Needs an A record pointing at the static IP before Caddy can obtain a certificate."
  type        = string
  default     = "beta.basivo.in"
}

variable "acme_email" {
  description = "Contact address for Let's Encrypt. Receives expiry warnings if renewal ever fails."
  type        = string
}

variable "bundle_id" {
  description = <<-EOT
    Lightsail bundle. micro_3_0 is 1 GB / 2 vCPU / 40 GB / 2 TB transfer at
    $7 a month, with the public IPv4 address and bandwidth included — which is
    where the equivalent EC2 setup quietly costs $10.58.

    nano_3_0 ($5) halves the memory to 512 MB. Postgres, Redis, Python and
    Caddy do not fit in that without swapping hard, and the OOM killer tends
    to choose the API.
  EOT
  type        = string
  default     = "micro_3_0"
}

variable "blueprint_id" {
  description = "OS image. Ubuntu 24.04 LTS."
  type        = string
  default     = "ubuntu_24_04"
}

variable "ssh_public_key_path" {
  description = "Public key authorised for SSH on the instance."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "ssh_allowed_cidr" {
  description = <<-EOT
    Who may reach port 22. Defaults to a single address rather than 0.0.0.0/0:
    an SSH port open to the internet collects credential-stuffing traffic
    within minutes of the instance existing.

    Set this to your current address; update it when it changes.
  EOT
  type        = string
}

variable "repo_url" {
  description = "Git repository the instance builds from."
  type        = string
  default     = "https://github.com/mohamedabubasith/basivo-orch.git"
}

variable "repo_ref" {
  description = "Branch or tag to deploy."
  type        = string
  default     = "main"
}
