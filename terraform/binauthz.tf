# Supply chain: CI signs the image digest with a Cloud KMS asymmetric key and
# records the signature as a Binary Authorization attestation. The cluster
# policy admits only images that carry an attestation from this attestor.
# An unsigned image (even one in our own registry) is rejected at admission.

resource "google_kms_key_ring" "signing" {
  name       = "${var.cluster_name}-signing"
  location   = var.region
  depends_on = [google_project_service.apis]
}

resource "google_kms_crypto_key" "attestor" {
  name     = "attestor-key"
  key_ring = google_kms_key_ring.signing.id
  purpose  = "ASYMMETRIC_SIGN"

  version_template {
    algorithm        = "EC_SIGN_P256_SHA256"
    protection_level = "SOFTWARE"
  }

  lifecycle {
    prevent_destroy = false
  }
}

data "google_kms_crypto_key_version" "attestor" {
  crypto_key = google_kms_crypto_key.attestor.id
}

resource "google_container_analysis_note" "attestor" {
  name = "${var.cluster_name}-attestor-note"
  attestation_authority {
    hint {
      human_readable_name = "Built, tested and signed by the ${var.app} pipeline"
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_binary_authorization_attestor" "ci" {
  name = "${var.cluster_name}-ci-attestor"
  attestation_authority_note {
    note_reference = google_container_analysis_note.attestor.name
    public_keys {
      id = data.google_kms_crypto_key_version.attestor.id
      pkix_public_key {
        public_key_pem      = data.google_kms_crypto_key_version.attestor.public_key[0].pem
        signature_algorithm = data.google_kms_crypto_key_version.attestor.public_key[0].algorithm
      }
    }
  }
}

resource "google_binary_authorization_policy" "policy" {
  # Google-managed system images (GKE add-ons) and the Chaos Mesh / Managed Prometheus
  # images stay deployable; everything else needs our attestation. Patterns need a registry
  # domain (Kubernetes normalises "curlimages/curl" to docker.io/curlimages/curl).
  admission_whitelist_patterns {
    name_pattern = "gcr.io/google_containers/*"
  }
  admission_whitelist_patterns {
    name_pattern = "gcr.io/google-containers/*"
  }
  admission_whitelist_patterns {
    name_pattern = "k8s.gcr.io/**"
  }
  admission_whitelist_patterns {
    name_pattern = "registry.k8s.io/**"
  }
  admission_whitelist_patterns {
    name_pattern = "gke.gcr.io/**"
  }
  admission_whitelist_patterns {
    name_pattern = "gcr.io/stackdriver-agents/*"
  }
  admission_whitelist_patterns {
    name_pattern = "gcr.io/gke-release/**"
  }
  admission_whitelist_patterns {
    name_pattern = "gcr.io/gke-release-staging/**"
  }
  admission_whitelist_patterns {
    name_pattern = "gcr.io/k8s-stackdriver/*"
  }
  admission_whitelist_patterns {
    name_pattern = "ghcr.io/chaos-mesh/**"
  }
  admission_whitelist_patterns {
    name_pattern = "docker.io/grafana/k6*"
  }
  admission_whitelist_patterns {
    name_pattern = "docker.io/curlimages/curl*"
  }
  admission_whitelist_patterns {
    name_pattern = "gcr.io/cloud-builders/*" # verify Job image (gcr.io/cloud-builders/curl)
  }

  global_policy_evaluation_mode = "ENABLE"

  default_admission_rule {
    evaluation_mode         = "REQUIRE_ATTESTATION"
    enforcement_mode        = "ENFORCED_BLOCK_AND_AUDIT_LOG"
    require_attestations_by = [google_binary_authorization_attestor.ci.name]
  }
}
