output "cluster_name" {
  value = google_container_cluster.delivery.name
}

output "get_credentials" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.delivery.name} --zone ${var.zone} --project ${var.project_id}"
}

output "artifact_registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.apps.repository_id}"
}

output "attestor" {
  value = google_binary_authorization_attestor.ci.name
}

output "kms_key_version" {
  description = "Key version used by `gcloud beta container binauthz attestations sign-and-create`."
  value       = data.google_kms_crypto_key_version.attestor.id
}

output "pipeline" {
  value = google_clouddeploy_delivery_pipeline.pipeline.name
}

output "deploy_service_account" {
  value = google_service_account.deploy.email
}

output "ci_service_account" {
  value = google_service_account.ci.email
}

output "postmortem_service_account" {
  value = google_service_account.postmortem.email
}

output "workload_identity_provider" {
  description = "Set as GitHub Actions secret GCP_WORKLOAD_IDENTITY_PROVIDER."
  value       = google_iam_workload_identity_pool_provider.github.name
}

output "bigquery_dataset" {
  value = google_bigquery_dataset.incidents.dataset_id
}

output "pipeline_url" {
  value = "https://console.cloud.google.com/deploy/delivery-pipelines/${var.region}/${google_clouddeploy_delivery_pipeline.pipeline.name}?project=${var.project_id}"
}

output "slo_url" {
  value = "https://console.cloud.google.com/monitoring/services/${data.google_project.this.number}/${google_monitoring_custom_service.prod.service_id}?project=${var.project_id}"
}

output "dashboard_url" {
  value = "https://console.cloud.google.com/monitoring/dashboards/builder/${element(split("/", google_monitoring_dashboard.app.id), length(split("/", google_monitoring_dashboard.app.id)) - 1)}?project=${var.project_id}"
}
