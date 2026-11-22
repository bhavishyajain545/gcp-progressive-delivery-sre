"""Demo video for Project 5: progressive delivery with guardrails.

Prerequisites:
  * platform deployed (`./deploy.sh`), prod reachable through the Gateway, `./deploy.sh slo` done
  * kubeconfig + terraform state in WSL (gcloud/kubectl/terraform/helm run there on Windows)
  * Chrome started with --remote-debugging-port=9222 and signed in to the Cloud console
  * DEMO_EXTRA_PATH with gcloud and ffmpeg; Python deps for postmortem/ (google-auth, requests) in WSL

  python -u demo/scenes.py

The take creates two real releases (one healthy, one with FAIL_RATE=0.5) and waits for Cloud Deploy, so it runs ~12 minutes.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

from recorder import Demo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PROJECT = "bj-progressive-delivery"
REGION = "us-central1"
ZONE = "us-central1-a"
PIPELINE = "orders-api"
CDP_URL = os.environ.get("DEMO_CDP_URL", "http://127.0.0.1:9222")

for p in os.environ.get("DEMO_EXTRA_PATH", "").split(os.pathsep):
    if p:
        os.environ["PATH"] = p + os.pathsep + os.environ["PATH"]
os.chdir(ROOT)


def wsl(cmd: str, timeout: int = 300) -> str:
    root = "/mnt/" + ROOT.drive[0].lower() + ROOT.as_posix()[2:]
    full = f"source ~/.gcp-env.sh; export USE_GKE_GCLOUD_AUTH_PLUGIN=True; cd {root}; {cmd}"
    try:
        r = subprocess.run(["wsl", "-e", "bash", "-c", full], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return "(timed out)"


def gd(args: str, timeout: int = 120) -> str:
    return wsl(f"gcloud deploy {args} --project {PROJECT} --region {REGION} 2>&1", timeout=timeout)


def rollouts(release: str) -> str:
    return gd(f"rollouts list --delivery-pipeline {PIPELINE} --release {release} --format='table[no-heading](targetId,state,phases[].id.list():label=PHASES)'").strip()


def cdp_available() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(CDP_URL + "/json/version", timeout=2):
            return True
    except Exception:
        return False


async def type_cmd(page, cmd: str, type_delay: int = 7, chunk: int = 4) -> None:
    await page.evaluate("(c) => window.__prompt(c)", "")
    for i in range(0, len(cmd), chunk):
        await page.evaluate("(c) => window.__type(c)", cmd[i:i + chunk])
        await asyncio.sleep(type_delay * chunk / 1000)


async def live_steps(page, steps, type_delay: int = 7, pause: float = 0.45, hold: float = 3.5) -> None:
    for cmd, producer in steps:
        await type_cmd(page, cmd, type_delay)
        out = await asyncio.to_thread(producer) if callable(producer) else producer
        await asyncio.sleep(pause)
        await page.evaluate("(o) => window.__output(o)", out)
        await asyncio.sleep(pause)
    await asyncio.sleep(hold)


def trim(s: str, n: int = 1500) -> str:
    return s if len(s) <= n else s[:n] + "\n…"


NARRATION = {
    "arch": (
        "This project is about shipping safely. A commit to orders-api rides one track: GitHub Actions tests it, builds the image and pushes it by digest, "
        "signs that digest with a Cloud KMS key and records a Binary Authorization attestation, then creates a Cloud Deploy release. "
        "Cloud Deploy promotes it through dev and staging with an in-cluster verification job at every stage, and into production as a canary on Gateway API traffic splitting, "
        "twenty-five, fifty, then one hundred percent, verified at each phase. An automation advances the phases and rolls back when verification fails. "
        "The cluster only admits attested images. Chaos Mesh game days and a deliberately bad release prove the guardrails, and every incident ends in a Gemini-drafted postmortem built from the logs. "
        "Everything is Terraform, and nothing in the path holds a key."
    ),
    "binauthz": (
        "First guardrail: the cluster itself. Binary Authorization is enforced with a policy that requires an attestation from our CI attestor. "
        "I try to run a plain nginx image in production: admission denies it, and the denial is an audit log entry. "
        "The image that is running carries an attestation on its exact digest, signed by a Cloud KMS key that only the CI identity can use. "
        "Tags are not identities here; digests are."
    ),
    "podkill": (
        "Game day one, while a release is building in the background. Chaos Mesh kills half of production's pods. "
        "The PodDisruptionBudget and the autoscaler keep the service up, the Gateway health check drops the dead backends within seconds, "
        "and thirty requests through the load balancer all come back two-oh-one. The availability SLO does not notice."
    ),
    "release": (
        "The release itself. Cloud Build tested and built the image; the digest was signed and attested; then a Cloud Deploy release was created with that digest and the Skaffold configuration. "
        "From here nobody runs kubectl: Cloud Deploy renders the manifests per target, with the deploy parameters substituted, and the automation takes it through the stages."
    ),
    "pipeline": (
        "This is the delivery pipeline in the console. The dev rollout deploys and then runs the verify job inside the cluster: eighty requests against the service, "
        "at most one failure, p95 under three hundred milliseconds, the SLOs restated as a gate. When it passes, the automation promotes to staging after a minute, and staging verifies again."
    ),
    "canary": (
        "Production is a canary. Cloud Deploy creates a canary deployment next to the stable one and rewrites the HTTPRoute weights: twenty-five percent of traffic to the new digest, then fifty, then all of it. "
        "At each phase the verify job targets the canary service specifically, so a bad build is caught while most users are still on the stable version. "
        "You can see the weights on the route and the two deployments side by side."
    ),
    "netdelay": (
        "Game day two, while a bad release builds. Chaos Mesh adds three hundred milliseconds to every packet from production to Firestore for two minutes. "
        "Availability stays green, but p95 crosses the latency objective on the dashboard and the latency SLO starts burning budget, which is exactly the alert that should fire. "
        "This is the difference between an outage and a degradation, and the SLOs tell them apart."
    ),
    "bad": (
        "Now the real test. I shipped a release with a fifty percent failure rate injected into the business routes only; the pods look healthy to kubelet, probes pass, it is Running. "
        "The verify job sees forty failures out of eighty and fails the rollout at the first gate. The repair automation rolls dev back to the previous release, "
        "promotion never happens, and production never ran it. Had it slipped through, the same verification runs against the canary at twenty-five percent."
    ),
    "slo": (
        "The SLOs in Cloud Monitoring: ninety-nine point five percent of production orders without a 5xx, and ninety-five percent under three hundred milliseconds, "
        "each with a multi-window burn-rate alert. The pod-kill left the availability budget untouched; the network delay took a visible bite out of the latency budget, which is what a game day is for."
    ),
    "postmortem": (
        "The learning loop. A log sink routes application logs, Cloud Deploy and Binary Authorization audit entries, Kubernetes events and Chaos Mesh activity into BigQuery. "
        "One command pulls the last half hour of that evidence plus the production error ratio and latency from the Monitoring API, and asks Gemini for a blameless postmortem draft: "
        "timeline, impact, detection, root cause, action items. It is told to say when evidence is missing. A human edits it, then it is committed next to the code."
    ),
    "wrap": (
        "Everything shown is in the repository: the Go service with its fault knobs and tests, Terraform for the cluster, Cloud Deploy pipeline and automation, the KMS attestor and the Binary Authorization policy, "
        "the Skaffold verify job, the chaos experiments, the postmortem tool and the keyless release workflow. "
        "The platform costs a few hundred rupees a day while it runs, and the teardown script removes it. Thanks for watching."
    ),
}


async def main() -> None:
    use_cdp = cdp_available()
    demo = Demo(out_dir=HERE, cdp_url=CDP_URL if use_cdp else None, max_silence=5.0, tail=4.0)
    await demo.prepare(*NARRATION.values())

    t0 = time.time()
    ip = wsl("kubectl -n prod get gateway external -o jsonpath='{.status.addresses[0].value}'")
    api = f"http://{ip}"
    pipeline_url = wsl("terraform -chdir=terraform output -raw pipeline_url")
    slo_url = wsl("terraform -chdir=terraform output -raw slo_url")
    dash_url = wsl("terraform -chdir=terraform output -raw dashboard_url")
    attestor = wsl("terraform -chdir=terraform output -raw attestor")
    running_img = wsl("kubectl -n prod get deploy orders-api -o jsonpath='{.spec.template.spec.containers[0].image}'")
    policy = wsl(f"gcloud container binauthz policy export --project {PROJECT} 2>/dev/null | grep -vE '^\\s*- namePattern' | head -12")
    cluster = wsl(f"gcloud container clusters describe delivery --zone {ZONE} --project {PROJECT} --format='value(binaryAuthorization.evaluationMode,privateClusterConfig.enablePrivateNodes,workloadIdentityConfig.workloadPool)'")
    stamp = time.strftime("%H%M", time.gmtime())
    print(f"pre-computed in {time.time() - t0:.0f}s; api={api}")

    # --- a healthy release builds in the background during scenes 1-3 (~3 min) ---
    good: dict = {}

    def do_good_release():
        out = wsl(f"./deploy.sh release demo{stamp} 0 2>&1 | tail -25", timeout=900)
        good["log"] = out
        good["name"] = out.strip().splitlines()[-1].strip() if out.strip() else ""
    good_task = asyncio.ensure_future(asyncio.to_thread(do_good_release))

    page = await demo.start()
    pipe_tab = slo_tab = dash_tab = None
    if use_cdp:
        pipe_tab = await demo.new_tab(pipeline_url)
        dash_tab = await demo.new_tab(dash_url)
        slo_tab = await demo.new_tab(slo_url)
        await demo.capture(page)

    # ---- 1 architecture (animated) ----
    async def s1():
        await page.goto((ROOT / "docs" / "architecture.html").as_uri())
        await page.evaluate("() => { document.body.style.zoom = '1.33'; document.body.style.margin = '0'; }")
    await demo.scene("Architecture", NARRATION["arch"], s1)

    # ---- 2 Binary Authorization ----
    async def s2():
        await demo.terminal("Guardrail 1: Binary Authorization admits attested digests only", [])
        await live_steps(page, [
            ("kubectl -n prod run unsigned --image=nginx:1.27-alpine --restart=Never -- sleep 60",
             lambda: wsl("kubectl -n prod run unsigned --image=nginx:1.27-alpine --restart=Never -- sleep 60 2>&1; kubectl -n prod delete pod unsigned --ignore-not-found >/dev/null 2>&1") or "(no output)"),
            ("kubectl -n prod get deploy orders-api -o jsonpath='{.spec.template.spec.containers[0].image}'", running_img),
            (f"gcloud beta container binauthz attestations list --attestor {attestor} --artifact-url \"$IMAGE\" --format='value(name,attestation.serializedPayload.len())'",
             lambda: wsl(f"gcloud beta container binauthz attestations list --project {PROJECT} --attestor {attestor} --attestor-project {PROJECT} --artifact-url '{running_img}' --format='value(name)' 2>&1 | head -2")),
            ("gcloud container binauthz policy export | grep -v namePattern", policy),
            (f"gcloud container clusters describe delivery --zone {ZONE} --format='value(binaryAuthorization.evaluationMode,privateClusterConfig.enablePrivateNodes,workloadIdentityConfig.workloadPool)'", cluster),
        ], type_delay=5)
    await demo.scene("Binary Authorization", NARRATION["binauthz"], s2, hold=1.0)

    # ---- 3 game day 1: pod-kill ----
    async def s3():
        await demo.terminal("Game day 1: Chaos Mesh pod-kill on prod", [])
        await live_steps(page, [
            ("kubectl -n prod get pods -l app=orders-api", lambda: wsl("kubectl -n prod get pods -l app=orders-api")),
            ("kubectl apply -f chaos/pod-kill.yaml", lambda: wsl("kubectl apply -f chaos/pod-kill.yaml")),
            ("sleep 8; kubectl -n prod get pods -l app=orders-api", lambda: wsl("sleep 8; kubectl -n prod get pods -l app=orders-api")),
            ("for i in $(seq 1 30); do curl -s -o /dev/null -w '%{http_code}\\n' -X POST $GW/orders -d '{\"item\":\"gameday\",\"qty\":1}'; done | sort | uniq -c",
             lambda: wsl(f"for i in $(seq 1 30); do curl -s --max-time 10 -o /dev/null -w '%{{http_code}}\\n' -X POST {api}/orders -H 'content-type: application/json' -d '{{\"item\":\"gameday-'$i'\",\"qty\":1}}'; done | sort | uniq -c", timeout=400)),
            ("kubectl -n prod get pdb,hpa", lambda: wsl("kubectl -n prod get pdb,hpa; kubectl delete -f chaos/pod-kill.yaml >/dev/null 2>&1")),
        ], type_delay=5)
    await demo.scene("Game day: pod-kill", NARRATION["podkill"], s3, hold=1.0)

    # ---- 4 the release ----
    if not good_task.done():
        try:
            await asyncio.wait_for(good_task, timeout=600)
        except Exception:
            pass
    rel = good.get("name", "")

    async def s4():
        await demo.terminal("A release: build → sign → attest → Cloud Deploy", [])
        await live_steps(page, [
            (f"./deploy.sh release demo{stamp}", trim(good.get("log", "(release log missing)"), 1700)),
            (f"gcloud deploy releases describe {rel} --delivery-pipeline {PIPELINE} --format='value(name.basename(),buildArtifacts[0].image,deployParameters,renderState)'",
             lambda: gd(f"releases describe {rel} --delivery-pipeline {PIPELINE} --format='value(name.basename(),buildArtifacts[0].image,deployParameters,renderState)'")),
        ], type_delay=5)
    await demo.scene("Release", NARRATION["release"], s4, hold=1.0)

    # ---- 5 pipeline in the console ----
    if use_cdp:
        async def s5():
            await pipe_tab.reload(wait_until="commit")
            await demo.capture(pipe_tab)
            await asyncio.sleep(10)
            # stay on the page while dev/staging progress; leave once staging succeeded or 70 s passed
            for _ in range(14):
                st = rollouts(rel)
                if "staging" in st and ("SUCCEEDED" in st.split("staging")[-1] or "prod" in st):
                    break
                await asyncio.sleep(5)
            await pipe_tab.reload(wait_until="commit")
            await asyncio.sleep(8)
        await demo.scene("Pipeline", NARRATION["pipeline"], s5, hold=1.5)
        await demo.capture(page)

    # ---- 6 prod canary ----
    async def s6():
        await demo.terminal("Prod canary: 25% → 50% → 100% on Gateway API weights", [])
        # wait for the prod rollout to be in a canary phase
        for _ in range(60):
            st = rollouts(rel)
            if "prod" in st and ("IN_PROGRESS" in st.split("prod")[-1] or "SUCCEEDED" in st.split("prod")[-1]):
                break
            await asyncio.sleep(5)
        await live_steps(page, [
            (f"gcloud deploy rollouts list --delivery-pipeline {PIPELINE} --release {rel}", lambda: rollouts(rel)),
            ("kubectl -n prod get httproute orders-api -o jsonpath='{range .spec.rules[0].backendRefs[*]}{.name}={.weight}{\"\\n\"}{end}'",
             lambda: wsl("for i in $(seq 1 12); do W=$(kubectl -n prod get httproute orders-api -o jsonpath='{range .spec.rules[0].backendRefs[*]}{.name}={.weight}{\"\\n\"}{end}'); echo \"$W\" | grep -q canary && break; sleep 5; done; echo \"$W\"", timeout=120)),
            ("kubectl -n prod get deploy,svc -l app=orders-api", lambda: wsl("kubectl -n prod get deploy,svc 2>&1 | grep -E 'NAME|orders-api'")),
            ("kubectl -n default get jobs | tail -4   # verify jobs Cloud Deploy ran in-cluster", lambda: wsl("kubectl -n default get jobs 2>&1 | tail -4")),
        ], type_delay=5)
    await demo.scene("Prod canary", NARRATION["canary"], s6, hold=1.0)

    # --- the bad release builds in the background during scene 7 (~3 min) ---
    bad: dict = {}

    def do_bad_release():
        out = wsl(f"./deploy.sh release bad{stamp} 0.5 2>&1 | tail -6", timeout=900)
        bad["log"] = out
        bad["name"] = out.strip().splitlines()[-1].strip() if out.strip() else ""
    bad_task = asyncio.ensure_future(asyncio.to_thread(do_bad_release))

    # ---- 7 game day 2: network delay + dashboard ----
    async def s7():
        await demo.terminal("Game day 2: +300 ms to Firestore (NetworkChaos)", [])
        await live_steps(page, [
            ("kubectl apply -f chaos/network-delay.yaml", lambda: wsl("kubectl apply -f chaos/network-delay.yaml")),
            ("for i in $(seq 1 10); do curl -s -o /dev/null -w '%{http_code} %{time_total}s\\n' -X POST $GW/orders -d '{\"item\":\"slow\",\"qty\":1}'; done",
             lambda: wsl(f"sleep 5; for i in $(seq 1 10); do curl -s --max-time 10 -o /dev/null -w '%{{http_code}} %{{time_total}}s\\n' -X POST {api}/orders -H 'content-type: application/json' -d '{{\"item\":\"slow-'$i'\",\"qty\":1}}'; done", timeout=200)),
        ], type_delay=5, hold=1.0)
        if use_cdp:
            await dash_tab.reload(wait_until="commit")
            await demo.capture(dash_tab)
            await asyncio.sleep(12)
    await demo.scene("Game day: network delay", NARRATION["netdelay"], s7, hold=1.5)
    if use_cdp:
        await demo.capture(page)

    # ---- 8 the bad release is caught ----
    if not bad_task.done():
        try:
            await asyncio.wait_for(bad_task, timeout=600)
        except Exception:
            pass
    badrel = bad.get("name", "")

    async def s8():
        await demo.terminal("The bad release: FAIL_RATE=0.5, caught by verify, rolled back", [])
        # wait for dev to FAIL and the repair rollout to appear
        for _ in range(48):
            st = gd(f"rollouts list --delivery-pipeline {PIPELINE} --release {badrel} --format='value(state)'")
            if "FAILED" in st:
                break
            await asyncio.sleep(5)
        await asyncio.sleep(20)  # give the repair automation time to start the rollback
        await live_steps(page, [
            (f"./deploy.sh release bad{stamp} 0.5", trim(bad.get("log", ""), 600)),
            (f"gcloud deploy releases describe {badrel} --delivery-pipeline {PIPELINE} --format='value(deployParameters)'",
             lambda: gd(f"releases describe {badrel} --delivery-pipeline {PIPELINE} --format='value(deployParameters)'")),
            ("kubectl -n dev get pods -l app=orders-api   # healthy to kubelet", lambda: wsl("kubectl -n dev get pods -l app=orders-api")),
            (f"gcloud deploy rollouts list --delivery-pipeline {PIPELINE} --format='table(name.basename(),targetId,state,failureReason,rollbackOfRollout.basename())' --limit 4",
             lambda: gd(f"rollouts list --delivery-pipeline {PIPELINE} --format='table(name.basename(),targetId,state,failureReason.slice(0:90).join(\"\"),rollbackOfRollout.basename())' --limit 4")),
            ("gcloud deploy rollouts describe <failed> --format='value(phases[0].deploymentJobs.verifyJob.state, phases[0].deploymentJobs.verifyJob.jobRun.basename())'",
             lambda: gd(f"rollouts list --delivery-pipeline {PIPELINE} --release {badrel} --format='value(name.basename(),phases[0].deploymentJobs.verifyJob.state)'")),
            (f"gcloud deploy job-runs list --delivery-pipeline {PIPELINE} --release {badrel} --rollout <failed> --format='value(name.basename(),state)' ; kubectl -n default logs job/<verify> | tail -3",
             lambda: wsl("J=$(kubectl -n default get jobs --sort-by=.metadata.creationTimestamp -o name 2>/dev/null | tail -1); kubectl -n default logs $J 2>/dev/null | grep -E 'verify:' | tail -3", timeout=60)),
        ], type_delay=5)
    await demo.scene("Bad release caught", NARRATION["bad"], s8, hold=1.0)

    # ---- 9 SLOs ----
    if use_cdp:
        async def s9():
            await slo_tab.reload(wait_until="commit")
            await demo.capture(slo_tab)
            try:
                await slo_tab.wait_for_selector("text=/orders", timeout=60000)
            except Exception:
                pass
            await asyncio.sleep(6)
        await demo.scene("SLOs", NARRATION["slo"], s9, hold=1.5)
        await demo.capture(page)

    # ---- 10 postmortem ----
    async def s10():
        await demo.terminal("Learning loop: logs → BigQuery → Gemini → postmortem draft", [])
        await live_steps(page, [
            ("bq ls incidents", lambda: wsl(f"bq ls --project_id={PROJECT} incidents 2>&1 | head -12")),
            (f"python3 postmortem/postmortem.py --last 30m --title 'bad release bad{stamp} caught by verify; chaos game day'",
             lambda: trim(wsl(f"python3 postmortem/postmortem.py --last 30m --title 'bad release bad{stamp} caught by verify; chaos game day' 2>&1 | head -40", timeout=240), 2200)),
        ], type_delay=5, hold=4.0)
    await demo.scene("Postmortem", NARRATION["postmortem"], s10, hold=1.0)

    # ---- 11 wrap ----
    async def s11():
        await demo.terminal("Infrastructure as code + cleanup", [])
        await live_steps(page, [
            ("terraform -chdir=terraform state list | wc -l", lambda: wsl("terraform -chdir=terraform state list | wc -l")),
            ("terraform -chdir=terraform output", lambda: wsl("terraform -chdir=terraform output -no-color")),
            ("ls postmortems/", lambda: wsl("ls postmortems/ 2>/dev/null; kubectl delete -f chaos/ >/dev/null 2>&1; true")),
            ("# when done: ./teardown.sh", "namespaces + chaos → terraform destroy → load-balancer leftovers → destroy again → billing audit"),
        ], type_delay=6)
    await demo.scene("Wrap-up", NARRATION["wrap"], s11, hold=1.5)

    await demo.finish("demo")


if __name__ == "__main__":
    asyncio.run(main())
