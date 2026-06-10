"""Generate "batch 2" of synthetic AI Gateway usage data and ship it.

Adds ~250 NEW rows dated over the last 3 days on top of the original batch:
  - one NEW latent pattern ("privacy_dsr": GDPR/CCPA data-subject-request help),
  - ~170 more rows across the existing 8 pattern briefs,
  - ~30 noise rows.
Appends to app/data/gateway_usage.json (preserving batch 1), INSERTs into the
existing UC table (no recreate), then replays only the new rows through the
AI Gateway so they land in the endpoint's real inference table.
"""
import json
import random
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests

from common import CATALOG, GATEWAY_URL, SCHEMA, chat_json, run_sql, sql_str, token
from promptgen import generate_prompts

random.seed(43)
OUT = Path(__file__).parent.parent / "app" / "data"
USAGE = OUT / "gateway_usage.json"

# NEW latent pattern for batch 2: privacy / data-subject-request workflows.
NEW_PATTERN = (
    "privacy_dsr", ["support", "pm", "manager"],
    "asking an LLM for help handling GDPR/CCPA data-subject requests: drafting responses to "
    "deletion or access requests, building DSAR fulfillment checklists across systems (CRM, "
    "warehouse, email, backups), right-to-be-forgotten verification steps, and privacy-review "
    "summaries of new features. Vary regulation (GDPR, CCPA/CPRA), request type, and deadlines.",
    50)

# Existing latent patterns: (key, persona pool, generator brief, batch-2 count).
# Briefs copied verbatim from generate_usage.py; counts scaled proportionally.
PATTERNS = [
    ("nl2sql", ["analyst", "pm"],
     "asking an LLM to write a SQL query against tables like orders, customers, sensor_events, "
     "revenue by region/month, churn cohorts. Vary phrasing, dialect mentions (Databricks SQL, Trino), "
     "column names, and sloppiness.", 32),
    ("incident_rca", ["sre", "engineer"],
     "asking an LLM to draft a root-cause-analysis or incident summary from pasted notes: outage "
     "timeline, impact, mitigation, action items. Vary incident types (latency spike, OOM, cert expiry, "
     "bad deploy, Kafka lag).", 25),
    ("support_reply", ["support"],
     "asking an LLM to draft a polite customer reply from a ticket description: refunds, login issues, "
     "API errors, feature requests, angry customers. Vary tone instructions.", 28),
    ("pr_summary", ["engineer"],
     "asking an LLM to summarize a code diff / PR for reviewers: what changed, risk, test coverage. "
     "Vary languages (Python, Go, Terraform) and detail levels.", 21),
    ("ticket_triage", ["support", "engineer"],
     "asking an LLM to classify/triage a Jira or support ticket: severity, team routing, dedupe "
     "against known issues, write a one-line summary.", 21),
    ("meeting_actions", ["pm", "manager"],
     "asking an LLM to turn pasted meeting notes into action items with owners and due dates, or a "
     "crisp recap email. Vary meeting types (standup, QBR, design review).", 17),
    ("doc_summarize", ["analyst", "pm", "manager", "engineer"],
     "asking an LLM to summarize a long document: design doc, vendor contract, research paper, "
     "runbook — into N bullets or an executive summary.", 15),
    ("log_regex", ["engineer", "sre"],
     "asking an LLM for help parsing logs or writing a regex/grok pattern: extract fields from nginx "
     "or app logs, explain a gnarly regex, convert to another flavor.", 11),
]
NOISE_BRIEF = ("one-off miscellaneous workplace prompts with NO common pattern: trivia, a poem for a "
               "coworker's farewell, travel question, unit conversion, random coding question, recipe.")
NOISE_N = 30

USERS = {
    "analyst":  [f"{n}@acme-corp.com" for n in ("maya.patel", "jon.weiss", "li.chen", "sara.kim", "omar.haddad", "tessa.brooks")],
    "engineer": [f"{n}@acme-corp.com" for n in ("dev.rao", "kate.olson", "mikhail.petrov", "ana.souza", "chris.doyle", "yuki.mori", "sam.akin", "nina.vogel")],
    "sre":      [f"{n}@acme-corp.com" for n in ("pat.murphy", "zoe.adler", "raj.iyer", "elena.diaz")],
    "support":  [f"{n}@acme-corp.com" for n in ("liam.walsh", "ivy.tran", "max.berg", "rosa.flores", "ken.ito")],
    "pm":       [f"{n}@acme-corp.com" for n in ("amy.ford", "tom.nagy", "lucia.rossi", "dan.okafor")],
    "manager":  [f"{n}@acme-corp.com" for n in ("ruth.lang", "vic.osei", "hana.sato")],
}
ENDPOINTS = ["databricks-claude-sonnet-4-6", "databricks-claude-haiku-4-5", "databricks-gpt-oss-120b"]
START = datetime(2026, 6, 7, 0, 0, 0)
END = datetime(2026, 6, 10, 17, 0, 0)
SPAN_S = (END - START).total_seconds()

