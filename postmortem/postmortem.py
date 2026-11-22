#!/usr/bin/env python3
"""Draft a blameless postmortem for a time window from the project's own evidence.

Sources (all read-only, all via the ambient credentials: ADC locally, the Cloud Run
job's service account in the cloud):
  * BigQuery dataset `incidents`  - the log sink: app logs, Cloud Deploy and Binary
    Authorization audit entries, Kubernetes events, Chaos Mesh
  * Cloud Monitoring PromQL API   - 5xx ratio and p95 for prod, per minute
  * Vertex AI Gemini              - turns the evidence into a structured draft

Usage:
  python postmortem/postmortem.py --last 30m                # window ending now
  python postmortem/postmortem.py --start 2026-11-23T10:00Z --end 2026-11-23T10:40Z
  python postmortem/postmortem.py --last 20m --title "Bad release bad-v3 caught by canary verify"
Writes postmortems/<UTC timestamp>.md and prints it. No client libraries: REST + google-auth.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import textwrap
import urllib.parse
from pathlib import Path

import google.auth
import google.auth.transport.requests
import requests

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "bj-progressive-delivery")
DATASET = os.environ.get("INCIDENTS_DATASET", "incidents")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
LOCATION = os.environ.get("GEMINI_LOCATION", "global")
OUT_DIR = Path(os.environ.get("POSTMORTEM_DIR", Path(__file__).resolve().parent.parent / "postmortems"))


def session() -> requests.Session:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {creds.token}"
    return s


# ----------------------------------------------------------------------------- BigQuery
def bq_query(s: requests.Session, sql: str) -> list[dict]:
    r = s.post(f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/queries",
               json={"query": sql, "useLegacySql": False, "timeoutMs": 60000, "maxResults": 500})
    r.raise_for_status()
    d = r.json()
    if not d.get("jobComplete", True):
        raise RuntimeError("BigQuery query did not complete in time")
    fields = [f["name"] for f in d.get("schema", {}).get("fields", [])]
    return [dict(zip(fields, [c.get("v") for c in row["f"]])) for row in d.get("rows", [])]


def log_tables(s: requests.Session) -> list[str]:
    rows = bq_query(s, f"SELECT table_name FROM `{PROJECT}.{DATASET}.INFORMATION_SCHEMA.TABLES` ORDER BY table_name")
    return [r["table_name"] for r in rows]


def collect_logs(s: requests.Session, start: dt.datetime, end: dt.datetime, limit_per_table: int = 120) -> list[dict]:
    """Pull the most informative rows from every sink table in the window."""
    out: list[dict] = []
    for t in log_tables(s):
        # Each sink table has `timestamp`; the useful text is in one of several columns.
        sql = f"""
        SELECT timestamp, severity,
               COALESCE(
                 JSON_VALUE(TO_JSON_STRING(t), '$.jsonPayload.message'),
                 JSON_VALUE(TO_JSON_STRING(t), '$.jsonPayload.MESSAGE'),
                 JSON_VALUE(TO_JSON_STRING(t), '$.textPayload'),
                 JSON_VALUE(TO_JSON_STRING(t), '$.protopayload_auditlog.methodName'),
                 JSON_VALUE(TO_JSON_STRING(t), '$.jsonPayload.reason')
               ) AS message,
               JSON_VALUE(TO_JSON_STRING(t), '$.resource.type') AS rtype,
               COALESCE(JSON_VALUE(TO_JSON_STRING(t), '$.resource.labels.namespace_name'),
                        JSON_VALUE(TO_JSON_STRING(t), '$.resource.labels.pipeline_id'),
                        JSON_VALUE(TO_JSON_STRING(t), '$.resource.labels.target_id')) AS scope,
               JSON_VALUE(TO_JSON_STRING(t), '$.protopayload_auditlog.status.message') AS audit_status
        FROM `{PROJECT}.{DATASET}.{t}` t
        WHERE timestamp BETWEEN TIMESTAMP('{start.isoformat()}') AND TIMESTAMP('{end.isoformat()}')
          AND (severity IN ('WARNING','ERROR','CRITICAL','ALERT','EMERGENCY','NOTICE')
               OR JSON_VALUE(TO_JSON_STRING(t), '$.resource.type') LIKE 'clouddeploy%'
               OR JSON_VALUE(TO_JSON_STRING(t), '$.protopayload_auditlog.serviceName') IN ('clouddeploy.googleapis.com','binaryauthorization.googleapis.com')
               OR JSON_VALUE(TO_JSON_STRING(t), '$.jsonPayload.reason') IS NOT NULL)
        ORDER BY timestamp LIMIT {limit_per_table}"""
        try:
            for r in bq_query(s, sql):
                r["table"] = t
                out.append(r)
        except requests.HTTPError as e:  # a table without those columns: skip it
            print(f"  skip {t}: {e.response.text[:120]}", file=sys.stderr)
    out.sort(key=lambda r: r["timestamp"])
    return out


# ----------------------------------------------------------------------------- Monitoring (PromQL)
def promql_range(s: requests.Session, query: str, start: dt.datetime, end: dt.datetime, step: str = "60s") -> list[tuple[str, float]]:
    url = f"https://monitoring.googleapis.com/v1/projects/{PROJECT}/location/global/prometheus/api/v1/query_range"
    r = s.get(url, params={"query": query, "start": start.isoformat(), "end": end.isoformat(), "step": step})
    r.raise_for_status()
    res = r.json().get("data", {}).get("result", [])
    if not res:
        return []
    return [(dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime("%H:%M"), float(v)) for ts, v in res[0]["values"] if v not in ("NaN", "+Inf")]


def collect_metrics(s: requests.Session, start: dt.datetime, end: dt.datetime) -> dict[str, list[tuple[str, float]]]:
    q = {
        "prod 5xx ratio": 'sum(rate(http_requests_total{namespace="prod",route="/orders",code=~"5.."}[2m])) / sum(rate(http_requests_total{namespace="prod",route="/orders"}[2m]))',
        "prod p95 latency s": 'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{namespace="prod",route="/orders"}[2m])))',
        "prod ready pods": 'count(up{namespace="prod"})',
        "faults injected/s": 'sum(rate(faults_injected_total[2m]))',
    }
    out = {}
    for name, query in q.items():
        try:
            out[name] = promql_range(s, query, start, end)
        except requests.HTTPError as e:
            print(f"  metrics {name}: {e.response.text[:120]}", file=sys.stderr)
            out[name] = []
    return out


# ----------------------------------------------------------------------------- Gemini
PROMPT = """You are an SRE writing a blameless postmortem draft for a small Google Cloud platform
(orders-api on GKE, delivered by Cloud Deploy with canary verification and Binary Authorization,
with Chaos Mesh game days). Use ONLY the evidence below. Where evidence is missing, say so plainly
instead of inventing. Keep it tight and specific. Output Markdown with exactly these sections:

