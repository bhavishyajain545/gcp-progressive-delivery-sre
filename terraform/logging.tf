# Learning loop: the logs that explain an incident (app, Cloud Deploy, Binary
# Authorization admission decisions, GKE events, Chaos Mesh) are routed to a
# BigQuery dataset the postmortem tool queries by time window.
resource "google_bigquery_dataset" "incidents" {
  dataset_id                 = "incidents"
  location                   = "US"
  description                = "Log sink destination: everything an incident timeline needs"
  delete_contents_on_destroy = true
  depends_on                 = [google_project_service.apis]
}

resource "google_logging_project_sink" "incidents" {
  name        = "incidents-to-bigquery"
  destination = "bigquery.googleapis.com/${google_bigquery_dataset.incidents.id}"
  filter      = <<-EOT
    (resource.type="k8s_container" AND resource.labels.namespace_name=("prod" OR "staging" OR "dev" OR "chaos-mesh"))
    OR resource.type="k8s_pod"
    OR resource.type="clouddeploy.googleapis.com/DeliveryPipeline"
    OR resource.type="clouddeploy.googleapis.com/Target"
    OR protoPayload.serviceName="clouddeploy.googleapis.com"
    OR protoPayload.serviceName="binaryauthorization.googleapis.com"
    OR (resource.type="k8s_cluster" AND protoPayload.methodName:"admission")
    OR logName:"events"
  EOT

  unique_writer_identity = true
  bigquery_options {
    use_partitioned_tables = true
  }
}

resource "google_bigquery_dataset_iam_member" "sink_writer" {
  dataset_id = google_bigquery_dataset.incidents.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.incidents.writer_identity
}
