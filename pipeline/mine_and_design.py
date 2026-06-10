"""SkillForge recommendation engine.

Mines AI Gateway usage logs for recurring prompt patterns, designs reusable
skills via FMAPI, computes value metrics, runs a quality A/B for the top
skills, and writes results.json + a UC table.

NOTE: `latent_pattern` in the source data is ground truth used ONLY for the
final purity sanity check — never for clustering.
"""
import json
import os
import math
import sys
from collections import Counter
from datetime import datetime, timezone

from common import CATALOG, SCHEMA, chat, run_sql, sql_str

from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parent.parent
DATA_PATH = str(_ROOT / "app" / "data" / "gateway_usage.json")
RESULTS_PATH = str(_ROOT / "app" / "data" / "results.json")
SONNET = "databricks-claude-sonnet-4-6"
HAIKU = "databricks-claude-haiku-4-5"
WINDOW_DAYS = int(os.environ.get("SKILLFORGE_WINDOW_DAYS", "14"))
SAVINGS_PCT = 30  # estimated boilerplate input-token savings from templating


def log(msg):
    print(msg, flush=True)


def robust_json(messages, retries=3, **kw):
    """Like common.chat_json, but fence-safe (the reply's JSON may itself
    contain ``` blocks, which breaks naive fence stripping) and retries on
    malformed/truncated JSON with a bigger max_tokens."""
    for attempt in range(retries):
        text = chat(messages, **kw).strip()
        start, end = text.find("{"), text.rfind("}")
        try:
            if start == -1 or end <= start:
                raise json.JSONDecodeError("no JSON object found", text, 0)
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as e:
            if attempt == retries - 1:
                raise
            kw["max_tokens"] = int(kw.get("max_tokens", 4096) * 1.5)
            log(f"    [retry] bad JSON ({e}); retrying with max_tokens={kw['max_tokens']}")


# ---------------------------------------------------------------- 1. mining
CLUSTER_INSTRUCTIONS = """\
You are analyzing LLM prompts from an AI Gateway usage log to find recurring
patterns that could be consolidated into reusable prompt templates ("skills").

Cluster the numbered prompts below into recurring patterns:
- A pattern needs >= 8 prompts that share the same underlying task/intent.
- Leave genuine one-offs unclustered.
- Every index must appear at most once (no index in two clusters).

Return ONLY JSON, no prose:
{"clusters": [{"name": "...", "description": "...", "prompt_indices": [0, 5, ...]}],
 "unclustered": [indices...]}
"""


def numbered_block(rows, indices):
    return "\n".join(f"[{i}] {rows[i]['prompt']}" for i in indices)


def ask_clusters(rows, indices):
    msg = CLUSTER_INSTRUCTIONS + "\nPROMPTS:\n" + numbered_block(rows, indices)
    return robust_json([{"role": "user", "content": msg}],
                       model=SONNET, max_tokens=8000, temperature=0.2)


def mine_patterns(rows):
    n = len(rows)
    log(f"[mine] clustering all {n} prompts in one sonnet call ...")
    try:
        result = ask_clusters(rows, list(range(n)))
    except (json.JSONDecodeError, RuntimeError) as e:
        log(f"[mine] single-pass failed ({e}); falling back to two-pass")
        result = mine_two_pass(rows)

    # Validate indices: in range, no duplicates across clusters.
    seen = set()
    clusters = []
    for c in result.get("clusters", []):
        idxs = []
        for i in c.get("prompt_indices", []):
            if isinstance(i, int) and 0 <= i < n and i not in seen:
                seen.add(i)
                idxs.append(i)
        if idxs:
            clusters.append({"name": c["name"], "description": c.get("description", ""),
                             "prompt_indices": sorted(idxs)})
    unclustered = sorted(set(range(n)) - seen)
    log(f"[mine] {len(clusters)} clusters, {len(unclustered)} unclustered")
    return clusters, unclustered


