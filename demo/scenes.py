"""Demo video for Project 5: progressive delivery with guardrails.

Prerequisites:
  * platform deployed (`./deploy.sh`), prod reachable through the Gateway, `./deploy.sh slo` done
  * kubeconfig + terraform state in WSL (gcloud/kubectl/terraform/helm run there on Windows)
  * Chrome started with --remote-debugging-port=9222 and signed in to the Cloud console
  * DEMO_EXTRA_PATH with gcloud and ffmpeg; Python deps for postmortem/ (google-auth, requests) in WSL

  python -u demo/scenes.py

The take creates two real releases (one healthy, one with FAIL_RATE=0.5) and waits for Cloud Deploy, so it runs ~20 minutes.
  Rule: nothing slow runs inside a scene. Commands run (and waits happen) BEFORE each scene; the scene types the
  commands and pastes the captured output, paced over the narration. Waiting between scenes is cut by the recorder.
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


def capture_steps(steps) -> list[tuple[str, str]]:
    """Run the producers now, before the scene, so the scene only types and pastes.

    A producer that runs inside a scene leaves the narration playing over an empty terminal, and a
    blocking call on the event loop stalls the screencast (frames are acked from the loop)."""
    return [(cmd, (producer() if callable(producer) else producer)) for cmd, producer in steps]


async def precapture(steps) -> list[tuple[str, str]]:
    return await asyncio.to_thread(capture_steps, steps)


async def replay(page, steps, spread: float, type_delay: int = 5, hold: float = 2.0) -> None:
    """Type the captured commands and paste their outputs, paced across about `spread` seconds."""
    n = max(len(steps), 1)
    pause = max(0.4, min(3.0, spread / n / 2))
    for cmd, out in steps:
        await type_cmd(page, cmd, type_delay)
        await asyncio.sleep(pause)
        await page.evaluate("(o) => window.__output(o)", out)
        await asyncio.sleep(pause)
    await asyncio.sleep(hold)


async def poll(check, every: float = 5.0, tries: int = 60) -> bool:
    """Wait, off the event loop and outside any scene, until check() is true."""
    for _ in range(tries):
        if await asyncio.to_thread(check):
            return True
        await asyncio.sleep(every)
    return False


async def console_ready(tab, text: str, reload: bool = True) -> None:
    """Refresh a pre-loaded console tab and wait until it has rendered, before the scene that shows it."""
    try:
        if reload:
            await tab.reload(wait_until="commit")
        await tab.wait_for_selector(f"text={text}", timeout=60000)
        await asyncio.sleep(2.5)
    except Exception as exc:
        print(f"  ! console tab not ready ({text}): {type(exc).__name__}")


def retry_nonempty(producer, tries: int = 3) -> str:
    """A WSL call occasionally returns nothing while another WSL process (a build) is running; retry."""
    out = ""
    for _ in range(tries):
        out = producer()
        if out.strip():
            return out
    return out or "(no output)"


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
        "Game day two, while a bad release builds. Chaos Mesh adds three hundred milliseconds to every packet leaving production's pods for two minutes, the datastore round trip and the response path included. "
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
    alen = {k: (await demo._tts(v))[1] for k, v in NARRATION.items()}  # seconds of narration per scene, for pacing

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
        await page.bring_to_front()  # the tab pre-loads left another tab in front; a hidden tab emits no screencast frames
        await page.goto((ROOT / "docs" / "architecture.html").as_uri(), wait_until="commit")
        await page.wait_for_selector(".canvas", timeout=15000)
        # animations off for the recording (a continuously repainting page floods the CDP screencast),
        # then zoom the 1400x800 canvas to the 1920x1080 frame and verify it took: take 3 recorded it unzoomed
        width = 0.0
        for _ in range(6):
            width = await page.evaluate(
                "() => { if (!document.getElementById('demo-still')) { const st = document.createElement('style'); st.id = 'demo-still'; st.textContent = '*{animation:none!important}'; document.head.appendChild(st); }"
                " document.body.style.zoom = '1.33'; document.body.style.margin = '0';"
                " return document.querySelector('.canvas').getBoundingClientRect().width; }")
            if width > 1800:
                break
            await asyncio.sleep(0.5)
        print(f"  architecture canvas width after zoom: {width:.0f}px")
    await demo.scene("Architecture", NARRATION["arch"], s1)

    # ---- 2 Binary Authorization ----
    steps2 = await precapture([
        ("kubectl -n prod apply -f chaos/unsigned-pod.yaml   # compliant pod, un-attested image",
         lambda: wsl("kubectl -n prod apply -f chaos/unsigned-pod.yaml 2>&1 | fold -s -w 150; kubectl -n prod delete pod unsigned-probe --ignore-not-found >/dev/null 2>&1") or "(no output)"),
        ("kubectl -n prod get deploy orders-api -o jsonpath='{.spec.template.spec.containers[0].image}'", running_img),
        (f"gcloud beta container binauthz attestations list --attestor {attestor} --artifact-url \"$IMAGE\" --format='value(name,attestation.serializedPayload.len())'",
         lambda: wsl(f"gcloud beta container binauthz attestations list --project {PROJECT} --attestor {attestor} --attestor-project {PROJECT} --artifact-url '{running_img}' --format='value(name)' 2>&1 | head -2")),
        ("gcloud container binauthz policy export | grep -v namePattern", policy),
        (f"gcloud container clusters describe delivery --zone {ZONE} --format='value(binaryAuthorization.evaluationMode,privateClusterConfig.enablePrivateNodes,workloadIdentityConfig.workloadPool)'", cluster),
    ])

    await demo.terminal("Guardrail 1: Binary Authorization admits attested digests only", [])  # before the scene: a slow about:blank hop must not eat narration time
    await asyncio.sleep(0.8)
    async def s2():
        await replay(page, steps2, spread=alen["binauthz"] * 0.6)
    await demo.scene("Binary Authorization", NARRATION["binauthz"], s2, hold=1.0)

    # ---- 3 game day 1: pod-kill (the experiment runs now; the scene replays it) ----
    steps3 = await precapture([
        ("kubectl -n prod get pods -l app=orders-api", lambda: wsl("kubectl -n prod get pods -l app=orders-api")),
        ("kubectl apply -f chaos/pod-kill.yaml", lambda: wsl("kubectl apply -f chaos/pod-kill.yaml")),
        ("sleep 8; kubectl -n prod get pods -l app=orders-api", lambda: wsl("sleep 8; kubectl -n prod get pods -l app=orders-api")),
        ("for i in $(seq 1 30); do curl -s -o /dev/null -w '%{http_code}\\n' -X POST $GW/orders -d '{\"item\":\"gameday\",\"qty\":1}'; done | sort | uniq -c",
         lambda: wsl(f"for i in $(seq 1 30); do curl -s --max-time 10 -o /dev/null -w '%{{http_code}}\\n' -X POST {api}/orders -H 'content-type: application/json' -d '{{\"item\":\"gameday-'$i'\",\"qty\":1}}'; done | sort | uniq -c", timeout=400)),
        ("kubectl -n prod get pdb,hpa", lambda: wsl("kubectl -n prod get pdb,hpa; kubectl delete -f chaos/pod-kill.yaml >/dev/null 2>&1")),
    ])

    await demo.terminal("Game day 1: Chaos Mesh pod-kill on prod", [])  # before the scene: a slow about:blank hop must not eat narration time
    await asyncio.sleep(0.8)
    async def s3():
        await replay(page, steps3, spread=alen["podkill"] * 0.65)
    await demo.scene("Game day 1 pod-kill", NARRATION["podkill"], s3, hold=1.0)

    # ---- 4 the release ----
    if not good_task.done():
        try:
            await asyncio.wait_for(good_task, timeout=600)
        except Exception:
            pass
    rel = good.get("name", "")
    steps4 = await precapture([
        (f"./deploy.sh release demo{stamp}", trim(good.get("log", "(release log missing)"), 1700)),
        (f"gcloud deploy releases describe {rel} --delivery-pipeline {PIPELINE} --format='value(name.basename(),buildArtifacts[0].image,deployParameters,renderState)'",
         lambda: gd(f"releases describe {rel} --delivery-pipeline {PIPELINE} --format='value(name.basename(),buildArtifacts[0].image,deployParameters,renderState)'")),
    ])

    await demo.terminal("A release: build → sign → attest → Cloud Deploy", [])  # before the scene: a slow about:blank hop must not eat narration time
    await asyncio.sleep(0.8)
    async def s4():
        await replay(page, steps4, spread=alen["release"] * 0.5)
    await demo.scene("Release", NARRATION["release"], s4, hold=1.0)

    # ---- 5 pipeline in the console (refreshed before the scene, shown as-is during it) ----
    if use_cdp:
        await console_ready(pipe_tab, "orders-api")

        async def s5():
            await demo.capture(pipe_tab)
        await demo.scene("Pipeline", NARRATION["pipeline"], s5, hold=1.5)
        await demo.capture(page)

    # ---- 6 prod canary: wait for the canary phase first, then replay the evidence ----
    def prod_in_canary() -> bool:
        st = rollouts(rel)
        return "prod" in st and ("IN_PROGRESS" in st.split("prod")[-1] or "SUCCEEDED" in st.split("prod")[-1])

    def canary_has_weight() -> bool:
        w = wsl("kubectl -n prod get httproute orders-api -o jsonpath='{range .spec.rules[0].backendRefs[*]}{.name}={.weight} {end}'")
        ready = wsl("kubectl -n prod get deploy orders-api-canary -o jsonpath='{.status.availableReplicas}' 2>/dev/null")
        return "canary" in w and ready not in ("", "0")

    await poll(prod_in_canary, 5, 60)
    await poll(canary_has_weight, 5, 60)
    steps6 = await precapture([
        (f"gcloud deploy rollouts list --delivery-pipeline {PIPELINE} --release {rel}", lambda: rollouts(rel)),
        ("kubectl -n prod get httproute orders-api -o jsonpath='{range .spec.rules[0].backendRefs[*]}{.name}={.weight}{\"\\n\"}{end}'",
         lambda: wsl("kubectl -n prod get httproute orders-api -o jsonpath='{range .spec.rules[0].backendRefs[*]}{.name}={.weight}{\"\\n\"}{end}'")),
        ("kubectl -n prod get deploy,svc,pods -L app | grep -E 'NAME|orders'", lambda: wsl("kubectl -n prod get deploy,svc,pods -L app 2>&1 | grep -E 'NAME|orders' | cut -c1-130")),
        ("kubectl -n prod get jobs,pods -l app=verify   # the verify Job Cloud Deploy runs in-cluster", lambda: wsl("kubectl -n prod get jobs,pods -l app=verify 2>&1 | tail -4")),
    ])

    await demo.terminal("Prod canary: 25% → 50% → 100% on Gateway API weights", [])  # before the scene: a slow about:blank hop must not eat narration time
    await asyncio.sleep(0.8)
    async def s6():
        await replay(page, steps6, spread=alen["canary"] * 0.6)
    await demo.scene("Prod canary", NARRATION["canary"], s6, hold=1.0)

    # --- the bad release builds in the background during scene 7 (~3 min) ---
    bad: dict = {}

    def do_bad_release():
        out = wsl(f"./deploy.sh release bad{stamp} 0.5 2>&1 | tail -6", timeout=900)
        bad["log"] = out
        bad["name"] = out.strip().splitlines()[-1].strip() if out.strip() else ""
    bad_task = asyncio.ensure_future(asyncio.to_thread(do_bad_release))

    # ---- 7 game day 2: network delay (runs now) + dashboard ----
    steps7 = await precapture([
        ("kubectl apply -f chaos/network-delay.yaml", lambda: wsl("kubectl apply -f chaos/network-delay.yaml")),
        ("for i in $(seq 1 10); do curl -s -o /dev/null -w '%{http_code} %{time_total}s\\n' -X POST $GW/orders -d '{\"item\":\"slow\",\"qty\":1}'; done",
         lambda: retry_nonempty(lambda: wsl(f"sleep 5; for i in $(seq 1 10); do curl -s --max-time 10 -o /dev/null -w '%{{http_code}} %{{time_total}}s\\n' -X POST {api}/orders -H 'content-type: application/json' -d '{{\"item\":\"slow-'$i'\",\"qty\":1}}'; done", timeout=200))),
    ])
    if use_cdp:
        await asyncio.sleep(45)  # let the slow requests reach the dashboard's 1-minute points
        await console_ready(dash_tab, "Requests/s")

    await demo.terminal("Game day 2: +300 ms egress delay on prod (NetworkChaos)", [])  # before the scene: a slow about:blank hop must not eat narration time
    await asyncio.sleep(0.8)
    async def s7():
        await replay(page, steps7, spread=8.0, hold=4.0)
        if use_cdp:
            await demo.capture(dash_tab)
    await demo.scene("Game day 2 network delay", NARRATION["netdelay"], s7, hold=1.5)
    if use_cdp:
        await demo.capture(page)

    # ---- 8 the bad release is caught: wait for the failure + rollback first ----
    if not bad_task.done():
        try:
            await asyncio.wait_for(bad_task, timeout=600)
        except Exception:
            pass
    badrel = bad.get("name", "")
    await poll(lambda: "FAILED" in gd(f"rollouts list --delivery-pipeline {PIPELINE} --release {badrel} --format='value(state)'"), 5, 48)
    await asyncio.sleep(25)  # the repair automation needs a moment to start the rollback
    steps8 = await precapture([
        (f"./deploy.sh release bad{stamp} 0.5", trim(bad.get("log", ""), 600)),
        (f"gcloud deploy releases describe {badrel} --delivery-pipeline {PIPELINE} --format='value(deployParameters)'",
         lambda: gd(f"releases describe {badrel} --delivery-pipeline {PIPELINE} --format='value(deployParameters)'")),
        ("kubectl -n dev get pods -l app=orders-api   # healthy to kubelet", lambda: wsl("kubectl -n dev get pods -l app=orders-api")),
        (f"gcloud deploy rollouts list --delivery-pipeline {PIPELINE} --release {badrel} --format='table(name.basename(),targetId,state,phases[0].deploymentJobs.verifyJob.state)'",
         lambda: gd(f"rollouts list --delivery-pipeline {PIPELINE} --release {badrel} --format='table(name.basename(),targetId,state,phases[0].deploymentJobs.verifyJob.state:label=VERIFY)'")),
        (f"gcloud deploy rollouts list --delivery-pipeline {PIPELINE} --release {rel} --format='table(name.basename(),targetId,state,rollbackOfRollout.basename())'   # the automation's rollback",
         lambda: gd(f"rollouts list --delivery-pipeline {PIPELINE} --release {rel} --format='table(name.basename(),targetId,state,rollbackOfRollout.basename():label=ROLLBACK_OF)'")),
        (f"gcloud deploy automation-runs list --delivery-pipeline {PIPELINE} --format='table(ruleId,state,createTime.date(\"%H:%M\"))' --limit 3",
         lambda: gd(f"automation-runs list --delivery-pipeline {PIPELINE} --format='table(ruleId,state,createTime.date(\"%H:%M\"))' --limit 3")),
        ("kubectl -n dev get deploy orders-api -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"FAIL_RATE\")].value}'   # back to the previous release",
         lambda: wsl("kubectl -n dev get deploy orders-api -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"FAIL_RATE\")].value}'; echo")),
    ])

    await demo.terminal("The bad release: FAIL_RATE=0.5, caught by verify, rolled back", [])  # before the scene: a slow about:blank hop must not eat narration time
    await asyncio.sleep(0.8)
    async def s8():
        await replay(page, steps8, spread=alen["bad"] * 0.7)
    await demo.scene("Bad release caught", NARRATION["bad"], s8, hold=1.0)

    # ---- 9 SLOs ----
    if use_cdp:
        await console_ready(slo_tab, "/orders")

        async def s9():
            await demo.capture(slo_tab)
        await demo.scene("SLOs", NARRATION["slo"], s9, hold=1.5)
        await demo.capture(page)

    # ---- 10 postmortem (Gemini runs now; the scene replays the draft) ----
    steps10 = await precapture([
        ("bq ls incidents", lambda: wsl(f"bq ls --project_id={PROJECT} incidents 2>&1 | head -12")),
        (f"python3 postmortem/postmortem.py --last 30m --title 'bad release bad{stamp} caught by verify; chaos game day'",
         lambda: trim(wsl(f"~/venvs/p5/bin/python postmortem/postmortem.py --last 30m --title 'bad release bad{stamp} caught by verify; chaos game day' 2>&1 | head -40", timeout=300), 2200)),
    ])

    await demo.terminal("Learning loop: logs → BigQuery → Gemini → postmortem draft", [])  # before the scene: a slow about:blank hop must not eat narration time
    await asyncio.sleep(0.8)
    async def s10():
        await replay(page, steps10, spread=6.0, hold=4.0)
    await demo.scene("Postmortem", NARRATION["postmortem"], s10, hold=1.0)

    # ---- 11 wrap ----
    steps11 = await precapture([
        ("terraform -chdir=terraform state list | wc -l", lambda: wsl("terraform -chdir=terraform state list | wc -l")),
        ("terraform -chdir=terraform output", lambda: wsl("terraform -chdir=terraform output -no-color")),
        ("ls postmortems/", lambda: wsl("ls postmortems/ 2>/dev/null; kubectl delete -f chaos/ >/dev/null 2>&1; true")),
        ("# when done: ./teardown.sh", "namespaces + chaos → terraform destroy → load-balancer leftovers → destroy again → billing audit"),
    ])

    await demo.terminal("Infrastructure as code + cleanup", [])  # before the scene: a slow about:blank hop must not eat narration time
    await asyncio.sleep(0.8)
    async def s11():
        await replay(page, steps11, spread=alen["wrap"] * 0.5)
    await demo.scene("Wrap-up", NARRATION["wrap"], s11, hold=1.5)

    await demo.finish("demo")


if __name__ == "__main__":
    asyncio.run(main())
