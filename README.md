# SkillForge

**Automatic skill recommendation & design engine for Databricks AI Gateway.**

Every org using LLMs has dozens of people independently hand-rolling prompts for
the same tasks — SQL generation, RCA drafting, ticket triage, meeting recaps.
SkillForge mines AI Gateway usage logs to find those recurring patterns, then
uses the Foundation Model API to *design* reusable, parameterized skills that
consolidate them — and quantifies the value (users covered, volume, token
savings, and a judged before/after quality A/B).

```
AI Gateway usage logs ──► pattern mining ──► skill design (FMAPI) ──► value scoring ──► dashboard
   (inference tables)       (clustering)      (templates + params)     (A/B judge)    (Databricks App)
```

## What's in the box

| Path | What it is |
|---|---|
| `pipeline/generate_usage.py` | Generates realistic synthetic gateway traffic via FMAPI (~236 prompts, 30 users, 8 latent patterns + noise) and loads it into a UC table shaped like AI Gateway inference-table payloads. Ground-truth labels are withheld from the table. |
| `pipeline/mine_and_design.py` | The engine: clusters raw prompts (LLM-based, blind to labels), designs a skill spec per qualifying cluster, computes value metrics from the data, and runs a before/after quality A/B (same inputs, same model, LLM-judged). Writes `app/data/results.json` + a UC `recommended_skills` table. |
| `app/` | Databricks App: FastAPI + a dark dashboard — KPIs, usage charts, discovered patterns (with purity badges), skill cards (template, params, A/B panel), a **live test bench**, a **prompt-injection panel**, one-click **Refresh** (incremental re-classification + emerging-pattern detection), a **Gateway Coverage scanner**, and **OBO auth** with signed-in identity display. |

All FMAPI calls route through the **AI Gateway URL**
(`https://<workspace-id>.ai-gateway.cloud.databricks.com/mlflow/v1`), so the
demo itself populates `system.ai_gateway.usage` — the very feed a production
deployment would mine instead of the synthetic table.

## Results on the bundled synthetic dataset

- Rediscovered **8/8** hidden patterns at **97%+ average purity**, correctly leaving all 36 noise prompts unclustered — from raw prompt text only.
- **84.7%** of traffic (200/236 prompts) consolidates into **8 skills** covering all 30 users.
- Quality A/B (top 3 skills, judged on completeness/structure/actionability): skill templates won **9–7, 9–7, 9–6** (+2.3 avg). The judge's rationale is the point: raw prompts missed schema precision, policy grounding, and lessons-learned sections — exactly the boilerplate a skill encodes once for everyone.
- ~31.7K input tokens/month estimated savings (30% boilerplate-elimination estimate, labeled as such).

The bundled `app/data/*.json` are from a real run on the synthetic data, so the
dashboard works out of the box. All names/emails in the data are fictitious
(`acme-corp.com`).


## Live loop: inject → refresh → emerging patterns

The deployed app closes the loop end to end:

1. **Inject Prompts** — paste prompts in the UI; each is sent through the AI
   Gateway (so it lands in the endpoint's real inference table) and recorded in
   `injected_prompts` for immediate visibility.
2. **Refresh** — pulls new prompts from three feeds (bundled snapshot, the
   `injected_prompts` table, and the **live inference table**), classifies them
   against known patterns in one FMAPI call, and when enough unassigned prompts
   cohere it proposes an **EMERGING** pattern *and designs a skill for it* on
   the spot. A progress banner with elapsed time shows while it runs.
3. **Gateway Coverage** — scans every serving endpoint for
   `ai_gateway.inference_table_config` and shows which feeds SkillForge can mine.
4. **Identity** — with Databricks Apps user authorization enabled
   (`user_api_scopes: sql, serving.serving-endpoints`), requests carry the
   signed-in user's token; the header chip shows who you are and whether calls
   run **OBO** or as the app **SP**. Downscoped OBO tokens are rejected by some
   data planes (e.g. the AI Gateway), so calls try OBO first and fall back to
   the SP automatically.

> Gotcha worth knowing: `PATCH /api/2.0/apps/{name}` **replaces** the whole
> config — patching `user_api_scopes` alone silently drops `resources` (and
> with it the env vars injected via `valueFrom`). Always PATCH resources and
> scopes together.

## Configuration

| Env var | Used by | Notes |
|---|---|---|
| `DATABRICKS_HOST` | pipeline, app (local mode) | Workspace URL |
| `DATABRICKS_WORKSPACE_ID` | pipeline | The `?o=` id; builds the Gateway URL |
| `SQL_WAREHOUSE_ID` | pipeline | Serverless warehouse for UC reads/writes |
| `SKILLFORGE_CATALOG` / `SKILLFORGE_SCHEMA` | pipeline + app | Default `main` / `skillforge`; this project uses `skillforge` / `core` |
| `AI_GATEWAY_URL` | app | Set in `app.yaml` (placeholder — fill in your workspace id) |

Auth: the pipeline and local app mode mint OAuth tokens via the Databricks CLI
(`databricks auth login --host <workspace>`). Deployed, the app uses its
service principal — attach the FMAPI serving endpoint as an app **resource**
with `CAN_QUERY`.

## Run it

```bash
# 1. Generate data + run the engine (FMAPI-heavy; a few minutes)
export DATABRICKS_HOST=https://<your-workspace>.cloud.databricks.com
export DATABRICKS_WORKSPACE_ID=<workspace-id>
export SQL_WAREHOUSE_ID=<warehouse-id>
cd pipeline && python3 generate_usage.py && python3 mine_and_design.py

# 2. Try the app locally
cd .. && pip install -r requirements.txt
python3 -m uvicorn app.app:app --port 8000   # needs AI_GATEWAY_URL exported for the test bench

# 3. Deploy as a Databricks App
#    (fill in AI_GATEWAY_URL in app.yaml first)
databricks apps create skillforge
databricks sync . /Users/<you>/skillforge-src --exclude .git --exclude __pycache__ --exclude pipeline
databricks apps deploy skillforge --source-code-path /Workspace/Users/<you>/skillforge-src
#    then attach the serving endpoint resource (CAN_QUERY) in the app's Edit UI
```

## Adapting to real usage

Point the engine at your actual gateway logs instead of the synthetic table:
`system.ai_gateway.usage` joined to your endpoints' inference tables (for
payloads), or any inference table with request payload capture enabled. The
mining/design/value stages don't care where the prompts came from.

## License

Apache-2.0
