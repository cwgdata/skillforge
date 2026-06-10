"""SkillForge — Automatic Skill Recommendation / Design Engine from AI Gateway usage.

FastAPI backend that serves a static dashboard plus an API over the mined
results (results.json) and raw gateway usage (gateway_usage.json), a live FMAPI
test bench (POST /api/test_skill), prompt injection (POST /api/inject),
incremental re-classification (POST /api/refresh), an inference-table scan
(GET /api/endpoints/scan), and an identity endpoint (GET /api/whoami).

Auth is dual/tri-mode:
  - On Databricks Apps with user authorization enabled, each request carries the
    signed-in user's OAuth token in the `x-forwarded-access-token` header (OBO).
    We prefer that token so SQL / serving-endpoint / FMAPI calls run AS the user.
  - Falling back to the app service principal (databricks-sdk ambient auth) when
    no OBO header is present and we're running in-app.
  - Locally (outside Databricks Apps) we mint a token via the databricks CLI.
"""
import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import requests
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
RESULTS_PATH = DATA_DIR / "results.json"
USAGE_PATH = DATA_DIR / "gateway_usage.json"
ASSIGNMENTS_PATH = DATA_DIR / "assignments.json"

# Local-mode only (remote uses the app SP's ambient auth / OBO). Set
# DATABRICKS_HOST to your workspace URL when running outside Databricks Apps.
# In-app DATABRICKS_HOST is set automatically (and may lack the https:// scheme).
WORKSPACE_HOST = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
if WORKSPACE_HOST and not WORKSPACE_HOST.startswith("http"):
    WORKSPACE_HOST = "https://" + WORKSPACE_HOST
# https://<workspace-id>.ai-gateway.cloud.databricks.com/mlflow/v1 — routing
# through the Gateway (not /serving-endpoints) is what makes calls land in
# system.ai_gateway.usage and the Gateway UI counters.
AI_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "").rstrip("/")
FMAPI_MODEL = "databricks-claude-haiku-4-5"
FMAPI_HAIKU = "databricks-claude-haiku-4-5"
FMAPI_SONNET = "databricks-claude-sonnet-4-6"
IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))

WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
CATALOG = os.environ.get("SKILLFORGE_CATALOG", "main")
SCHEMA = os.environ.get("SKILLFORGE_SCHEMA", "skillforge")
# Live serving-endpoint inference table for the haiku gateway feed (payload mode).
INFERENCE_TABLE = f"{CATALOG}.{SCHEMA}.fmapi_haiku_payload"
INJECTED_TABLE = f"{CATALOG}.{SCHEMA}.injected_prompts"

app = FastAPI(title="SkillForge")