def mine_two_pass(rows):
    n = len(rows)
    half = n // 2
    a = ask_clusters(rows, list(range(half)))
    b = ask_clusters(rows, list(range(half, n)))
    merge_msg = (
        "Two halves of a prompt log were clustered separately. Merge equivalent "
        "clusters (same underlying task) into single clusters, unioning their "
        "prompt_indices. Keep indices exactly as given. Return ONLY JSON: "
        '{"clusters":[{"name","description","prompt_indices"}],"unclustered":[...]}\n'
        f"HALF A: {json.dumps(a)}\nHALF B: {json.dumps(b)}"
    )
    return robust_json([{"role": "user", "content": merge_msg}],
                       model=SONNET, max_tokens=8000, temperature=0.2)


def enrich_cluster(rows, cluster, pid):
    rs = [rows[i] for i in cluster["prompt_indices"]]
    examples = sorted((r["prompt"] for r in rs), key=len)[:3]
    latent = Counter(r["latent_pattern"] for r in rs)
    dominant, dom_count = latent.most_common(1)[0]
    return {
        "id": pid,
        "name": cluster["name"],
        "description": cluster["description"],
        "prompt_count": len(rs),
        "user_count": len({r["user_email"] for r in rs}),
        "total_tokens": sum(r["input_tokens"] + r["output_tokens"] for r in rs),
        "example_prompts": examples,
        "purity_pct": round(100.0 * dom_count / len(rs), 1),
        "dominant_latent": dominant,
        "_indices": cluster["prompt_indices"],
    }


