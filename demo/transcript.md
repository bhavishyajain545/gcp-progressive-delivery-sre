# Demo transcript: demo

## 00:00  Architecture

This project is about shipping safely. A commit to orders-api rides one track: GitHub Actions tests it, builds the image and pushes it by digest, signs that digest with a Cloud KMS key and records a Binary Authorization attestation, then creates a Cloud Deploy release. Cloud Deploy promotes it through dev and staging with an in-cluster verification job at every stage, and into production as a canary on Gateway API traffic splitting, twenty-five, fifty, then one hundred percent, verified at each phase. An automation advances the phases and rolls back when verification fails. The cluster only admits attested images. Chaos Mesh game days and a deliberately bad release prove the guardrails, and every incident ends in a Gemini-drafted postmortem built from the logs. Everything is Terraform, and nothing in the path holds a key.

## 00:54  Binary Authorization

First guardrail: the cluster itself. Binary Authorization is enforced with a policy that requires an attestation from our CI attestor. I try to run a plain nginx image in production: admission denies it, and the denial is an audit log entry. The image that is running carries an attestation on its exact digest, signed by a Cloud KMS key that only the CI identity can use. Tags are not identities here; digests are.

## 01:23  Game day 1 pod-kill

Game day one, while a release is building in the background. Chaos Mesh kills half of production's pods. The PodDisruptionBudget and the autoscaler keep the service up, the Gateway health check drops the dead backends within seconds, and thirty requests through the load balancer all come back two-oh-one. The availability SLO does not notice.

## 01:43  Release

The release itself. Cloud Build tested and built the image; the digest was signed and attested; then a Cloud Deploy release was created with that digest and the Skaffold configuration. From here nobody runs kubectl: Cloud Deploy renders the manifests per target, with the deploy parameters substituted, and the automation takes it through the stages.

## 02:04  Pipeline

This is the delivery pipeline in the console. The dev rollout deploys and then runs the verify job inside the cluster: eighty requests against the service, at most one failure, p95 under three hundred milliseconds, the SLOs restated as a gate. When it passes, the automation promotes to staging after a minute, and staging verifies again.

## 02:27  Prod canary

Production is a canary. Cloud Deploy creates a canary deployment next to the stable one and rewrites the HTTPRoute weights: twenty-five percent of traffic to the new digest, then fifty, then all of it. At each phase the verify job targets the canary service specifically, so a bad build is caught while most users are still on the stable version. You can see the weights on the route and the two deployments side by side.

## 02:54  Game day 2 network delay

Game day two, while a bad release builds. Chaos Mesh adds three hundred milliseconds to every packet leaving production's pods for two minutes, the datastore round trip and the response path included. Availability stays green, but p95 crosses the latency objective on the dashboard and the latency SLO starts burning budget, which is exactly the alert that should fire. This is the difference between an outage and a degradation, and the SLOs tell them apart.

## 03:23  Bad release caught

Now the real test. I shipped a release with a fifty percent failure rate injected into the business routes only; the pods look healthy to kubelet, probes pass, it is Running. The verify job sees forty failures out of eighty and fails the rollout at the first gate. The repair automation rolls dev back to the previous release, promotion never happens, and production never ran it. Had it slipped through, the same verification runs against the canary at twenty-five percent.

## 03:56  SLOs

The SLOs in Cloud Monitoring: ninety-nine point five percent of production orders without a 5xx, and ninety-five percent under three hundred milliseconds, each with a multi-window burn-rate alert. The pod-kill left the availability budget untouched; the network delay took a visible bite out of the latency budget, which is what a game day is for.

## 04:17  Postmortem

The learning loop. A log sink routes application logs, Cloud Deploy and Binary Authorization audit entries, Kubernetes events and Chaos Mesh activity into BigQuery. One command pulls the last half hour of that evidence plus the production error ratio and latency from the Monitoring API, and asks Gemini for a blameless postmortem draft: timeline, impact, detection, root cause, action items. It is told to say when evidence is missing. A human edits it, then it is committed next to the code.

## 04:52  Wrap-up

Everything shown is in the repository: the Go service with its fault knobs and tests, Terraform for the cluster, Cloud Deploy pipeline and automation, the KMS attestor and the Binary Authorization policy, the Skaffold verify job, the chaos experiments, the postmortem tool and the keyless release workflow. The platform costs a few hundred rupees a day while it runs, and the teardown script removes it. Thanks for watching.
