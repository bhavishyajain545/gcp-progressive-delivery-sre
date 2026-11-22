#!/usr/bin/env bash
# End-to-end bring-up: Terraform -> first image -> sign + attest -> Chaos Mesh -> first release.
#   ./deploy.sh              # full bring-up
#   ./deploy.sh release [TAG] [FAIL_RATE]   # build+sign+attest+release again from here (default TAG=v<N>)
#   ./deploy.sh slo          # second terraform apply once metrics exist (SLOs, alerts)
set -euo pipefail
cd "$(dirname "$0")"
export USE_GKE_GCLOUD_AUTH_PLUGIN=True

PROJECT_ID="$(grep -E '^project_id' terraform/terraform.tfvars | cut -d'"' -f2)"
REGION="$(grep -E '^region' terraform/terraform.tfvars | cut -d'"' -f2)"
ZONE="$(grep -E '^zone' terraform/terraform.tfvars | cut -d'"' -f2)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/apps/orders-api"
MODE="${1:-full}"

tf_out() { terraform -chdir=terraform output -raw "$1"; }

build_sign_release() {  # TAG FAIL_RATE
  local TAG="$1" FAIL_RATE="${2:-0}"
  echo "== build ${IMAGE}:${TAG} (Cloud Build, sa-build)"
  gcloud builds submit --project "$PROJECT_ID" --config cloudbuild.yaml --substitutions="_TAG=${TAG},_REGION=${REGION}" . --quiet
  local DIGEST
  DIGEST="$(gcloud artifacts docker images describe "${IMAGE}:${TAG}" --project "$PROJECT_ID" --format='value(image_summary.digest)')"
  echo "== sign + attest ${IMAGE}@${DIGEST}"
  gcloud beta container binauthz attestations sign-and-create --project "$PROJECT_ID" \
    --artifact-url="${IMAGE}@${DIGEST}" --attestor="$(tf_out attestor)" --attestor-project="$PROJECT_ID" \
    --keyversion="$(tf_out kms_key_version)" --quiet
  local NAME="rel-${TAG}-$(date -u +%H%M%S)"; [[ "$FAIL_RATE" != "0" ]] && NAME="bad-${NAME}"
  echo "== Cloud Deploy release ${NAME} (FAIL_RATE=${FAIL_RATE})"
  gcloud deploy releases create "$NAME" --project "$PROJECT_ID" --region "$REGION" \
    --delivery-pipeline "$(tf_out pipeline)" --images "orders-api=${IMAGE}@${DIGEST}" \
    --deploy-parameters="FAIL_RATE=${FAIL_RATE},EXTRA_LATENCY_MS=0" --skaffold-file skaffold.yaml --source . --quiet
  echo "$NAME"
}

case "$MODE" in
  release)
    build_sign_release "${2:-v$(date -u +%m%d%H%M)}" "${3:-0}"; exit 0 ;;
  slo)
    terraform -chdir=terraform apply -input=false -auto-approve; exit 0 ;;
esac

echo "== 1/6 terraform apply (the two SLOs fail until prod has served traffic: expected, see ./deploy.sh slo)"
terraform -chdir=terraform init -input=false >/dev/null
terraform -chdir=terraform apply -input=false -auto-approve || echo "   (continuing: run './deploy.sh slo' once prod has traffic)"

echo "== 2/6 kubeconfig"
gcloud container clusters get-credentials "$(tf_out cluster_name)" --zone "$ZONE" --project "$PROJECT_ID" >/dev/null

echo "== 3/6 Chaos Mesh (helm)"
helm repo add chaos-mesh https://charts.chaos-mesh.org >/dev/null 2>&1 || true
helm repo update >/dev/null
helm upgrade --install chaos-mesh chaos-mesh/chaos-mesh --namespace chaos-mesh --create-namespace \
  --set chaosDaemon.runtime=containerd --set chaosDaemon.socketPath=/run/containerd/containerd.sock \
  --set dashboard.create=false --wait --timeout 10m

echo "== 4/6 first image, signature, attestation and release"
NAME="$(build_sign_release v1 0 | tail -1)"

echo "== 5/6 wait for the rollout to reach prod (dev → staging → prod canary → stable)"
for _ in $(seq 1 90); do
  STATE="$(gcloud deploy rollouts list --project "$PROJECT_ID" --region "$REGION" --delivery-pipeline "$(tf_out pipeline)" --release "$NAME" \
    --format='value(targetId,state)' 2>/dev/null | tr '\n' ' ')"
  echo "   $STATE"
  [[ "$STATE" == *"prod SUCCEEDED"* ]] && break
  [[ "$STATE" == *"FAILED"* ]] && { echo "a rollout failed; see: gcloud deploy rollouts list ..."; break; }
  sleep 20
done

echo "== 6/6 gateway address"
for _ in $(seq 1 60); do
  IP="$(kubectl -n prod get gateway external -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)"
  [[ -n "$IP" ]] && break; sleep 10
done
echo
echo "prod:      http://${IP:-<pending>}/orders"
echo "pipeline:  $(tf_out pipeline_url)"
echo "next:      ./smoke.sh ; ./deploy.sh slo ; kubectl apply -f chaos/pod-kill.yaml ; ./deploy.sh release bad1 0.5"
