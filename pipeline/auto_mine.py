"""Scheduled auto-mining: re-mine the live AI Gateway inference data and
record a time-series snapshot row into skillforge.core.mining_history.

Pipeline (all reusing the existing pipeline/ modules, no duplication):
  1. extract_from_inference.main()  -> rebuilds app/data/gateway_usage.json
     from the live inference payload table.
  2. mine_and_design.main()         -> clusters + designs skills, writes
     app/data/results.json and the recommended_skills UC table.
  3. read app/data/results.json (READ ONLY) and INSERT one summary row into
     skillforge.core.mining_history.

Idempotent / safe to re-run: each run appends exactly one snapshot row keyed
by its own snapshot_at timestamp. Steps 1 and 2 are themselves rerunnable
(extract overwrites the JSON; mine_and_design uses CREATE OR REPLACE).

Env (see setup.env):
  DATABRICKS_HOST, DATABRICKS_WORKSPACE_ID, SQL_WAREHOUSE_ID,
  SKILLFORGE_CATALOG=skillforge, SKILLFORGE_SCHEMA=core
  SKILLFORGE_SOURCE_TABLE / SKILLFORGE_PAYLOAD_TABLE =
    skillforge_inference.feeds.databricks-claude-haiku-4-5_payload
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make sibling pipeline modules importable when run as `python pipeline/auto_mine.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import CATALOG, SCHEMA, run_sql, sql_str  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = _ROOT / "app" / "data" / "results.json"
HISTORY_TABLE = f"{CATALOG}.{SCHEMA}.mining_history"

# The endpoint payload table name contains hyphens, so each identifier part
# must be backtick-quoted for the Statements API parser. extract_from_inference
# interpolates SKILLFORGE_PAYLOAD_TABLE directly into SQL, so we pass the quoted
# form there; SKILLFORGE_SOURCE_TABLE is only a display label, so it stays plain.
PAYLOAD_TABLE = "skillforge_inference.feeds.databricks-claude-haiku-4-5_payload"
PAYLOAD_TABLE_SQL = "`skillforge_inference`.`feeds`.`databricks-claude-haiku-4-5_payload`"


def log(msg):
    print(msg, flush=True)


def ensure_history_table():
    """Idempotent: the deploy step already created this, but be self-contained."""
    run_sql(
        f"CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} ("
        "snapshot_at TIMESTAMP, source_table STRING, total_prompts INT, "
        "pattern_count INT, skill_count INT, consolidated_pct DOUBLE, "
        "patterns_json STRING) "
        "COMMENT 'Time-series snapshots of SkillForge auto-mining runs. "
        "patterns_json = [{id,name,prompt_count,user_count,purity_pct}]'"
    )


def run_extract():
    log("[auto-mine] step 1/3: extracting corpus from inference table ...")
    import extract_from_inference
    rc = extract_from_inference.main()
    if rc not in (0, None):
        raise RuntimeError(
            "extract_from_inference returned non-zero — inference batching may "
            "not have flushed yet. Aborting before snapshot."
        )


def run_mine():
    log("[auto-mine] step 2/3: mining + designing skills ...")
    import mine_and_design
    mine_and_design.main()


def snapshot(results: dict):
    """READ results (already loaded) and INSERT one summary row."""
    log("[auto-mine] step 3/3: writing snapshot row ...")
    overview = results.get("overview", {})
    summary = results.get("summary", {})
    source = results.get("source", {})

    total_prompts = int(overview.get("total_prompts", source.get("rows", 0)) or 0)
    pattern_count = len(results.get("patterns", []))
    skill_count = int(summary.get("skills_recommended", len(results.get("skills", []))) or 0)
    consolidated_pct = float(summary.get("prompts_consolidated_pct", 0.0) or 0.0)
    source_table = source.get("table", PAYLOAD_TABLE)

    # Compact per-pattern payload for the patterns-over-time panel.
    patterns_json = json.dumps([
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "prompt_count": p.get("prompt_count"),
            "user_count": p.get("user_count"),
            "purity_pct": p.get("purity_pct"),
        }
        for p in results.get("patterns", [])
    ])

    snap_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    run_sql(
        f"INSERT INTO {HISTORY_TABLE} (snapshot_at, source_table, total_prompts, "
        f"pattern_count, skill_count, consolidated_pct, patterns_json) VALUES ("
        f"TIMESTAMP'{snap_at}', {sql_str(source_table)}, {total_prompts}, "
        f"{pattern_count}, {skill_count}, {consolidated_pct}, {sql_str(patterns_json)})"
    )
    return {
        "snapshot_at": snap_at + " UTC",
        "source_table": source_table,
        "total_prompts": total_prompts,
        "pattern_count": pattern_count,
        "skill_count": skill_count,
        "consolidated_pct": consolidated_pct,
    }


def main():
    # Pin the source/payload table so the engine + extractor read the live one.
    # SOURCE_TABLE = plain display label; PAYLOAD_TABLE = backtick-quoted for SQL.
    os.environ["SKILLFORGE_SOURCE_TABLE"] = PAYLOAD_TABLE
    os.environ["SKILLFORGE_PAYLOAD_TABLE"] = PAYLOAD_TABLE_SQL

    ensure_history_table()
    run_extract()
    run_mine()

    if not RESULTS_PATH.exists():
        raise RuntimeError(f"{RESULTS_PATH} missing — mining did not complete.")
    results = json.loads(RESULTS_PATH.read_text())  # READ ONLY
    summary = snapshot(results)

    log("\n=== auto-mine snapshot recorded ===")
    for k, v in summary.items():
        log(f"  {k}: {v}")
    log(f"  -> {HISTORY_TABLE}")
    log("[auto-mine] done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