# --------------------------------------------------------------------------- #
# Data loading
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
    """Return parsed gateway_usage.json (list of rows), or [] on failure.

    NOTE: another process appends to gateway_usage.json concurrently — we only
    ever READ it here, never write.
    """
    if not USAGE_PATH.exists():
        return []
    try:
        with open(USAGE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def load_assignments():
    """sha1(prompt) -> pattern_id assignments. May be absent → treat as empty."""
    if not ASSIGNMENTS_PATH.exists():
        return {}
    try:
        with open(ASSIGNMENTS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def sha1(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Auth — OBO (user) -> service principal -> local CLI
# --------------------------------------------------------------------------- #
def get_token():
    """App/local token (service principal in-app, CLI locally). No OBO."""
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


def request_token(request):
    """Return the OBO user token if the header is present, else fall back to
    the service-principal / local token. This is what makes downstream SQL,
    serving-endpoint and FMAPI calls run as the signed-in user."""
    if request is not None:
        obo = request.headers.get("x-forwarded-access-token")
        if obo:
            return obo
    return get_token()


def request_identity(request):
    """{"email", "auth_mode"} for the current request.

    auth_mode is one of: obo (OBO header present), service_principal (in-app, no
    OBO), or local (running outside Databricks Apps).
    """
    email = None
    auth_mode = "local"
    if request is not None:
        obo = request.headers.get("x-forwarded-access-token")
        email = (
            request.headers.get("x-forwarded-email")
            or request.headers.get("x-forwarded-preferred-username")
        )
        if obo:
            auth_mode = "obo"
        elif IS_DATABRICKS_APP:
            auth_mode = "service_principal"
    elif IS_DATABRICKS_APP:
        auth_mode = "service_principal"
    return {"email": email, "auth_mode": auth_mode}


# --------------------------------------------------------------------------- #
# FMAPI / SQL helpers
# --------------------------------------------------------------------------- #
def token_candidates(request):
    """Tokens to try in order: OBO (user) first, then app SP / local.

    Databricks Apps user-authorization tokens are DOWNSCOPED to the app's
    user_api_scopes; in practice the AI Gateway data plane rejects them (403)
    and the serving-endpoints list comes back empty. So: run as the user where
    the scopes allow it, fall back to the service principal where they don't.
    Identity display (whoami) always reflects the real signed-in user.
    """
    toks = []
    if request is not None:
        obo = request.headers.get("x-forwarded-access-token")
        if obo:
            toks.append(obo)
    toks.append(get_token())
    return toks


def call_fmapi(prompt_text, request=None, model=None, max_tokens=700, temperature=None):
    """Call the AI Gateway chat/completions endpoint with a single user prompt.

    Tries the OBO token first, falls back to SP on 401/403. Returns
    (answer, usage_dict). Raises on transport/HTTP error.
    """
    if not AI_GATEWAY_URL:
        raise RuntimeError(
            "AI_GATEWAY_URL is not set — point it at "
            "https://<workspace-id>.ai-gateway.cloud.databricks.com/mlflow/v1"
        )
    url = f"{AI_GATEWAY_URL}/chat/completions"
    payload = {
        "model": model or FMAPI_MODEL,
        "messages": [{"role": "user", "content": prompt_text}],
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    resp = None
    for token in token_candidates(request):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        resp = requests.post(url, json=payload, headers=headers, timeout=120)
        if resp.status_code not in (401, 403):
            break
    resp.raise_for_status()
    data = resp.json()
    answer = ""
    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        answer = json.dumps(data)[:2000]
    return answer, data.get("usage")


def call_fmapi_chat(messages, request=None, model=None, max_tokens=4096, temperature=None):
    """Chat-completions call taking a full messages list (for the classifier)."""
    if not AI_GATEWAY_URL:
        raise RuntimeError("AI_GATEWAY_URL is not set.")
    url = f"{AI_GATEWAY_URL}/chat/completions"
    payload = {"model": model or FMAPI_SONNET, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        payload["temperature"] = temperature
    resp = None
    for token in token_candidates(request):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        resp = requests.post(url, json=payload, headers=headers, timeout=300)
        if resp.status_code not in (401, 403):
            break
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def parse_json_loose(text):
    """Strip code fences and slice from the first { to the last } before parsing."""
    t = (text or "").strip()
    if t.startswith("```"):
        # drop the opening fence line and any closing fence
        t = re.sub(r"^```[a-zA-Z]*\n", "", t)
        t = re.sub(r"\n```\s*$", "", t)
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        t = t[start : end + 1]
    return json.loads(t)


def run_sql(statement, request=None, wait="50s"):
    """Execute SQL via the Statements API on the attached warehouse, OBO when
    a request token is available. Returns result rows (data_array)."""
    if not WAREHOUSE_ID:
        raise RuntimeError("DATABRICKS_WAREHOUSE_ID is not set.")
    r = None
    for token in token_candidates(request):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        r = requests.post(
            f"{WORKSPACE_HOST}/api/2.0/sql/statements",
            headers=headers,
            json={"warehouse_id": WAREHOUSE_ID, "statement": statement, "wait_timeout": wait},
            timeout=120,
        )
        if r.status_code not in (401, 403):
            break
    r.raise_for_status()
    d = r.json()
    state = d.get("status", {}).get("state")
    while state in ("PENDING", "RUNNING"):
        time.sleep(3)
        sid = d["statement_id"]
        d = requests.get(
            f"{WORKSPACE_HOST}/api/2.0/sql/statements/{sid}",
            headers=headers,
            timeout=60,
        ).json()
        state = d.get("status", {}).get("state")
    if state != "SUCCEEDED":
        msg = d.get("status", {}).get("error", {}).get("message", "")[:300]
        raise RuntimeError(f"SQL failed ({state}): {msg}")
    return d.get("result", {}).get("data_array") or []


def sql_str(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def fill_template(template, parameters):
    """Fill {placeholder} tokens in template from the parameters dict.

    Leaves unknown placeholders intact rather than erroring.
    """
    out = template or ""
    for name, value in (parameters or {}).items():
        out = out.replace("{" + str(name) + "}", str(value))
    return out


# --------------------------------------------------------------------------- #
# Usage / analysis window
# --------------------------------------------------------------------------- #
def _parse_event_time(et):
    """Parse an event_time string ('YYYY-MM-DD HH:MM:SS' or ISO) to datetime."""
    if not et:
        return None
    s = str(et).replace("T", " ").split("+")[0].split(".")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _window_cutoff(rows, window_days):
    """Cutoff datetime relative to the MAX event_time in the data (synthetic
    data → use data-relative, not wall-clock). None ⇒ no filtering (all)."""
    if not window_days or window_days <= 0:
        return None
    times = [t for t in (_parse_event_time(r.get("event_time")) for r in rows) if t]
    if not times:
        return None
    return max(times) - timedelta(days=window_days)


def filter_window(rows, window_days):
    cutoff = _window_cutoff(rows, window_days)
    if cutoff is None:
        return rows
    out = []
    for r in rows:
        t = _parse_event_time(r.get("event_time"))
        if t is None or t >= cutoff:
            out.append(r)
    return out


# --------------------------------------------------------------------------- #
# API — base
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/results")
def results():
    return JSONResponse(load_results())


@app.get("/api/whoami")
def whoami(request: Request):
    ident = request_identity(request)
    if ident["auth_mode"] in ("obo", "service_principal"):
        return {"email": ident["email"] or "service-principal", "auth_mode": ident["auth_mode"]}
    # Local mode: resolve the current user via SCIM /Me (5s timeout, fallbacks).
    email = "local-dev"
    try:
        token = get_token()
        resp = requests.get(
            f"{WORKSPACE_HOST}/api/2.0/preview/scim/v2/Me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.ok:
            email = resp.json().get("userName") or "local-dev"
    except Exception:  # noqa: BLE001
        email = "unknown"
    return {"email": email, "auth_mode": "local"}


@app.get("/api/usage/stats")
def usage_stats(window_days: int = 0):
    rows = filter_window(load_usage(), window_days)
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

    return {
        "prompts_per_day": [{"date": d, "count": c} for d, c in sorted(per_day.items())],
        "prompts_per_user": [{"user": u, "count": c} for u, c in per_user.most_common(10)],
        "tokens_by_endpoint": [
            {"endpoint": e, "tokens": t}
            for e, t in sorted(tokens_per_endpoint.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "total_rows": len(rows),
        "window_days": window_days,
    }


# --------------------------------------------------------------------------- #
# Test bench
# --------------------------------------------------------------------------- #
class TestSkillRequest(BaseModel):
    skill_id: str
    parameters: dict = {}
    raw_prompt: str | None = None


@app.post("/api/test_skill")
def test_skill(req: TestSkillRequest, request: Request):
    res = load_results()
    if res.get("status") == "pending":
        return JSONResponse(
            {"error": "Engine has not run yet — no skills available."}, status_code=409
        )

    skill = next((s for s in res.get("skills", []) if s.get("id") == req.skill_id), None)
    if skill is None:
        return JSONResponse({"error": f"Skill '{req.skill_id}' not found."}, status_code=404)

    skill_prompt = fill_template(skill.get("template", ""), req.parameters)
    result = {
        "skill_prompt": skill_prompt,
        "skill_answer": None,
        "raw_answer": None,
        "skill_usage": None,
        "raw_usage": None,
    }
    try:
        skill_answer, skill_usage = call_fmapi(skill_prompt, request=request)
        result["skill_answer"] = skill_answer
        result["skill_usage"] = skill_usage
        if req.raw_prompt:
            raw_answer, raw_usage = call_fmapi(req.raw_prompt, request=request)
            result["raw_answer"] = raw_answer
            result["raw_usage"] = raw_usage
    except Exception as exc:  # noqa: BLE001 — surface any failure as JSON
        return JSONResponse({"error": f"FMAPI call failed: {exc}", **result}, status_code=502)
    return result


# --------------------------------------------------------------------------- #
# Inject prompts
# --------------------------------------------------------------------------- #
class InjectRequest(BaseModel):
    prompts: list[str]
    user_email: str | None = None


@app.post("/api/inject")
def inject(req: InjectRequest, request: Request):
    prompts = [p.strip() for p in (req.prompts or []) if p and p.strip()][:20]
    if not prompts:
        return JSONResponse({"error": "Provide 1-20 prompts."}, status_code=400)

    ident = request_identity(request)
    email = req.user_email or ident.get("email") or "injected@skillforge"

    sent, failed, errors = 0, 0, []
    inserted = 0
    for p in prompts:
        # (a) Fire through the AI Gateway so it lands in the REAL inference table.
        try:
            call_fmapi(p, request=request, model=FMAPI_HAIKU, max_tokens=16)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            errors.append(str(exc)[:200])
            continue
        # (b) Record the prompt in our injected_prompts UC table for refresh.
        try:
            run_sql(
                f"CREATE TABLE IF NOT EXISTS {INJECTED_TABLE} "
                "(request_time TIMESTAMP, user_email STRING, prompt STRING)",
                request=request,
            )
            run_sql(
                f"INSERT INTO {INJECTED_TABLE} (request_time, user_email, prompt) "
                f"VALUES (current_timestamp(), {sql_str(email)}, {sql_str(p)})",
                request=request,
            )
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"UC insert: {str(exc)[:200]}")

    return {
        "sent": sent,
        "failed": failed,
        "inserted": inserted,
        "user_email": email,
        "errors": errors[:5],
        "note": (
            "Prompts were sent through the Gateway — they appear in the serving "
            "endpoint's inference table within ~10-30 min (batch logging), but "
            "SkillForge sees the injected_prompts UC rows immediately. Hit Refresh "
            "to re-classify."
        ),
    }


# --------------------------------------------------------------------------- #
# Endpoint / inference-table scan (cached 60s)
# --------------------------------------------------------------------------- #
_SCAN_CACHE = {"ts": 0.0, "data": None}


@app.get("/api/endpoints/scan")
def endpoints_scan(request: Request):
    now = time.time()
    if _SCAN_CACHE["data"] is not None and (now - _SCAN_CACHE["ts"]) < 60:
        return _SCAN_CACHE["data"]

    # OBO-downscoped tokens list zero endpoints here — fall back to the SP
    # token when the user-scoped call comes back empty or denied.
    eps, last_exc = [], None
    for token in token_candidates(request):
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = requests.get(
                f"{WORKSPACE_HOST}/api/2.0/serving-endpoints", headers=headers, timeout=30
            )
            resp.raise_for_status()
            eps = resp.json().get("endpoints", [])
            if eps:
                break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    if not eps and last_exc is not None:
        return JSONResponse({"error": f"Endpoint scan failed: {last_exc}", "endpoints": []}, status_code=502)

    # The list API omits ai_gateway.inference_table_config — it only appears on
    # the per-endpoint GET. Fetch details concurrently (bounded; cached 60s).
    from concurrent.futures import ThreadPoolExecutor

    def detail(name):
        try:
            r = requests.get(
                f"{WORKSPACE_HOST}/api/2.0/serving-endpoints/{name}",
                headers=headers,
                timeout=15,
            )
            if r.ok:
                return r.json().get("ai_gateway") or {}
        except Exception:  # noqa: BLE001
            pass
        return {}

    names = [e.get("name") for e in eps if e.get("name")]
    with ThreadPoolExecutor(max_workers=12) as pool:
        gateways = dict(zip(names, pool.map(detail, names)))

    out = []
    for ep in eps:
        gw = gateways.get(ep.get("name")) or ep.get("ai_gateway") or {}
        itc = gw.get("inference_table_config") or {}
        enabled = bool(itc.get("enabled"))
        table = None
        if enabled:
            cat = itc.get("catalog_name", "")
            sch = itc.get("schema_name", "")
            prefix = itc.get("table_name_prefix", "")
            table = ".".join([x for x in (cat, sch, f"{prefix}_payload" if prefix else None) if x])
        usage = gw.get("usage_tracking_config") or {}
        out.append(
            {
                "name": ep.get("name"),
                "endpoint_type": ep.get("endpoint_type") or ep.get("task") or "",
                "state": (ep.get("state") or {}).get("ready")
                or (ep.get("state") or {}).get("config_update")
                or "",
                "inference_table": table,
                "usage_tracking": bool(usage.get("enabled")),
            }
        )
    # configured (inference_table set) first, then alphabetical
    out.sort(key=lambda e: (e["inference_table"] is None, e["name"] or ""))
    payload = {
        "endpoints": out,
        "configured": sum(1 for e in out if e["inference_table"]),
        "total": len(out),
    }
    _SCAN_CACHE["ts"] = now
    _SCAN_CACHE["data"] = payload
    return payload


# --------------------------------------------------------------------------- #
# Refresh — incremental re-classification
# --------------------------------------------------------------------------- #
def _gather_candidates(request, window_days):
    """Collect candidate prompts from 3 sources, dedup by sha1(prompt).

    Returns list of {"prompt", "user_email", "h"}.
    """
    usage = load_usage()
    cutoff = _window_cutoff(usage, window_days)

    def in_window(et):
        if cutoff is None:
            return True
        t = _parse_event_time(et)
        return t is None or t >= cutoff

    seen = {}

    # (i) gateway_usage.json rows
    for r in usage:
        if not in_window(r.get("event_time")):
            continue
        p = r.get("prompt")
        if not p:
            continue
        h = sha1(p)
        if h not in seen:
            seen[h] = {"prompt": p, "user_email": r.get("user_email", "unknown"), "h": h}

    # (ii) injected_prompts UC table
    try:
        rows = run_sql(
            f"SELECT prompt, user_email FROM {INJECTED_TABLE}", request=request
        )
        for row in rows:
            p = row[0] if row else None
            if not p:
                continue
            h = sha1(p)
            if h not in seen:
                seen[h] = {"prompt": p, "user_email": (row[1] if len(row) > 1 else "injected"), "h": h}
    except Exception:  # noqa: BLE001 — table may not exist yet
        pass

    # (iii) live inference table — parse request JSON, take last user-role message
    try:
        rows = run_sql(
            f"SELECT request, response FROM {INFERENCE_TABLE} WHERE status_code = 200",
            request=request,
        )
        for row in rows:
            raw = row[0] if row else None
            if not raw:
                continue
            try:
                doc = json.loads(raw) if isinstance(raw, str) else raw
                msgs = doc.get("messages") or []
                user_msgs = [m for m in msgs if m.get("role") == "user"]
                if not user_msgs:
                    continue
                content = user_msgs[-1].get("content")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                p = content
            except Exception:  # noqa: BLE001
                continue
            if not p:
                continue
            h = sha1(p)
            if h not in seen:
                seen[h] = {"prompt": p, "user_email": "inference-table", "h": h}
    except Exception:  # noqa: BLE001 — table may not exist yet → skip source iii
        pass

    return list(seen.values())


def _classify_batch(patterns, candidates, request):
    """One sonnet call: map candidate idx -> pattern_id or 'none'. Returns dict."""
    plist = "\n".join(
        f"- {p['id']}: {p['name']} — {p.get('description','')}" for p in patterns
    )
    numbered = "\n".join(f"{i}. {c['prompt'][:600]}" for i, c in enumerate(candidates))
    sys = (
        "You are a prompt-clustering classifier. Given existing usage patterns and a "
        "numbered list of new prompts, assign each prompt to the single best-matching "
        "pattern id, or 'none' if it does not clearly belong to any. Return ONLY JSON "
        'of the form {"assignments": {"<idx>": "<pattern_id or none>"}}.'
    )
    user = f"EXISTING PATTERNS:\n{plist}\n\nNEW PROMPTS:\n{numbered}\n\nReturn the JSON now."
    text = call_fmapi_chat(
        [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        request=request,
        model=FMAPI_SONNET,
        max_tokens=6000,
        temperature=0.2,
    )
    return parse_json_loose(text).get("assignments", {})


@app.post("/api/refresh")
def refresh(request: Request, window_days: int = 14):
    res = load_results()
    if res.get("status") == "pending":
        res = {"patterns": [], "skills": [], "summary": {}, "overview": {}}
    assignments = load_assignments()

    candidates = _gather_candidates(request, window_days)
    new_cands = [c for c in candidates if c["h"] not in assignments]
    if not new_cands:
        return {
            "new_prompts": 0,
            "assigned": {},
            "unassigned": 0,
            "new_patterns": [],
            "new_skills": [],
            "window_days": window_days,
        }

    patterns = res.get("patterns", [])
    # Classify (chunk into batches of 150 to keep the FMAPI prompt manageable).
    assign_map = {}  # global candidate index -> pattern_id|none
    BATCH = 150
    try:
        for start in range(0, len(new_cands), BATCH):
            chunk = new_cands[start : start + BATCH]
            local = _classify_batch(patterns, chunk, request)
            for k, v in local.items():
                try:
                    gi = start + int(k)
                except (ValueError, TypeError):
                    continue
                if 0 <= gi < len(new_cands):
                    assign_map[gi] = v
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Classification failed: {exc}"}, status_code=502)

    valid_pids = {p["id"] for p in patterns}
    assigned_counts = Counter()
    none_idxs = []
    for i, c in enumerate(new_cands):
        pid = assign_map.get(i, "none")
        if pid in valid_pids:
            assignments[c["h"]] = pid
            assigned_counts[pid] += 1
        else:
            none_idxs.append(i)

    # Bump matching patterns' prompt_count and per-prompt user sets.
    for p in patterns:
        n = assigned_counts.get(p["id"], 0)
        if n:
            p["prompt_count"] = (p.get("prompt_count") or 0) + n

    new_patterns_named = []
    new_skills_named = []
    absorbed_into_new = 0  # unassigned prompts that became members of a new pattern

    # If enough unassigned, ask sonnet whether they form coherent NEW pattern(s).
    if len(none_idxs) >= 6:
        none_prompts = [new_cands[i]["prompt"] for i in none_idxs]
        numbered = "\n".join(f"{j}. {p[:500]}" for j, p in enumerate(none_prompts))
        sys = (
            "You are a prompt-clustering analyst. The following prompts did NOT match "
            "any existing pattern. Identify whether they form one or more coherent NEW "
            "usage patterns. Only propose a pattern if at least 6 prompts support it. "
            'Return ONLY JSON: {"new_patterns": [{"name": "...", "description": "...", '
            '"prompt_idxs": [int, ...]}]}'
        )
        try:
            text = call_fmapi_chat(
                [{"role": "system", "content": sys},
                 {"role": "user", "content": numbered + "\n\nReturn the JSON now."}],
                request=request, model=FMAPI_SONNET, max_tokens=6000, temperature=0.2,
            )
            proposed = parse_json_loose(text).get("new_patterns", [])
        except Exception:  # noqa: BLE001
            proposed = []

        existing_ids = [int(p["id"][1:]) for p in patterns if re.match(r"^p\d+$", p.get("id", ""))]
        next_pid = (max(existing_ids) + 1) if existing_ids else 1

        for np in proposed:
            idxs = [j for j in (np.get("prompt_idxs") or []) if isinstance(j, int) and 0 <= j < len(none_prompts)]
            if len(idxs) < 6:
                continue
            member_cands = [new_cands[none_idxs[j]] for j in idxs]
            users = {c["user_email"] for c in member_cands}
            prompts_txt = [c["prompt"] for c in member_cands]
            pid = f"p{next_pid}"
            next_pid += 1
            pattern = {
                "id": pid,
                "name": np.get("name", "Emerging Pattern"),
                "description": np.get("description", ""),
                "prompt_count": len(member_cands),
                "user_count": len(users),
                "total_tokens": sum(len(p) // 4 + 150 for p in prompts_txt),
                "example_prompts": prompts_txt[:3],
                "purity_pct": None,
                "dominant_latent": "live",
                "status": "emerging",
            }
            patterns.append(pattern)
            new_patterns_named.append(pattern["name"])
            absorbed_into_new += len(member_cands)
            # Record assignments for the members.
            for c in member_cands:
                assignments[c["h"]] = pid

            # Design a skill for the new pattern (reuse the existing skill shape).
            try:
                skill = _design_skill(pattern, prompts_txt, len(users), window_days, request, res)
                if skill:
                    res.setdefault("skills", []).append(skill)
                    new_skills_named.append(skill.get("title") or skill.get("name"))
            except Exception:  # noqa: BLE001 — skill design is best-effort
                pass

    # Recompute summary consolidation vs new total prompt count.
    total_prompts = sum(p.get("prompt_count") or 0 for p in patterns)
    consolidated = total_prompts  # all clustered prompts are "consolidated"
    summary = res.setdefault("summary", {})
    sm_overview = res.get("overview", {})
    base_total = sm_overview.get("total_prompts") or total_prompts
    grand_total = max(base_total, total_prompts)
    summary["prompts_consolidated"] = consolidated
    if grand_total:
        summary["prompts_consolidated_pct"] = round(100.0 * consolidated / grand_total, 1)
    summary["skills_recommended"] = len(res.get("skills", []))

    res["patterns"] = patterns
    res["refreshed_at"] = datetime.utcnow().isoformat() + "Z"

    # Persist results + assignments. NOTE: the Apps container FS is ephemeral —
    # these writes survive only for the life of the running container (fine for a
    # demo / live re-classification session).
    try:
        with open(RESULTS_PATH, "w") as f:
            json.dump(res, f, indent=2)
        with open(ASSIGNMENTS_PATH, "w") as f:
            json.dump(assignments, f, indent=2)
    except OSError:
        pass

    return {
        "new_prompts": len(new_cands),
        "assigned": dict(assigned_counts),
        "unassigned": len(none_idxs) - absorbed_into_new,
        "new_patterns": new_patterns_named,
        "new_skills": new_skills_named,
        "window_days": window_days,
    }


def _design_skill(pattern, prompts_txt, users_covered, window_days, request, res):
    """Design a skill spec for an emerging pattern, reusing results.skills shape.

    `res` is the live in-memory results dict — IDs are computed against it so
    multiple new skills in one refresh get distinct ids (s9, s10, ...).
    """
    shape_hint = json.dumps(
        {
            "name": "kebab-case-id",
            "title": "Human Title",
            "description": "what it does",
            "template": "full reusable prompt template with {placeholders}",
            "parameters": [{"name": "x", "description": "..."}],
            "example_invocation": "the template filled with a realistic example",
        }
    )
    sys = (
        "You are a prompt-engineering expert. Design ONE reusable skill (a "
        "parameterized prompt template) that would replace the ad-hoc prompts "
        "below. Return ONLY JSON matching this shape: " + shape_hint
    )
    examples = "\n".join(f"- {p[:400]}" for p in prompts_txt[:8])
    user = (
        f"PATTERN: {pattern['name']} — {pattern.get('description','')}\n\n"
        f"EXAMPLE PROMPTS:\n{examples}\n\nReturn the JSON now."
    )
    text = call_fmapi_chat(
        [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        request=request, model=FMAPI_SONNET, max_tokens=6000, temperature=0.2,
    )
    spec = parse_json_loose(text)

    n = len(prompts_txt)
    est_input_tokens = sum(len(p) // 4 + 150 for p in prompts_txt)
    days = window_days if window_days and window_days > 0 else 30
    prompts_per_month = round(n * 30 / days)
    est_monthly_tokens = round(est_input_tokens * 30 / days)
    est_savings = round(est_monthly_tokens * 0.30)

    existing_ids = [int(s["id"][1:]) for s in res.get("skills", []) if re.match(r"^s\d+$", s.get("id", ""))]
    next_sid = (max(existing_ids) + 1) if existing_ids else 1

    return {
        "id": f"s{next_sid}",
        "pattern_id": pattern["id"],
        "name": spec.get("name", pattern["name"].lower().replace(" ", "-")),
        "title": spec.get("title", pattern["name"]),
        "description": spec.get("description", pattern.get("description", "")),
        "template": spec.get("template", ""),
        "parameters": spec.get("parameters", []),
        "example_invocation": spec.get("example_invocation", ""),
        "value": {
            "users_covered": users_covered,
            "prompt_count": n,
            "prompts_per_month_est": prompts_per_month,
            "est_monthly_tokens": est_monthly_tokens,
            "input_token_savings_pct": 30,
            "est_monthly_token_savings": est_savings,
            "priority": "emerging",
        },
        "quality_ab": None,
        "status": "emerging",
    }


# --------------------------------------------------------------------------- #
# Static
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(STATIC_DIR)), name="static")
