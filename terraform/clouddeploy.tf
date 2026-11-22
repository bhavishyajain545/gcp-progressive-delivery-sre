# Cloud Deploy: one delivery pipeline, three targets on the same cluster
# (namespaces dev / staging / prod via Skaffold profiles). dev and staging use
# the standard strategy with a verify job; prod is a canary (25% → 50% → 100%)
# on Gateway API traffic splitting, verified at each phase. An automation
# promotes dev → staging on success, advances canary phases, and rolls prod
# back when a verify job fails.

resource "google_service_account" "deploy" {
  account_id   = "sa-deploy"
  display_name = "Cloud Deploy execution (render, deploy, verify)"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "deploy_roles" {
  for_each = toset([
    "roles/clouddeploy.jobRunner",
    "roles/container.developer",
    "roles/logging.logWriter",
    "roles/monitoring.viewer", # verify job reads the SLI from Cloud Monitoring
    "roles/artifactregistry.reader",
    "roles/storage.objectAdmin", # render/verify artifacts in the Cloud Deploy bucket
  ])
  project = var.project_id
  role    = each.value
  member  = google_service_account.deploy.member
}

resource "google_clouddeploy_target" "env" {
  for_each = toset(var.environments)

  name        = each.key
  location    = var.region
  description = "${var.app} on namespace ${each.key}"

  gke {
    cluster = google_container_cluster.delivery.id
  }

  execution_configs {
    usages          = ["RENDER", "DEPLOY", "VERIFY"]
    service_account = google_service_account.deploy.email
  }

  # Target-level deploy parameters, substituted into "# from-param" placeholders after rendering.
  deploy_parameters = {
    "APP_ENV"  = each.key
    "REPLICAS" = each.key == "prod" ? "3" : "2"
  }

  require_approval = false
  depends_on       = [google_project_iam_member.deploy_roles]
}

resource "google_clouddeploy_delivery_pipeline" "pipeline" {
  name        = var.app
  location    = var.region
  description = "${var.app}: dev → staging → prod (canary 25/50/100 with verification)"

  serial_pipeline {
    stages {
      target_id = google_clouddeploy_target.env["dev"].name
      profiles  = ["dev"]
      strategy {
        standard {
          verify = true
        }
      }
    }
    stages {
      target_id = google_clouddeploy_target.env["staging"].name
      profiles  = ["staging"]
      strategy {
        standard {
          verify = true
        }
      }
    }
    stages {
      target_id = google_clouddeploy_target.env["prod"].name
      profiles  = ["prod"]
      strategy {
        canary {
          runtime_config {
            kubernetes {
              gateway_service_mesh {
                http_route             = var.app
                service                = var.app
                deployment             = var.app
                route_update_wait_time = "60s"
              }
            }
          }
          canary_deployment {
            percentages = [25, 50]
            verify      = true
          }
        }
      }
    }
  }
}

# Automation: hands-off promotion through the gates, automatic rollback on a
# failed verify. Advancing between canary phases waits so the verify job and
# the SLO have time to say something.
resource "google_clouddeploy_automation" "flow" {
  name              = "${var.app}-flow"
  location          = var.region
  delivery_pipeline = google_clouddeploy_delivery_pipeline.pipeline.name
  service_account   = google_service_account.deploy.email
  description       = "promote dev→staging→prod, advance canary phases, repair (rollback) failed rollouts"

  selector {
    targets {
      id = "*"
    }
  }

  rules {
    promote_release_rule {
      id   = "promote"
      wait = "60s"
    }
  }

  rules {
    advance_rollout_rule {
      id   = "advance"
      wait = "60s"
    }
  }

  rules {
    repair_rollout_rule {
      id = "rollback-on-failure"
      repair_phases {
        rollback {
          destination_phase = "stable"
        }
      }
    }
  }

  depends_on = [google_project_iam_member.deploy_roles]
}
