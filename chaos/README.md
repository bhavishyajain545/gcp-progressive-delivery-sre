# Game days

Chaos Mesh is installed by `deploy.sh` (namespace `chaos-mesh`, containerd runtime). Experiments live
here as plain manifests; the third experiment is not a Chaos Mesh object but a release:

| # | Experiment | Command | Expected |
|---|---|---|---|
| 1 | Kill 50% of prod pods | `kubectl apply -f chaos/pod-kill.yaml` | PDB/HPA keep serving; Gateway drops dead backends in ~10 s; no 5xx at the edge |
| 2 | +300 ms to Firestore for 2 min | `kubectl apply -f chaos/network-delay.yaml` | p95 > 300 ms; latency SLO burns; fast-burn alert fires; availability unaffected |
| 3 | Bad release (`FAIL_RATE=0.5`) | `gcloud deploy releases create … --deploy-parameters=FAIL_RATE=0.5` or the `bad-release` workflow input | dev/staging verify fails, or prod canary fails at 25%; automation rolls back; prod stable pods never run it |

Clean up: `kubectl delete -f chaos/`. Each experiment leaves a trail in Cloud Logging, which the log sink
copies to BigQuery, which `postmortem/postmortem.py` turns into a draft.
