#!/usr/bin/env bash
# Rebuilding after ./teardown.sh: three things survive a destroy and make `terraform apply` 409.
#   * KMS key ring + key (key material cannot be deleted; the version is only scheduled for destruction)
#   * the Workload Identity pool/provider (soft-deleted for 30 days)
#   * the Firestore database (deletion is disabled on purpose; the data is harmless)
# This restores/undeletes them and imports them into the state, then ./deploy.sh runs clean.
set -uo pipefail
cd "$(dirname "$0")"
PROJECT_ID="$(grep -E '^project_id' terraform/terraform.tfvars | cut -d'"' -f2)"
REGION="$(grep -E '^region' terraform/terraform.tfvars | cut -d'"' -f2)"
RING="delivery-signing"; KEY="attestor-key"

echo "== KMS: restore + enable the signing key version(s)"
for v in $(gcloud kms keys versions list --key "$KEY" --keyring "$RING" --location "$REGION" --project "$PROJECT_ID" --filter='state=DESTROY_SCHEDULED' --format='value(name.basename())'); do
  gcloud kms keys versions restore "$v" --key "$KEY" --keyring "$RING" --location "$REGION" --project "$PROJECT_ID" --quiet && \
  gcloud kms keys versions enable "$v" --key "$KEY" --keyring "$RING" --location "$REGION" --project "$PROJECT_ID" --quiet
done
if ! gcloud kms keys versions list --key "$KEY" --keyring "$RING" --location "$REGION" --project "$PROJECT_ID" --filter='state=ENABLED' --format='value(name)' | grep -q .; then
  echo "   no enabled version: creating one"; gcloud kms keys versions create --key "$KEY" --keyring "$RING" --location "$REGION" --project "$PROJECT_ID" --primary 2>/dev/null || true
fi

echo "== WIF: undelete pool + provider"
gcloud iam workload-identity-pools undelete github --location=global --project "$PROJECT_ID" --quiet 2>&1 | tail -1 || true
gcloud iam workload-identity-pools providers undelete github-oidc --workload-identity-pool=github --location=global --project "$PROJECT_ID" --quiet 2>&1 | tail -1 || true

echo "== terraform import"
terraform -chdir=terraform init -input=false >/dev/null
imp() { terraform -chdir=terraform state show "$1" >/dev/null 2>&1 && { echo "   $1 already in state"; return; }
        terraform -chdir=terraform import -input=false "$1" "$2" >/dev/null 2>&1 && echo "   imported $1" || echo "   (skip) $1"; }
imp google_kms_key_ring.signing "projects/${PROJECT_ID}/locations/${REGION}/keyRings/${RING}"
imp google_kms_crypto_key.attestor "projects/${PROJECT_ID}/locations/${REGION}/keyRings/${RING}/cryptoKeys/${KEY}"
imp google_iam_workload_identity_pool.github "projects/${PROJECT_ID}/locations/global/workloadIdentityPools/github"
imp google_iam_workload_identity_pool_provider.github "projects/${PROJECT_ID}/locations/global/workloadIdentityPools/github/providers/github-oidc"
imp google_firestore_database.orders "projects/${PROJECT_ID}/databases/(default)"
echo "done; now ./deploy.sh"