# Postmortem: <one-line title>
## Summary (3 sentences max)
## Impact (what users/SLOs saw, with numbers from the metrics)
## Timeline (UTC, bullet per event, from the logs; include Cloud Deploy, verify, rollback, chaos, admission denials)
## Detection (what signal caught it and how long it took)
## Root cause (technical, blameless)
## What went well
## What went poorly
## Action items (table: action | owner role | priority | type: prevent/detect/mitigate)
## Open questions

Window: {start} to {end} (UTC). Title hint: {title}

=== METRICS (per minute) ===
{metrics}

=== LOG EVENTS (chronological, trimmed) ===
{logs}
"""


def gemini(s: requests.Session, prompt: str) -> str:
    host = "https://aiplatform.googleapis.com" if LOCATION == "global" else f"https://{LOCATION}-aiplatform.googleapis.com"
    url = f"{host}/v1/projects/{PROJECT}/locations/{LOCATION}/publishers/google/models/{MODEL}:generateContent"
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096, "thinkingConfig": {"thinkingLevel": "LOW"}}}
    r = s.post(url, json=body, timeout=120)
    r.raise_for_status()
    d = r.json()
    return "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])


# ----------------------------------------------------------------------------- main
def parse_when(v: str) -> dt.datetime:
    return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", help="window length ending now, e.g. 30m or 2h")
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--title", default="incident")
    ap.add_argument("--dry-run", action="store_true", help="collect evidence, print it, skip Gemini")
    a = ap.parse_args()
    end = parse_when(a.end) if a.end else dt.datetime.now(dt.timezone.utc)
    if a.last:
        n, unit = int(a.last[:-1]), a.last[-1]
        start = end - dt.timedelta(minutes=n if unit == "m" else n * 60)
    else:
        start = parse_when(a.start)

    s = session()
    print(f"window {start:%Y-%m-%dT%H:%M}Z → {end:%Y-%m-%dT%H:%M}Z", file=sys.stderr)
    metrics = collect_metrics(s, start, end)
    logs = collect_logs(s, start, end)
    print(f"evidence: {len(logs)} log events, {sum(len(v) for v in metrics.values())} metric points", file=sys.stderr)

    metrics_txt = "\n".join(f"{k}: " + ", ".join(f"{t}={v:.3g}" for t, v in pts) for k, pts in metrics.items()) or "(no metric data)"
    log_lines = []
    for r in logs[:400]:
        msg = (r.get("message") or r.get("audit_status") or "").replace("\n", " ")
        log_lines.append(f"{r['timestamp'][:19]} [{r.get('severity') or '-'}] {r.get('rtype') or r['table']}/{r.get('scope') or '-'}: {msg[:220]}")
    logs_txt = "\n".join(log_lines) or "(no log events matched in the window)"

    if a.dry_run:
        print(metrics_txt); print(logs_txt)
        return 0
    draft = gemini(s, PROMPT.format(start=start.isoformat(), end=end.isoformat(), title=a.title, metrics=metrics_txt, logs=logs_txt))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", a.title.lower()).strip("-")[:40] or "incident"
    out = OUT_DIR / f"{end:%Y%m%dT%H%M}Z-{slug}.md"
    header = textwrap.dedent(f"""\
        <!-- generated by postmortem/postmortem.py · model {MODEL} · window {start.isoformat()} → {end.isoformat()} ·
             evidence: {len(logs)} log events from BigQuery `{DATASET}`, {len(metrics)} metric series from Cloud Monitoring.
             This is a DRAFT for humans to correct; the facts come from the logs, the prose from the model. -->
        """)
    out.write_text(header + draft.strip() + "\n", encoding="utf-8")
    print(draft)
    print(f"\nwrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
