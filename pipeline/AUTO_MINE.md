# Scheduled auto-mining

A Databricks Job periodically re-mines the live AI Gateway inference data and
records a time-series **snapshot** into `skillforge.core.mining_history`, so the
app can show *patterns-over-time*.

## Components

| Thing | Where |
|---|---|
| Snapshot table | `skillforge.core.mining_history` (UC) |
| Local runner | `pipeline/auto_mine.py` |
| Notebook (serverless, self-contained) | workspace `/Users/cliff.gilmore@databricks.com/skillforge-auto-mine` (source: `deploy/auto_mine_notebook.py`) |
| Job spec | `deploy/auto_mine_job.json` |
| Job | name `skillforge-auto-mine`, **job_id `518303301566796`**, DAILY `0 0 8 * * ?` `America/Los_Angeles` |

There are two equivalent mining paths:

1. **`pipeline/auto_mine.py`** — runs locally, reuses the existing pipeline
   modules (`extract_from_inference` → `mine_and_design`), then reads
   `app/data/results.json` (read-only) and inserts one summary row. Auth via
   `databricks auth token`. Also refreshes `recommended_skills` +
   `results.json` as a side effect (same as a normal mining run).
2. **The serverless notebook** — what the scheduled Job runs. It is
   self-contained (inlines extract → cluster → snapshot, mirroring
   `mine_and_design.py`) and uses the **notebook's own credentials** for both
   the SQL Statements API and the FMAPI AI Gateway — no PAT, no repo checkout,
   no pip installs. It only writes the snapshot row (it does not touch
   `results.json` or `recommended_skills`).

Both are idempotent: each run appends exactly one row keyed by its own
`snapshot_at`. The notebook aborts before snapshotting if fewer than 200
prompts match (inference batching not yet flushed).

## Schema: `skillforge.core.mining_history`

```
snapshot_at      TIMESTAMP   -- when this mining run completed (UTC)
source_table     STRING      -- inference payload table mined
total_prompts    INT         -- corpus size for this run
pattern_count    INT         -- recurring patterns found (>=8 prompts each)
skill_count      INT         -- skills recommended (pattern with >=3 users)
consolidated_pct DOUBLE      -- % of prompts covered by patterns
patterns_json    STRING      -- JSON array: [{id,name,prompt_count,user_count,purity_pct}]
```

The app SP `410e256c-84f4-4a4c-84f4-b9fcd03c6f3b` has `SELECT` on this table
(and `USE` on the schema already).

## How the app should read it (patterns-over-time panel)

Headline trend lines (one point per run):

```sql
SELECT snapshot_at, total_prompts, pattern_count, skill_count, consolidated_pct
FROM   skillforge.core.mining_history
ORDER  BY snapshot_at;
```

Per-pattern trends — explode `patterns_json` to track a single pattern's
`prompt_count` / `user_count` / `purity_pct` over time:

```sql
SELECT m.snapshot_at,
       p.id            AS pattern_id,
       p.name          AS pattern_name,
       p.prompt_count,
       p.user_count,
       p.purity_pct
FROM   skillforge.core.mining_history m
LATERAL VIEW explode(
  from_json(m.patterns_json,
            'array<struct<id:string,name:string,prompt_count:int,user_count:int,purity_pct:double>>')
) AS p
ORDER BY m.snapshot_at, p.id;
```

Latest snapshot only:

```sql
SELECT * FROM skillforge.core.mining_history
ORDER BY snapshot_at DESC LIMIT 1;
```

## Deploy / trigger

The table, notebook, and Job are already created. To redeploy from scratch:

```bash
source setup.env
TOKEN=$(databricks auth token --host $DATABRICKS_HOST | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")

# 1. (re)create the table
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "$DATABRICKS_HOST/api/2.0/sql/statements" -d "{\"warehouse_id\":\"$SQL_WAREHOUSE_ID\",\"statement\":\"CREATE TABLE IF NOT EXISTS skillforge.core.mining_history (snapshot_at TIMESTAMP, source_table STRING, total_prompts INT, pattern_count INT, skill_count INT, consolidated_pct DOUBLE, patterns_json STRING)\",\"wait_timeout\":\"30s\"}"

# 2. import the notebook (SOURCE/PYTHON, overwrite)
python3 - <<'PY'
import base64,json,os,urllib.request
host=os.environ["DATABRICKS_HOST"]; tok="'"$TOKEN"'"
c=open("deploy/auto_mine_notebook.py","rb").read()
b={"path":"/Users/cliff.gilmore@databricks.com/skillforge-auto-mine","format":"SOURCE","language":"PYTHON","content":base64.b64encode(c).decode(),"overwrite":True}
urllib.request.urlopen(urllib.request.Request(host+"/api/2.0/workspace/import",data=json.dumps(b).encode(),headers={"Authorization":"Bearer "+tok,"Content-Type":"application/json"}))
print("imported")
PY

# 3. create the Job (delete any existing one with the same name first)
#    spec lives in deploy/auto_mine_job.json -> POST /api/2.2/jobs/create
```

Trigger an on-demand run:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "$DATABRICKS_HOST/api/2.2/jobs/run-now" -d '{"job_id":518303301566796}'
```

Run the local path instead (also refreshes `results.json` + `recommended_skills`):

```bash
source setup.env && python3 pipeline/auto_mine.py
```

## Notes / caveats

- **Serverless FMAPI auth works**: the notebook calls the AI Gateway with the
  notebook context token (`apiToken()`), verified by the successful test run.
- The payload table name `databricks-claude-haiku-4-5_payload` contains hyphens,
  so every identifier part must be backtick-quoted in SQL (the local runner sets
  `SKILLFORGE_PAYLOAD_TABLE` to the quoted form; the notebook hardcodes it).
- No secrets are written to disk. `setup.env` is gitignored; tokens are fetched
  at runtime.
- Pattern/skill counts vary slightly run-to-run because clustering is an LLM call
  — expected, and exactly the kind of drift the time series captures.
