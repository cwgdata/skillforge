"""SkillForge — Automatic Skill Recommendation / Design Engine from AI Gateway usage.

FastAPI backend that serves a static dashboard plus an API over the mined
results (results.json) and raw gateway usage (gateway_usage.json), and a live
FMAPI test bench (POST /api/test_skill).
"""
import json
import os
import subprocess
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import requests
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
RESULTS_PATH = DATA_DIR / "results.json"
USAGE_PATH = DATA_DIR / "gateway_usage.json"

# Local-mode only (remote uses the app SP's ambient auth). Set DATABRICKS_HOST
# to your workspace URL when running outside Databricks Apps.
WORKSPACE_HOST = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
# https://<workspace-id>.ai-gateway.cloud.databricks.com/mlflow/v1 — routing
# through the Gateway (not /serving-endpoints) is what makes calls land in
# system.ai_gateway.usage and the Gateway UI counters.
AI_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "").rstrip("/")
FMAPI_MODEL = "databricks-claude-haiku-4-5"
IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))

app = FastAPI(title="SkillForge")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def load_results():
    """Return parsed results.json, or {'status': 'pending'} if absent/invalid."""
    if not RESULTS_PATH.exists():
        return {"status": "pending"}
    try:
        with open(RESULTS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"status": "pending"}


def load_usage():
    """Return parsed gateway_usage.json (list of rows), or [] on failure."""
    if not USAGE_PATH.exists():
        return []
    try:
        with open(USAGE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def get_token():
    """Get a bearer token for FMAPI calls, dual-mode (app vs local)."""
    if IS_DATABRICKS_APP:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient()
        auth = w.config.authenticate()
        return auth["Authorization"].replace("Bearer ", "")
    # Local mode: use the databricks CLI to mint a token.
    if not WORKSPACE_HOST:
        raise RuntimeError("Set DATABRICKS_HOST to your workspace URL for local runs.")
    out = subprocess.run(
        ["databricks", "auth", "token", "--host", WORKSPACE_HOST],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"databricks auth token failed: {out.stderr.strip()}")
    return json.loads(out.stdout)["access_token"]


def call_fmapi(prompt_text):
    """Call the AI Gateway chat/completions endpoint with a single user prompt.

    Returns (answer_text, usage_dict_or_None). Raises on transport/HTTP error.
    """
    if not AI_GATEWAY_URL:
        raise RuntimeError(
            "AI_GATEWAY_URL is not set — point it at "
            "https://<workspace-id>.ai-gateway.cloud.databricks.com/mlflow/v1"
        )
    token = get_token()
    url = f"{AI_GATEWAY_URL}/chat/completions"
    payload = {
        "model": FMAPI_MODEL,
        "messages": [{"role": "user", "content": prompt_text}],
        "max_tokens": 700,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    answer = ""
    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        answer = json.dumps(data)[:2000]
    return answer, data.get("usage")


def fill_template(template, parameters):
    """Fill {placeholder} tokens in template from the parameters dict.

    Leaves unknown placeholders intact rather than erroring.
    """
    out = template or ""
    for name, value in (parameters or {}).items():
        out = out.replace("{" + str(name) + "}", str(value))
    return out


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/results")
def results():
    return JSONResponse(load_results())


@app.get("/api/usage/stats")
def usage_stats():
    rows = load_usage()
    per_day = Counter()
    per_user = Counter()
    tokens_per_endpoint = defaultdict(int)

    for r in rows:
        et = str(r.get("event_time", ""))
        day = et.split(" ")[0].split("T")[0] if et else "unknown"
        per_day[day] += 1
        per_user[r.get("user_email", "unknown")] += 1
        tok = (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
        tokens_per_endpoint[r.get("endpoint_name", "unknown")] += tok

    prompts_per_day = [
        {"date": d, "count": c} for d, c in sorted(per_day.items())
    ]
    prompts_per_user = [
        {"user": u, "count": c} for u, c in per_user.most_common(10)
    ]
    tokens_by_endpoint = [
        {"endpoint": e, "tokens": t}
        for e, t in sorted(
            tokens_per_endpoint.items(), key=lambda kv: kv[1], reverse=True
        )
    ]
    return {
        "prompts_per_day": prompts_per_day,
        "prompts_per_user": prompts_per_user,
        "tokens_by_endpoint": tokens_by_endpoint,
        "total_rows": len(rows),
    }


class TestSkillRequest(BaseModel):
    skill_id: str
    parameters: dict = {}
    raw_prompt: str | None = None


@app.post("/api/test_skill")
def test_skill(req: TestSkillRequest):
    res = load_results()
    if res.get("status") == "pending":
        return JSONResponse(
            {"error": "Engine has not run yet — no skills available."},
            status_code=409,
        )

    skill = next(
        (s for s in res.get("skills", []) if s.get("id") == req.skill_id), None
    )
    if skill is None:
        return JSONResponse(
            {"error": f"Skill '{req.skill_id}' not found."}, status_code=404
        )

    skill_prompt = fill_template(skill.get("template", ""), req.parameters)

    result = {
        "skill_prompt": skill_prompt,
        "skill_answer": None,
        "raw_answer": None,
        "skill_usage": None,
        "raw_usage": None,
    }
    try:
        skill_answer, skill_usage = call_fmapi(skill_prompt)
        result["skill_answer"] = skill_answer
        result["skill_usage"] = skill_usage

        if req.raw_prompt:
            raw_answer, raw_usage = call_fmapi(req.raw_prompt)
            result["raw_answer"] = raw_answer
            result["raw_usage"] = raw_usage
    except Exception as exc:  # noqa: BLE001 — surface any failure as JSON
        return JSONResponse(
            {"error": f"FMAPI call failed: {exc}", **result}, status_code=502
        )

    return result


# --------------------------------------------------------------------------- #
# Static
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(STATIC_DIR)), name="static")