# ------------------------------------------------------------- 2. design
def design_skill(rows, pattern):
    idxs = pattern["_indices"]
    # 5 representative prompts: spread across the cluster
    step = max(1, len(idxs) // 5)
    reps = [rows[i]["prompt"] for i in idxs[::step][:5]]
    msg = (
        "Users keep hand-writing prompts like the 5 examples below (pattern: "
        f"\"{pattern['name']}\" — {pattern['description']}). Design ONE reusable "
        "skill (parameterized prompt template) that consolidates this pattern and "
        "encodes the best-practice structure these users are fumbling toward: an "
        "explicit role, clearly delimited inputs, a specified output format, and "
        "constraints. Use {placeholder} syntax for parameters.\n"
        "Return ONLY JSON:\n"
        '{"name": "kebab-case-name", "title": "...", '
        '"description": "1-2 sentences", '
        '"template": "the parameterized prompt with {placeholder} params", '
        '"parameters": [{"name": "...", "description": "..."}], '
        '"example_invocation": "the template filled with realistic values"}\n\n'
        "EXAMPLE PROMPTS:\n" + "\n".join(f"- {p}" for p in reps)
    )
    return robust_json([{"role": "user", "content": msg}],
                       model=SONNET, max_tokens=4000, temperature=0.3)


def value_metrics(rows, pattern):
    rs = [rows[i] for i in pattern["_indices"]]
    users = len({r["user_email"] for r in rs})
    count = len(rs)
    per_month = round(count * 30 / WINDOW_DAYS)
    total_tokens = sum(r["input_tokens"] + r["output_tokens"] for r in rs)
    input_tokens = sum(r["input_tokens"] for r in rs)
    est_monthly_tokens = round(total_tokens * 30 / WINDOW_DAYS)
    est_monthly_input = input_tokens * 30 / WINDOW_DAYS
    savings = round(est_monthly_input * SAVINGS_PCT / 100)
    if users >= 5 and per_month >= 40:
        priority = "high"
    elif users >= 3:
        priority = "medium"
    else:
        priority = "low"
    return {
        "users_covered": users,
        "prompt_count_14d": count,
        "prompts_per_month_est": per_month,
        "est_monthly_tokens": est_monthly_tokens,
        "input_token_savings_pct": SAVINGS_PCT,  # estimate: boilerplate removed by template
        "est_monthly_token_savings": savings,
        "priority": priority,
    }


# ------------------------------------------------------------- 4. A/B test
def quality_ab(rows, pattern, skill):
    # pick a representative real raw prompt (median length in the cluster)
    cluster_prompts = sorted((rows[i]["prompt"] for i in pattern["_indices"]), key=len)
    raw_prompt = cluster_prompts[len(cluster_prompts) // 2]

    build_msg = (
        "We are A/B testing a raw ad-hoc prompt against a reusable skill template "
        "for the SAME task.\n"
        f"RAW PROMPT:\n{raw_prompt}\n\n"
        f"SKILL TEMPLATE:\n{skill['template']}\n\n"
        f"PARAMETERS: {json.dumps(skill['parameters'])}\n\n"
        "Fill the template's parameters so it performs the exact same task as the "
        "raw prompt. If the raw prompt references pasted content (e.g. <ticket text>, "
        "<notes>, <log lines>), invent ONE short realistic stand-in (60-120 words) "
        "and substitute the SAME stand-in into BOTH the raw prompt and the filled "
        "template, so the two arms see identical source content.\n"
        "Return ONLY JSON: {\"raw_prompt_final\": \"...\", \"skill_prompt_final\": \"...\"}"
    )
    arms = robust_json([{"role": "user", "content": build_msg}],
                       model=SONNET, max_tokens=4000, temperature=0.3)
    raw_final, skill_final = arms["raw_prompt_final"], arms["skill_prompt_final"]

    log("    [ab] generating answers with haiku ...")
    raw_answer = chat([{"role": "user", "content": raw_final}], model=HAIKU, max_tokens=700)
    skill_answer = chat([{"role": "user", "content": skill_final}], model=HAIKU, max_tokens=700)

    judge_msg = (
        "Judge two answers to the same underlying task. Score each 1-10 on "
        "completeness, structure, and actionability (one combined score per answer).\n"
        f"TASK A PROMPT:\n{raw_final}\n\nANSWER A:\n{raw_answer}\n\n"
        f"TASK B PROMPT:\n{skill_final}\n\nANSWER B:\n{skill_answer}\n\n"
        'Return ONLY JSON: {"raw_score": <A score>, "skill_score": <B score>, '
        '"rationale": "2 sentences"}'
    )
    verdict = robust_json([{"role": "user", "content": judge_msg}],
                          model=SONNET, max_tokens=1000, temperature=0.0)
    return {
        "raw_prompt": raw_final,
        "skill_prompt": skill_final,
        "raw_answer": raw_answer[:600],
        "skill_answer": skill_answer[:600],
        "raw_score": verdict["raw_score"],
        "skill_score": verdict["skill_score"],
        "rationale": verdict["rationale"],
    }


# ------------------------------------------------------------------ main
def main():
    rows = json.load(open(DATA_PATH))
    n = len(rows)
    users_all = sorted({r["user_email"] for r in rows})
    log(f"[load] {n} rows, {len(users_all)} users")

    clusters_raw, unclustered = mine_patterns(rows)
    patterns = [enrich_cluster(rows, c, f"p{i+1}") for i, c in enumerate(clusters_raw)]
    # Keep only real patterns (>=8 prompts); smaller ones go back to unclustered
    kept, dropped_idxs = [], []
    for p in patterns:
        if p["prompt_count"] >= 8:
            kept.append(p)
        else:
            dropped_idxs.extend(p["_indices"])
    unclustered = sorted(set(unclustered) | set(dropped_idxs))
    patterns = [dict(p, id=f"p{i+1}") for i, p in enumerate(kept)]
    # Persist per-prompt assignments so the app's incremental refresh knows
    # which prompts are already covered (keyed by sha1 of prompt text).
    import hashlib
    assignments = {}
    for p in patterns:
        for i in p["_indices"]:
            h = hashlib.sha1(rows[i]["prompt"].encode()).hexdigest()
            assignments[h] = p["id"]
    with open(str(_ROOT / "app" / "data" / "assignments.json"), "w") as f:
        json.dump(assignments, f)
    log(f"[write] assignments.json ({len(assignments)} prompts)")
    for p in patterns:
        log(f"  {p['id']} {p['name']}: {p['prompt_count']} prompts, "
            f"{p['user_count']} users, purity {p['purity_pct']}% ({p['dominant_latent']})")

    # ---- design skills
    skills = []
    for p in patterns:
        if p["prompt_count"] >= 8 and p["user_count"] >= 3:
            log(f"[design] skill for {p['id']} {p['name']} ...")
            spec = design_skill(rows, p)
            skills.append({
                "id": f"s{len(skills)+1}",
                "pattern_id": p["id"],
                "name": spec["name"],
                "title": spec["title"],
                "description": spec["description"],
                "template": spec["template"],
                "parameters": spec["parameters"],
                "example_invocation": spec["example_invocation"],
                "value": value_metrics(rows, p),
                "quality_ab": None,
                "_pattern": p,
            })
            log(f"  -> {spec['name']} ({skills[-1]['value']['priority']})")

    # ---- A/B for top 3 by prompts_per_month_est
    top3 = sorted(skills, key=lambda s: -s["value"]["prompts_per_month_est"])[:3]
    for s in top3:
        log(f"[ab] {s['name']} ...")
        s["quality_ab"] = quality_ab(rows, s["_pattern"], s)
        log(f"    raw {s['quality_ab']['raw_score']} vs skill {s['quality_ab']['skill_score']}")

    # ---- assemble results.json
    consolidated_idxs = set()
    for s in skills:
        consolidated_idxs.update(s["_pattern"]["_indices"])
    users_impacted = len({rows[i]["user_email"] for i in consolidated_idxs})
    lifts = [s["quality_ab"]["skill_score"] - s["quality_ab"]["raw_score"]
             for s in skills if s["quality_ab"]]
    avg_lift = sum(lifts) / len(lifts) if lifts else 0.0

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {"table": os.environ.get("SKILLFORGE_SOURCE_TABLE", f"{CATALOG}.{SCHEMA}.gateway_usage"), "rows": n,
                   "users": len(users_all), "window_days": WINDOW_DAYS},
        "overview": {
            "total_prompts": n,
            "total_input_tokens": sum(r["input_tokens"] for r in rows),
            "total_output_tokens": sum(r["output_tokens"] for r in rows),
            "users": len(users_all),
            "endpoints": sorted({r["endpoint_name"] for r in rows}),
            "unclustered_prompts": len(unclustered),
        },
        "patterns": [{k: v for k, v in p.items() if not k.startswith("_")} for p in patterns],
        "skills": [{k: v for k, v in s.items() if not k.startswith("_")} for s in skills],
        "summary": {
            "skills_recommended": len(skills),
            "prompts_consolidated": len(consolidated_idxs),
            "prompts_consolidated_pct": round(100.0 * len(consolidated_idxs) / n, 1),
            "users_impacted": users_impacted,
            "est_monthly_token_savings_total": sum(
                s["value"]["est_monthly_token_savings"] for s in skills),
            "avg_quality_lift": f"+{avg_lift:.1f} points (top-3 A/B)",
        },
    }
    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
    log(f"[write] {RESULTS_PATH}")

    # ---- UC table
    log("[uc] writing recommended_skills table ...")
    fq = f"{CATALOG}.{SCHEMA}.recommended_skills"
    run_sql(f"""CREATE OR REPLACE TABLE {fq} (
        skill_id STRING, name STRING, title STRING, pattern STRING,
        users_covered INT, prompts_per_month_est INT,
        est_monthly_token_savings BIGINT, priority STRING, template STRING)""")
    if skills:
        vals = ",\n".join(
            f"({sql_str(s['id'])}, {sql_str(s['name'])}, {sql_str(s['title'])}, "
            f"{sql_str(s['_pattern']['name'])}, {s['value']['users_covered']}, "
            f"{s['value']['prompts_per_month_est']}, "
            f"{s['value']['est_monthly_token_savings']}, "
            f"{sql_str(s['value']['priority'])}, {sql_str(s['template'])})"
            for s in skills)
        run_sql(f"INSERT INTO {fq} VALUES {vals}")
    log(f"[uc] {len(skills)} rows inserted into {fq}")
    log("[done]")


if __name__ == "__main__":
    main()
