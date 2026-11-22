# SLOs on the prod namespace; the canary verify job reads the same SLI through
# the Cloud Monitoring PromQL API, so "verify" and "SLO" cannot disagree.
resource "google_monitoring_notification_channel" "email" {
  display_name = "orders-api SLO alerts"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
  depends_on = [google_project_service.apis]
}

resource "google_monitoring_custom_service" "prod" {
  service_id   = "${var.app}-prod"
  display_name = "${var.app} (prod)"
  depends_on   = [google_project_service.apis]
}

locals {
  prom_filter  = "resource.type=\"prometheus_target\" resource.labels.namespace=\"prod\""
  requests_cnt = "metric.type=\"prometheus.googleapis.com/http_requests_total/counter\" ${local.prom_filter} metric.labels.route=\"/orders\""
  requests_ok  = "${local.requests_cnt} metric.labels.code!=monitoring.regex.full_match(\"5..\")"
  latency_hist = "metric.type=\"prometheus.googleapis.com/http_request_duration_seconds/histogram\" ${local.prom_filter} metric.labels.route=\"/orders\""
}

resource "google_monitoring_slo" "availability" {
  service             = google_monitoring_custom_service.prod.service_id
  slo_id              = "orders-availability"
  display_name        = "99.5% of /orders non-5xx (prod)"
  goal                = 0.995
  rolling_period_days = 28

  request_based_sli {
    good_total_ratio {
      good_service_filter  = local.requests_ok
      total_service_filter = local.requests_cnt
    }
  }
}

resource "google_monitoring_slo" "latency" {
  service             = google_monitoring_custom_service.prod.service_id
  slo_id              = "orders-latency-p95-300ms"
  display_name        = "95% of /orders under 300 ms (prod)"
  goal                = 0.95
  rolling_period_days = 28

  request_based_sli {
    distribution_cut {
      distribution_filter = local.latency_hist
      range {
        min = 0
        max = 0.3
      }
    }
  }
}

resource "google_monitoring_alert_policy" "burn" {
  for_each = {
    availability = google_monitoring_slo.availability.name
    latency      = google_monitoring_slo.latency.name
  }
  display_name = "${var.app} prod ${each.key} SLO burn rate"
  combiner     = "OR"

  conditions {
    display_name = "fast burn (1h > 14.4x)"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${each.value}\", \"3600s\")"
      comparison      = "COMPARISON_GT"
      threshold_value = 14.4
      duration        = "0s"
    }
  }
  conditions {
    display_name = "slow burn (6h > 6x)"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${each.value}\", \"21600s\")"
      comparison      = "COMPARISON_GT"
      threshold_value = 6
      duration        = "0s"
    }
  }

  notification_channels = [google_monitoring_notification_channel.email.id]
  documentation {
    content   = "orders-api prod ${each.key} SLO is burning budget. Check the active Cloud Deploy rollout (canary phase, verify job), Chaos Mesh experiments in flight, and faults_injected_total. Run postmortem/postmortem.py for the timeline."
    mime_type = "text/markdown"
  }
}

locals {
  panels = [
    { title = "Requests/s by code (prod /orders)", q = "sum by (code) (rate(http_requests_total{namespace=\"prod\",route=\"/orders\"}[2m]))", type = "STACKED_AREA" },
    { title = "p95 latency by namespace (s)", q = "histogram_quantile(0.95, sum by (namespace, le) (rate(http_request_duration_seconds_bucket{route=\"/orders\"}[2m])))", type = "LINE" },
    { title = "5xx ratio by pod (canary vs stable)", q = "sum by (pod) (rate(http_requests_total{namespace=\"prod\",code=~\"5..\"}[2m])) / sum by (pod) (rate(http_requests_total{namespace=\"prod\"}[2m]))", type = "LINE" },
    { title = "Faults injected/s (bad-release drill)", q = "sum by (kind, namespace) (rate(faults_injected_total[2m]))", type = "STACKED_AREA" },
    { title = "Ready pods per namespace", q = "count by (namespace) (up{job=\"orders-api\"})", type = "LINE" },
    { title = "Orders created/s", q = "sum by (namespace) (rate(orders_created_total[2m]))", type = "LINE" },
  ]
}

resource "google_monitoring_dashboard" "app" {
  dashboard_json = jsonencode({
    displayName = "orders-api delivery"
    mosaicLayout = {
      columns = 12
      tiles = [
        for i, p in local.panels : {
          xPos   = (i % 3) * 4
          yPos   = floor(i / 3) * 4
          width  = 4
          height = 4
          widget = {
            title   = p.title
            xyChart = { dataSets = [{ timeSeriesQuery = { prometheusQuery = p.q }, plotType = p.type }] }
          }
        }
      ]
    }
  })
}
