#!/usr/bin/env bash
# Prove the delivery track end to end against the live platform.
#   ./smoke.sh
set -uo pipefail
cd "$(dirname "$0")"
export USE_GKE_GCLOUD_AUTH_PLUGIN=True
PROJECT_ID="$(grep -E '^project_id' terraform/terraform.tfvars | cut -d'"' -f2)"
REGION="$(grep -E '^region' terraform/terraform.tfvars | cut -d'"' -f2)"
IP="$(kubectl -n prod get gateway external -o jsonpath='{.status.addresses[0].value}')"
GW="http://$IP"
c() { curl -s --max-time 20 "$@"; }

echo "== prod through the Gateway: $GW"
echo "-- version: $(c $GW/version)"
echo "-- create:  $(c -X POST $GW/orders -H 'content-type: application/json' -d '{"item":"smoke","qty":2}')"
echo "-- list:    $(c $GW/orders | head -c 200)"
echo "-- 30 requests, status histogram:"; for i in $(seq 1 30); do c -o /dev/null -w '%{http_code}\n' -X POST $GW/orders -H 'content-type: application/json' -d "{\"item\":\"smoke-$i\",\"qty\":1}"; done | sort | uniq -c

echo "== Cloud Deploy"
gcloud deploy releases list --project "$PROJECT_ID" --region "$REGION" --delivery-pipeline orders-api --format='table(name.basename(),renderState,createTime.date())' --limit 5 2>/dev/null
gcloud deploy rollouts list --project "$PROJECT_ID" --region "$REGION" --delivery-pipeline orders-api --format='table(name.basename(),targetId,state,phases[].id.list():label=PHASES)' --limit 6 2>/dev/null

echo "== Binary Authorization: an unsigned image must be rejected"
kubectl -n prod run binauthz-probe --image=nginx:1.27-alpine --restart=Never --command -- sleep 5 2>&1 | sed 's/^/   /'
kubectl -n prod delete pod binauthz-probe --ignore-not-found >/dev/null 2>&1
echo "-- attestations for the running image:"
IMG="$(kubectl -n prod get deploy orders-api -o jsonpath='{.spec.template.spec.containers[0].image}')"
gcloud beta container binauthz attestations list --project "$PROJECT_ID" --attestor delivery-ci-attestor --attestor-project "$PROJECT_ID" --artifact-url "$IMG" --format='value(name)' 2>/dev/null | head -1
echo "== Chaos Mesh: $(kubectl -n chaos-mesh get pods --no-headers 2>/dev/null | awk '{print $3}' | sort | uniq -c | tr '\n' ' ')"
echo "== namespaces: $(kubectl get ns dev staging prod --no-headers | awk '{print $1":"$2}' | tr '\n' ' ')"
