#!/usr/bin/env bash
# Full teardown so nothing bills afterwards.
#   ./teardown.sh   # 1 delete k8s objects (Gateway -> LB cleanup) + chaos  2 terraform destroy
#                   # 3 Gateway-controller leftovers  4 destroy again  5 audit
set -uo pipefail
cd "$(dirname "$0")"
PROJECT_ID="$(grep -E '^project_id' terraform/terraform.tfvars | cut -d'"' -f2)"
REGION="$(grep -E '^region' terraform/terraform.tfvars | cut -d'"' -f2)"
export USE_GKE_GCLOUD_AUTH_PLUGIN=True
g() { gcloud "$@" --project "$PROJECT_ID" --quiet; }

echo "== 1/5 delete Kubernetes objects (Gateway -> LB cleanup), chaos experiments, Chaos Mesh"
if timeout 20 kubectl get ns prod >/dev/null 2>&1; then
  timeout 60 kubectl delete -f chaos/ --ignore-not-found 2>/dev/null || true
  for e in prod staging dev; do timeout 300 kubectl delete ns "$e" --ignore-not-found --wait=true || true; done
  timeout 300 helm uninstall chaos-mesh -n chaos-mesh 2>/dev/null || true
  for _ in $(seq 1 12); do
    n=$(g compute forwarding-rules list --format='value(name)' | wc -l); echo "  forwarding rules left: $n"
    [ "$n" = "0" ] && break; sleep 15
  done
else
  echo "  control plane unreachable or namespaces gone; skipping (leftovers are removed in step 3)"
fi

echo "== 2/5 terraform destroy"
terraform -chdir=terraform init -input=false >/dev/null 2>&1 || true
terraform -chdir=terraform destroy -input=false -auto-approve 2>&1 | grep -v 'Still destroying' | tail -15

echo "== 3/5 Gateway / GKE leftovers (not in Terraform state)"
del_all() { local list=(); while [ "$1" != "--" ]; do list+=("$1"); shift; done; shift
  for n in $(g "${list[@]}" --format='value(name)' 2>/dev/null); do echo "  delete $n"; g "$@" "$n" || true; done; }
del_all compute forwarding-rules list --global -- compute forwarding-rules delete --global
del_all compute target-https-proxies list --global -- compute target-https-proxies delete --global
del_all compute target-http-proxies list --global -- compute target-http-proxies delete --global
del_all compute url-maps list --global -- compute url-maps delete --global
del_all compute backend-services list --global -- compute backend-services delete --global
del_all compute health-checks list --global -- compute health-checks delete --global
for line in $(g compute network-endpoint-groups list --format='csv[no-heading](name,zone.basename())' 2>/dev/null); do
  IFS=, read -r n z <<< "$line"; echo "  delete NEG $n"; g compute network-endpoint-groups delete "$n" --zone "$z" || true
done
del_all compute addresses list --global -- compute addresses delete --global
for n in $(g compute firewall-rules list --format='value(name)' 2>/dev/null | grep -v '^default-'); do echo "  delete firewall $n"; g compute firewall-rules delete "$n" || true; done
# Cloud Deploy and Cloud Build leave staging buckets behind
for b in $(gcloud storage ls --project "$PROJECT_ID" 2>/dev/null); do echo "  delete bucket $b"; gcloud storage rm -r "$b" --project "$PROJECT_ID" --quiet || true; done

echo "== 4/5 terraform destroy again (VPC may have been blocked by the leftovers)"
terraform -chdir=terraform destroy -input=false -auto-approve 2>&1 | grep -v 'Still destroying' | tail -5

echo "== 5/5 what is left in $PROJECT_ID"
./audit-billing.sh "$PROJECT_ID"
echo "done. KMS key rings cannot be deleted (key versions are destroyed, no charge); the WIF pool is soft-deleted for 30 days; an empty project costs nothing."
