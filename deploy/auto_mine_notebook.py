# Databricks notebook source
# MAGIC %md
# MAGIC # skillforge-auto-mine
# MAGIC Scheduled re-mining of the live AI Gateway inference data, writing a
# MAGIC time-series snapshot row into `skillforge.core.mining_history`.
# MAGIC
# MAGIC Self-contained: uses the notebook's own credentials for BOTH the SQL
# MAGIC Statements API and the FMAPI AI Gateway (no PATs, no repo checkout).
# MAGIC Runs on serverless compute. Mirrors pipeline/mine_and_design.py.

# COMMAND ----------

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone

import requests

# --- config ---------------------------------------------------------------
CATALOG = "skillforge"
SCHEMA = "core"
HISTORY_TABLE = f"{CATALOG}.{SCHEMA}.mining_history"
MAPPING_TABLE = f"{CATALOG}.{SCHEMA}.gateway_usage"
RECS_TABLE = f"{CATALOG}.{SCHEMA}.recommended_skills"
PAYLOAD_TABLE_SQL = "`skillforge_inference`.`feeds`.`databricks-claude-haiku-4-5_payload`"
SOURCE_LABEL = "skillforge_inference.feeds.databricks-claude-haiku-4-5_payload"
WAREHOUSE_ID = "bd926c5277db7d1d"
WINDOW_DAYS = 14
SAVINGS_PCT = 30
SONNET = "databricks-claude-sonnet-4-6"
HAIKU = "databricks-claude-haiku-4-5"

# Notebook context: derive workspace host + a short-lived API token from the
# notebook's own identity. These power both the Statements API and FMAPI.
ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
HOST = ctx.apiUrl().get().rstrip("/")
TOKEN = ctx.apiToken().get()
WORKSPACE_ID = ctx.workspaceId().get()
GATEWAY_URL = f"https://{WORKSPACE_ID}.ai-gateway.cloud.databricks.com/mlflow/v1"
HDRS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

print(f"host={HOST} workspace={WORKSPACE_ID}")

# COMMAND ----------

def run_sql(statement, wait="50s"):
    r = requests.post(
        f"{HOST}/api/2.0/sql/statements",
        headers=HDRS,
        json={"warehouse_id": WAREHOUSE_ID, "statement": statement, "wait_timeout": wait},
        timeout=120,
    )
    d = r.json()
    state = d.get("status", {}).get("state")
    while state in ("PENDING", "RUNNING"):
        time.sleep(3)
        sid = d["statement_id"]
        d = requests.get(f"{HOST}/api/2.0/sql/statements/{sid}", headers=HDRS, timeout=60).json()
        state = d.get("status", {}).get("state")
    if state != "SUCCEEDED":
        raise RuntimeError(f"SQL {state}: {d.get('status',{}).get('error',{}).get('message','')[:300]}")
    return d.get("result", {}).get("data_array") or []


