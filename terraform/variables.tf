variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "zone" {
  description = "Zonal cluster: covered by the GKE free tier (no management fee)."
  type        = string
  default     = "us-central1-a"
}

variable "cluster_name" {
  type    = string
  default = "delivery"
}

variable "authorized_cidr" {
  description = "CIDR allowed to reach the GKE control plane (your public IP/32, or your ISP's block behind CGNAT)."
  type        = string
}

variable "github_repo" {
  description = "GitHub repository (owner/name) allowed to use Workload Identity Federation for CI."
  type        = string
  default     = "bhavishyajain545/gcp-progressive-delivery-sre"
}

variable "alert_email" {
  type = string
}

variable "machine_type" {
  type    = string
  default = "e2-standard-2"
}

variable "min_nodes" {
  type    = number
  default = 2
}

variable "max_nodes" {
  type    = number
  default = 4
}

variable "app" {
  type    = string
  default = "orders-api"
}

variable "environments" {
  description = "Cloud Deploy targets, in promotion order; each is a namespace on the cluster."
  type        = list(string)
  default     = ["dev", "staging", "prod"]
}
