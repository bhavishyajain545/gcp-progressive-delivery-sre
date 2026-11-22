# Rebuild from zero

Terraform owns every cloud resource; `deploy.sh` builds, signs, attests and releases the first image; GitHub
Actions does the same keylessly for every push afterwards. This page is the exact order of operations plus the
gotchas met while building.

Time: ~25 minutes to a prod rollout (cluster ~7 min, Chaos Mesh ~2 min, build ~2 min, dev → staging → prod canary
~10 min with the automation's 60 s waits). Cost while up: ~₹400-500/day. Tear it down after the demo.

## 0. Prerequisites

| Need | Notes |
|---|---|
| Billing-linked GCP account | Budget + kill switch recommended. |
| `gcloud` (>= 590) **with the `beta` component**, `terraform` >= 1.6, `kubectl`, `helm` 3, `gke-gcloud-auth-plugin` | `gcloud components install beta kubectl gke-gcloud-auth-plugin`; `export USE_GKE_GCLOUD_AUTH_PLUGIN=True`. |
| Go 1.27, Python 3.12 + `google-auth requests` (postmortem tool, demo) | `cd app && go test ./...` |
| Vertex AI access to `gemini-3.8-flash` (global) | For the postmortem tool only. |
| A GitHub repo at `github_repo` (tfvars) | CI auth is bound to exactly this `owner/repo` by the WIF attribute condition. |
| Your public IP | `authorized_cidr`; the cluster also allows Google's public ranges so Cloud Deploy's default execution pool can reach the control plane. |

Organisation constraints: domain-restricted sharing must be overridden per project (Cloud Deploy, Binary
Authorization and the log sink add Google-managed principals):

```bash
export PROJECT_ID=bj-progressive-delivery
gcloud services enable orgpolicy.googleapis.com --project "$PROJECT_ID"
cat > /tmp/drs.yaml <<EOF
name: projects/$PROJECT_ID/policies/iam.allowedPolicyMemberDomains
spec:
  rules:
  - allowAll: true
EOF
gcloud org-policies set-policy /tmp/drs.yaml --project "$PROJECT_ID" --billing-project "$PROJECT_ID"
```

## 1. Project

```bash
gcloud projects create "$PROJECT_ID" --name "Progressive delivery SRE" --organization <ORG_ID>
gcloud billing projects link "$PROJECT_ID" --billing-account <BILLING_ACCOUNT_ID>
gcloud config set project "$PROJECT_ID"
gcloud auth application-default set-quota-project "$PROJECT_ID"
```

## 2. Configure

```bash
git clone https://github.com/bhavishyajain545/gcp-progressive-delivery-sre && cd gcp-progressive-delivery-sre
cp terraform/terraform.tfvars.example terraform/terraform.tfvars   # project_id, zone, authorized_cidr, github_repo, alert_email
```

If you change `project_id`, also change it in `k8s/common/serviceaccount.yaml`, `k8s/common/deployment.yaml`
(image, env) and `.github/workflows/release.yml` (env block).

## 3. Bring-up

```bash
./deploy.sh   # 1 terraform apply  2 kubeconfig  3 Chaos Mesh (helm)  4 Cloud Build image v1 → sign → attest → release
              # 5 wait for dev → staging → prod canary → stable  6 Gateway address
```

Expected on the first apply: the two SLOs fail with "Cannot find metric" until prod has served traffic. After
`./smoke.sh`, run `./deploy.sh slo`.

## 4. Verify

```bash
./smoke.sh     # prod through the Gateway; releases + rollouts; `kubectl run nginx` denied by Binary Authorization;
               # the attestation on the running digest; Chaos Mesh pods; namespaces
gcloud deploy rollouts describe <rollout> --release <release> --delivery-pipeline orders-api --region us-central1 \
  --format='yaml(phases[].id,phases[].state,phases[].deploymentJobs.verifyJob.state)'
```

Game days and the bad release:

```bash
kubectl apply -f chaos/pod-kill.yaml           # then: watch kubectl -n prod get pods ; ./smoke.sh shows no 5xx
kubectl apply -f chaos/network-delay.yaml      # then: dashboard p95 panel, SLO page, alert email
./deploy.sh release bad1 0.5                   # bad release: verify fails, automation rolls back
gcloud deploy rollouts list --delivery-pipeline orders-api --region us-central1 --format='table(name.basename(),targetId,state,failureReason)'
python postmortem/postmortem.py --last 30m --title "bad release bad1 caught by verify"
```

## 5. CI (keyless)

```bash
terraform -chdir=terraform output -raw workload_identity_provider
gh secret set GCP_WORKLOAD_IDENTITY_PROVIDER --repo <owner/repo> --body "<that value>"
```

Push under `app/`, `k8s/` or `skaffold.yaml`: the workflow tests, builds, pushes, signs the digest with the KMS
key, creates the attestation and the Cloud Deploy release. "Run workflow" with `fail_rate=0.5` ships the bad
release on purpose.

## 6. Re-record the demo (optional)

DemoProfile Chrome with `--remote-debugging-port=9222` signed in to the console; `DEMO_EXTRA_PATH` with gcloud and
ffmpeg. `python -u demo/scenes.py` creates a real bad release and waits for the rollback, so a take is ~12 minutes.

## 7. Tear down (stop all cost)

```bash
./teardown.sh        # namespaces + chaos → terraform destroy → Gateway LB leftovers + staging buckets → destroy again → audit
./audit-billing.sh
```

KMS key rings cannot be deleted; the key versions are destroyed and cost nothing. The WIF pool is soft-deleted
for 30 days. The empty project costs nothing.

## Gotchas met while building

- `--images` keys must be the image name exactly as the manifests reference it (`us-central1-docker.pkg.dev/…/orders-api`),
  not a short alias. With `orders-api=…` Skaffold changed nothing, the pod referenced an un-pinned image and Binary
  Authorization denied it: a correct denial for the wrong reason.
- The operator's ISP moved the public IP to another /16 mid-build; `authorized_cidrs` is a list for that reason.

- A Namespace rendered in the same release as the objects inside it races `kubectl apply` ("namespaces dev not
  found") and the first rollout fails. Namespaces live in `k8s/namespaces/` and are applied by `deploy.sh`.

- **Kustomize strips comments**, so Cloud Deploy's `# from-param: ${X}` placeholders vanished from the rendered
  manifests. The repo renders raw YAML per Skaffold profile instead; `deploy.kubectl.defaultNamespace` picks the namespace.
- Chaos Mesh needs a privileged DaemonSet → GKE Standard, not Autopilot; on GKE set
  `chaosDaemon.runtime=containerd` and `chaosDaemon.socketPath=/run/containerd/containerd.sock`.
- Binary Authorization applies to *every* pod the cluster admits, including verify Jobs and Chaos Mesh:
  allow-list their images by pattern or they are denied too.
- The verify Job runs in the `default` namespace (in-cluster execution mode); the app NetworkPolicy must allow
  ingress from that namespace or verification times out and the rollout fails for the wrong reason.
- The Cloud Deploy service agent must be able to act as the execution service account
  (`roles/iam.serviceAccountUser` for `service-<number>@gcp-sa-clouddeploy.iam.gserviceaccount.com`).
- `gcloud beta container binauthz attestations sign-and-create` needs the `beta` component and
  `roles/cloudkms.signerVerifier` on the key, `containeranalysis.notes.attacher` on the note.