def sql_str(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def chat(messages, model=SONNET, max_tokens=4096, temperature=1.0, retries=3):
    for attempt in range(retries):
        r = requests.post(
            f"{GATEWAY_URL}/chat/completions",
            headers=HDRS,
            json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
            timeout=180,
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        if r.status_code in (429, 500, 502, 503) and attempt < retries - 1:
            time.sleep(5 * (attempt + 1))
            continue
        raise RuntimeError(f"FMAPI {r.status_code}: {r.text[:300]}")


def robust_json(messages, retries=3, **kw):
    for attempt in range(retries):
        text = chat(messages, **kw).strip()
        start, end = text.find("{"), text.rfind("}")
        try:
            if start == -1 or end <= start:
                raise json.JSONDecodeError("no JSON object", text, 0)
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            if attempt == retries - 1:
                raise
            kw["max_tokens"] = int(kw.get("max_tokens", 4096) * 1.5)

# COMMAND ----------

# --- step 1: extract corpus from the live inference payload table ----------
try:
    rows_raw = run_sql(
        f"SELECT request_id, request, event_time FROM {PAYLOAD_TABLE_SQL} "
        "WHERE status_code = 200 ORDER BY event_time"
    )
except Exception:
    rows_raw = run_sql(
        f"SELECT request_id, request, request_time FROM {PAYLOAD_TABLE_SQL} "
        "WHERE status_code = 200 ORDER BY request_time"
    )
print(f"{len(rows_raw)} payload rows")

prompts = {}
for req_id, request_json, req_time in rows_raw:
    try:
        req = json.loads(request_json)
        user_msgs = [m for m in req.get("messages", []) if m.get("role") == "user"]
        if user_msgs:
            prompts[user_msgs[-1]["content"]] = (req_id, str(req_time))
    except (json.JSONDecodeError, TypeError, KeyError):
        continue

mapping = run_sql(
    f"SELECT request_id, event_time, user_email, endpoint_name, prompt, "
    f"input_tokens, output_tokens FROM {MAPPING_TABLE}"
)
rows = []
for req_id, event_time, user, endpoint, prompt, in_tok, out_tok in mapping:
    if prompt not in prompts:
        continue
    rows.append({
        "request_id": prompts[prompt][0], "event_time": str(event_time),
        "user_email": user, "endpoint_name": endpoint, "prompt": prompt,
        "input_tokens": int(in_tok), "output_tokens": int(out_tok),
        "latent_pattern": "unknown",
    })
n = len(rows)
print(f"corpus: {n} prompts matched")
if n < 200:
    raise RuntimeError(f"only {n} prompts matched — inference batching may not have flushed; aborting before snapshot")

# COMMAND ----------

# --- step 2: mine recurring patterns via FMAPI -----------------------------
CLUSTER_INSTRUCTIONS = (
    "You are analyzing LLM prompts from an AI Gateway usage log to find recurring "
    "patterns that could be consolidated into reusable prompt templates (\"skills\").\n\n"
    "Cluster the numbered prompts below into recurring patterns:\n"
    "- A pattern needs >= 8 prompts that share the same underlying task/intent.\n"
    "- Leave genuine one-offs unclustered.\n"
    "- Every index must appear at most once (no index in two clusters).\n\n"
    "Return ONLY JSON, no prose:\n"
    '{"clusters": [{"name": "...", "description": "...", "prompt_indices": [0, 5, ...]}], '
    '"unclustered": [indices...]}'
)


def numbered_block(idxs):
    return "\n".join(f"[{i}] {rows[i]['prompt']}" for i in idxs)


def ask_clusters(idxs):
    msg = CLUSTER_INSTRUCTIONS + "\nPROMPTS:\n" + numbered_block(idxs)
    return robust_json([{"role": "user", "content": msg}], model=SONNET, max_tokens=8000, temperature=0.2)


try:
    result = ask_clusters(list(range(n)))
except Exception as e:
    print(f"single-pass failed ({e}); two-pass")
    half = n // 2
    a = ask_clusters(list(range(half)))
    b = ask_clusters(list(range(half, n)))
    merge_msg = (
        "Two halves of a prompt log were clustered separately. Merge equivalent "
        "clusters into single clusters, unioning their prompt_indices. Keep indices "
        "exactly. Return ONLY JSON: "
        '{"clusters":[{"name","description","prompt_indices"}],"unclustered":[...]}\n'
        f"HALF A: {json.dumps(a)}\nHALF B: {json.dumps(b)}"
    )
    result = robust_json([{"role": "user", "content": merge_msg}], model=SONNET, max_tokens=8000, temperature=0.2)

seen, clusters = set(), []
for c in result.get("clusters", []):
    idxs = []
    for i in c.get("prompt_indices", []):
        if isinstance(i, int) and 0 <= i < n and i not in seen:
            seen.add(i)
            idxs.append(i)
    if idxs:
        clusters.append({"name": c["name"], "description": c.get("description", ""), "prompt_indices": sorted(idxs)})


def enrich(cluster, pid):
    rs = [rows[i] for i in cluster["prompt_indices"]]
    latent = Counter(r["latent_pattern"] for r in rs)
    dominant, dom_count = latent.most_common(1)[0]
    return {
        "id": pid, "name": cluster["name"], "description": cluster["description"],
        "prompt_count": len(rs), "user_count": len({r["user_email"] for r in rs}),
        "purity_pct": round(100.0 * dom_count / len(rs), 1),
        "_indices": cluster["prompt_indices"],
    }


patterns = [enrich(c, f"p{i+1}") for i, c in enumerate(clusters)]
patterns = [p for p in patterns if p["prompt_count"] >= 8]
patterns = [dict(p, id=f"p{i+1}") for i, p in enumerate(patterns)]
consolidated = set()
for p in patterns:
    consolidated.update(p["_indices"])
consolidated_pct = round(100.0 * len(consolidated) / n, 1)
skill_count = sum(1 for p in patterns if p["prompt_count"] >= 8 and p["user_count"] >= 3)
for p in patterns:
    print(f"  {p['id']} {p['name']}: {p['prompt_count']} prompts, {p['user_count']} users, purity {p['purity_pct']}%")
print(f"patterns={len(patterns)} skills={skill_count} consolidated_pct={consolidated_pct}")

# COMMAND ----------

# --- step 3: write snapshot row to mining_history --------------------------
run_sql(
    f"CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} ("
    "snapshot_at TIMESTAMP, source_table STRING, total_prompts INT, "
    "pattern_count INT, skill_count INT, consolidated_pct DOUBLE, patterns_json STRING)"
)

patterns_json = json.dumps([
    {"id": p["id"], "name": p["name"], "prompt_count": p["prompt_count"],
     "user_count": p["user_count"], "purity_pct": p["purity_pct"]}
    for p in patterns
])
snap_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
run_sql(
    f"INSERT INTO {HISTORY_TABLE} (snapshot_at, source_table, total_prompts, "
    f"pattern_count, skill_count, consolidated_pct, patterns_json) VALUES ("
    f"TIMESTAMP'{snap_at}', {sql_str(SOURCE_LABEL)}, {n}, {len(patterns)}, "
    f"{skill_count}, {consolidated_pct}, {sql_str(patterns_json)})"
)
print(f"snapshot recorded at {snap_at} UTC -> {HISTORY_TABLE}")
dbutils.notebook.exit(json.dumps({
    "snapshot_at": snap_at, "total_prompts": n,
    "pattern_count": len(patterns), "skill_count": skill_count,
    "consolidated_pct": consolidated_pct,
}))