REPLAY_MODEL = "databricks-claude-haiku-4-5"
WORKERS = 8


def gen_variations(brief: str, n: int) -> list[str]:
    return generate_prompts(brief, n)


def make_row(prompt: str, key: str, personas: list[str] | None) -> dict:
    pool = ([u for pe in personas for u in USERS[pe]] if personas
            else [u for us in USERS.values() for u in us])
    ts = END - timedelta(seconds=random.uniform(0, SPAN_S))
    return {
        "request_id": str(uuid.uuid4()), "event_time": ts.isoformat(sep=" ", timespec="seconds"),
        "user_email": random.choice(pool), "endpoint_name": random.choice(ENDPOINTS),
        "prompt": prompt, "input_tokens": max(20, len(prompt) // 4 + random.randint(50, 400)),
        "output_tokens": random.randint(150, 900), "latent_pattern": key,
    }


def replay(rows: list[dict]) -> tuple[int, int]:
    """Replay only the new rows through the AI Gateway (same approach as
    replay_to_gateway.py: tiny max_tokens, thread pool, count ok/fail)."""

    def send(row):
        r = requests.post(
            f"{GATEWAY_URL}/chat/completions",
            headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
            json={"model": REPLAY_MODEL, "messages": [{"role": "user", "content": row["prompt"]}],
                  "max_tokens": 16},
            timeout=120,
        )
        return row["request_id"], r.status_code

    print(f"replaying {len(rows)} NEW prompts through {GATEWAY_URL} ({WORKERS} workers)")
    ok = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(send, r) for r in rows]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                _, code = f.result()
                ok += 1 if code == 200 else 0
                fail += 0 if code == 200 else 1
            except Exception:
                fail += 1
            if i % 25 == 0:
                print(f"  {i}/{len(rows)} (ok={ok} fail={fail}, {time.time()-t0:.0f}s)")
    print(f"replay done: ok={ok} fail={fail} in {time.time()-t0:.0f}s")
    print("NOTE: inference-table writes are batched — expect 10-30 min before rows appear.")
    return ok, fail


def main():
    # 1) Generate batch-2 rows.
    new_rows = []
    counts = {}
    for key, personas, brief, n in [NEW_PATTERN] + PATTERNS:
        prompts = gen_variations(brief, n)
        print(f"  pattern {key}: {len(prompts)} prompts")
        counts[key] = len(prompts)
        new_rows += [make_row(p, key, personas) for p in prompts]
    noise = gen_variations(NOISE_BRIEF, NOISE_N)
    print(f"  noise: {len(noise)} prompts")
    counts["noise"] = len(noise)
    new_rows += [make_row(p, "noise", None) for p in noise]
    random.shuffle(new_rows)
    print(f"generated {len(new_rows)} new rows: {counts}")

    # 2) Append to app/data/gateway_usage.json (keep batch 1 intact).
    existing = json.loads(USAGE.read_text())
    before = len(existing)
    existing.extend(new_rows)
    USAGE.write_text(json.dumps(existing, indent=1))
    print(f"appended {len(new_rows)} rows -> gateway_usage.json ({before} -> {len(existing)})")

    # 3) INSERT into the existing UC table (no recreate).
    print("inserting into UC ...")
    B = 50
    for i in range(0, len(new_rows), B):
        vals = ",".join(
            f"({sql_str(r['request_id'])}, TIMESTAMP{sql_str(r['event_time'])}, {sql_str(r['user_email'])}, "
            f"{sql_str(r['endpoint_name'])}, {sql_str(r['prompt'])}, {r['input_tokens']}, {r['output_tokens']})"
            for r in new_rows[i:i + B])
        run_sql(f"INSERT INTO {CATALOG}.{SCHEMA}.gateway_usage VALUES {vals}")
        print(f"  inserted {min(i + B, len(new_rows))}/{len(new_rows)}")

    # 4) Replay only the NEW rows through the AI Gateway.
    ok, fail = replay(new_rows)

    # 5) Verify total UC row count.
    cnt = run_sql(f"SELECT count(*) FROM {CATALOG}.{SCHEMA}.gateway_usage")
    print(f"UC table total rows: {cnt[0][0]}")
    print(f"SUMMARY: per-pattern={counts} | new_rows={len(new_rows)} | json_total={len(existing)} | "
          f"uc_total={cnt[0][0]} | replay ok={ok} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
