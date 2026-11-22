# ---------------------------------------------------------------------------
# App identity (one GSA, bound to the KSA in each environment namespace).
# ---------------------------------------------------------------------------
resource "google_service_account" "app" {
  account_id   = var.app
  display_name = "${var.app} (GKE workload identity)"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "app_datastore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = google_service_account.app.member
}

resource "google_service_account_iam_member" "app_workload_identity" {
  for_each           = toset(var.environments)
  service_account_id = google_service_account.app.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${each.key}/${var.app}]"
  depends_on         = [google_container_cluster.delivery]
}

# Cloud Deploy's control plane agent needs to act as the execution SA.
resource "google_service_account_iam_member" "deploy_actas_self" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-clouddeploy.iam.gserviceaccount.com"
  depends_on         = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# CI identity (GitHub Actions via WIF): build, push, SIGN, ATTEST, create release.
# ---------------------------------------------------------------------------
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  attribute_condition = "assertion.repository == \"${var.github_repo}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "ci" {
  account_id   = "sa-github-ci"
  display_name = "GitHub Actions CI (build, sign, attest, release)"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "ci_roles" {
  for_each = toset([
    "roles/artifactregistry.writer",
    "roles/binaryauthorization.attestorsViewer",
    "roles/containeranalysis.notes.attacher",
    "roles/containeranalysis.notes.occurrences.viewer", # list the attestation it just created
    "roles/containeranalysis.occurrences.editor",
    "roles/clouddeploy.releaser",
  ])
  project = var.project_id
  role    = each.value
  member  = google_service_account.ci.member
}

# `gcloud deploy releases create` uploads the source to the Cloud Deploy staging bucket
# (created by Cloud Deploy, random name) and needs storage.buckets.get on it. A custom
# role keeps that narrower than roles/storage.admin.
resource "google_project_iam_custom_role" "release_storage" {
  role_id     = "deployReleaseStorage"
  title       = "Cloud Deploy release source upload"
  description = "Read bucket metadata and manage objects in the Cloud Deploy staging bucket"
  permissions = [
    "storage.buckets.list", # gcloud looks the staging bucket up by listing
    "storage.buckets.get",
    "storage.buckets.create",
    "storage.objects.create",
    "storage.objects.get",
    "storage.objects.list",
    "storage.objects.delete",
  ]
}

resource "google_project_iam_member" "ci_release_storage" {
  project = var.project_id
  role    = google_project_iam_custom_role.release_storage.id
  member  = google_service_account.ci.member
}

resource "google_kms_crypto_key_iam_member" "ci_signer" {
  crypto_key_id = google_kms_crypto_key.attestor.id
  role          = "roles/cloudkms.signerVerifier"
  member        = google_service_account.ci.member
}

# creating a release requires actAs on the execution service account
resource "google_service_account_iam_member" "ci_actas_deploy" {
  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.ci.member
}

resource "google_service_account_iam_member" "ci_wif" {
  service_account_id = google_service_account.ci.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}

# ---------------------------------------------------------------------------
# Bootstrap build identity for `gcloud builds submit` (first image before CI exists).
# ---------------------------------------------------------------------------
resource "google_service_account" "build" {
  account_id   = "sa-build"
  display_name = "Cloud Build (bootstrap image build)"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "build_roles" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/artifactregistry.writer",
    "roles/storage.objectViewer",
  ])
  project = var.project_id
  role    = each.value
  member  = google_service_account.build.member
}

# ---------------------------------------------------------------------------
# Postmortem tool identity (runs as a Cloud Run job or locally with ADC).
# ---------------------------------------------------------------------------
resource "google_service_account" "postmortem" {
  account_id   = "sa-postmortem"
  display_name = "postmortem tool (BigQuery + Monitoring + Vertex AI)"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "postmortem_roles" {
  for_each = toset([
    "roles/bigquery.jobUser",
    "roles/bigquery.dataViewer",
    "roles/monitoring.viewer",
    "roles/aiplatform.user",
    "roles/logging.viewer",
  ])
  project = var.project_id
  role    = each.value
  member  = google_service_account.postmortem.member
}
