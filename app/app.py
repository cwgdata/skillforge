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
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import requests
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
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
INFERENCE_TABLE = os.environ.get(
    "SKILLFORGE_INFERENCE_TABLE", f"{CATALOG}.{SCHEMA}.fmapi_haiku_payload"
)
INJECTED_TABLE = f"{CATALOG}.{SCHEMA}.injected_prompts"
MINING_TABLE = f"{CATALOG}.{SCHEMA}.mining_config"
# Per-user runtime overlay (results_doc / assignments_doc). The baseline
# results.json shipped with the app is shared + READ-ONLY; everything a user
# mutates at runtime lives in one row of this table, keyed by user_email.
USER_STATE_TABLE = f"{CATALOG}.{SCHEMA}.user_state"

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


def atomic_write_json(path, obj):
    """Write JSON to a temp file then os.replace into place so concurrent
    readers (load_results/load_assignments) never observe a partial file."""
    tmp = Path(str(path) + f".tmp.{os.getpid()}.{threading.get_ident()}")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


class _TokenShim:
    """Minimal request-like object carrying a captured OBO token in .headers.

    The background refresh thread has no FastAPI Request, so we capture the
    signed-in user's OBO token before starting the thread and wrap it here.
    token_candidates()/run_sql()/call_fmapi* all only read
    request.headers.get('x-forwarded-access-token'), so this is sufficient.
    """

    def __init__(self, obo_token):
        self.headers = {"x-forwarded-access-token": obo_token} if obo_token else {}


# --------------------------------------------------------------------------- #
# Per-user state layer
#
# Each signed-in user gets their OWN analysis view. The baseline results.json
# (and an empty assignments map) is the shared, READ-ONLY starting point;
# everything a user changes at runtime — refresh classification, emerging
# patterns/skills, on-demand quality_ab — lives in a per-user overlay persisted
# to the {CATALOG}.{SCHEMA}.user_state UC table. We keep an in-process
# write-through cache (dict + lock) so repeated reads don't hit UC.
# --------------------------------------------------------------------------- #
# user_key -> {"results": dict, "assignments": dict, "overlay": bool}. Whole-
# entry swaps under the lock keep readers from observing a half-updated overlay.
# "overlay" distinguishes a real personal overlay (a row exists / will be
# written) from a cached baseline fallback (read-through that hasn't mutated
# anything) — only the former counts as a "personal view".
_USER_STATE_CACHE = {}
_USER_STATE_LOCK = threading.Lock()


def user_key(request):
    """Stable per-user key: the signed-in email (lowercased).

    Falls back to "service-principal" in-app when there is no OBO identity, and
    "local-dev" when running outside Databricks Apps.
    """
    ident = request_identity(request)
    email = (ident.get("email") or "").strip().lower()
    if email:
        return email
    if IS_DATABRICKS_APP:
        return "service-principal"
    return "local-dev"


def _parse_doc(raw, fallback):
    """Defensively parse a JSON doc string from UC; fall back on any error."""
    if raw is None:
        return fallback
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback


def load_user_state(request):
    """Return (results_doc, assignments_doc) for the current user.

    Cache first; else SELECT the user's row from user_state; else fall back to
    the shared baseline (results.json + empty assignments). The returned dicts
    are the live cache copies — callers mutate then call save_user_state.
    """
    key = user_key(request)
    with _USER_STATE_LOCK:
        cached = _USER_STATE_CACHE.get(key)
        if cached is not None:
            return cached["results"], cached["assignments"]

    results_doc = None
    assignments_doc = None
    overlay = False
    try:
        rows = run_sql(
            f"SELECT results_doc, assignments_doc FROM {USER_STATE_TABLE} "
            f"WHERE user_email = {sql_str(key)}",
            request=request,
        )
        if rows and rows[0]:
            results_doc = _parse_doc(rows[0][0], None)
            assignments_doc = _parse_doc(rows[0][1] if len(rows[0]) > 1 else None, None)
            overlay = results_doc is not None
    except Exception:  # noqa: BLE001 — table may be unreachable; use baseline
        results_doc = None
        assignments_doc = None

    if results_doc is None:
        results_doc = load_results()
    if assignments_doc is None:
        assignments_doc = load_assignments()

    with _USER_STATE_LOCK:
        # Another thread may have populated the cache while we hit UC; re-check.
        cached = _USER_STATE_CACHE.get(key)
        if cached is not None:
            return cached["results"], cached["assignments"]
        _USER_STATE_CACHE[key] = {
            "results": results_doc,
            "assignments": assignments_doc,
            "overlay": overlay,
        }
    return results_doc, assignments_doc


def has_user_overlay(request):
    """True if this user has a real personal overlay (a UC row / pending write).

    A cached baseline fallback (a pure read-through that mutated nothing) does
    NOT count — only entries flagged overlay=True, or a row in UC, do.
    """
    key = user_key(request)
    with _USER_STATE_LOCK:
        cached = _USER_STATE_CACHE.get(key)
        if cached is not None:
            return bool(cached.get("overlay"))
    try:
        rows = run_sql(
            f"SELECT 1 FROM {USER_STATE_TABLE} WHERE user_email = {sql_str(key)} LIMIT 1",
            request=request,
        )
        return bool(rows)
    except Exception:  # noqa: BLE001
        return False


def save_user_state(request, results_doc, assignments_doc, key=None):
    """Cache update + UC upsert of the user's overlay.

    The in-memory cache is updated atomically (whole-tuple swap under the lock)
    regardless of UC outcome. The UC write runs in the calling thread (the
    refresh path already runs in a background thread, which is fine). Returns a
    "persist_warning" string on UC failure, else None — callers fold it into
    their response rather than failing the request.
    """
    if key is None:
        key = user_key(request)
    with _USER_STATE_LOCK:
        _USER_STATE_CACHE[key] = {
            "results": results_doc,
            "assignments": assignments_doc,
            "overlay": True,
        }

    rdoc = json.dumps(results_doc, separators=(",", ":"))
    adoc = json.dumps(assignments_doc, separators=(",", ":"))
    try:
        run_sql(
            f"DELETE FROM {USER_STATE_TABLE} WHERE user_email = {sql_str(key)}",
            request=request,
        )
        run_sql(
            f"INSERT INTO {USER_STATE_TABLE} "
            f"(user_email, results_doc, assignments_doc, updated_at) "
            f"VALUES ({sql_str(key)}, {sql_str(rdoc)}, {sql_str(adoc)}, now())",
            request=request,
        )
    except Exception as exc:  # noqa: BLE001 — keep cache, surface a warning
        return f"State persisted in-memory only (UC write failed: {str(exc)[:200]})"
    return None


def reset_user_state(request):
    """Drop the user's overlay from the cache and UC. Returns persist_warning."""
    key = user_key(request)
    with _USER_STATE_LOCK:
        _USER_STATE_CACHE.pop(key, None)
    try:
        run_sql(
            f"DELETE FROM {USER_STATE_TABLE} WHERE user_email = {sql_str(key)}",
            request=request,
        )
    except Exception as exc:  # noqa: BLE001
        return f"Cache cleared; UC delete failed: {str(exc)[:200]}"
    return None


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


# Allow hyphens: real inference tables are named e.g.
# databricks-claude-haiku-4-5_payload. Backtick-quoting makes them safe in SQL.
_IDENT_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+){0,2}$")


def safe_table_ident(name):
    """Validate a `catalog.schema.table` identifier before splicing it into a
    FROM/WHERE clause. Scan-derived table names (from serving-endpoint configs
    and system.ai_gateway.usage) are workspace-controllable, so reject anything
    that isn't a plain dotted identifier. Returns a backtick-quoted form."""
    n = (name or "").strip().strip("`")
    if not _IDENT_RE.match(n):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return ".".join(f"`{part}`" for part in n.split("."))


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
def results(request: Request):
    results_doc, _ = load_user_state(request)
    # The shipped baseline's generated_at, surfaced so the UI can show when the
    # shared engine baseline was produced even on a personalized view.
    baseline = load_results()
    baseline_generated_at = (
        baseline.get("generated_at") if isinstance(baseline, dict) else None
    )
    personal = has_user_overlay(request)
    out = dict(results_doc) if isinstance(results_doc, dict) else {"status": "pending"}
    if out.get("status") == "pending":
        # Surface how much traffic is already waiting to be mined: the bundled
        # usage snapshot plus (best-effort) live injected prompts.
        backlog = {"snapshot_prompts": len(load_usage()), "injected_prompts": None}
        try:
            rows = run_sql(f"SELECT count(*) FROM {INJECTED_TABLE}", request=request)
            backlog["injected_prompts"] = int(rows[0][0]) if rows and rows[0] else 0
        except Exception:  # noqa: BLE001 — table may not exist yet
            pass
        out["backlog"] = backlog
    out["view"] = {
        "user": user_key(request),
        "personal": bool(personal),
        "baseline_generated_at": baseline_generated_at,
    }
    return JSONResponse(out)


@app.get("/api/whoami")
def whoami(request: Request):
    ident = request_identity(request)
    personal = has_user_overlay(request)
    if ident["auth_mode"] in ("obo", "service_principal"):
        return {
            "email": ident["email"] or "service-principal",
            "auth_mode": ident["auth_mode"],
            "personal_view": bool(personal),
        }
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
    return {"email": email, "auth_mode": "local", "personal_view": bool(personal)}


def _real_tokens_by_endpoint(window_days, request):
    """REAL per-endpoint token volume from system.ai_gateway.usage for THIS
    workspace (the synthetic corpus's endpoint column is random noise). Returns
    [{endpoint, tokens}] or [] if the system table is unreadable."""
    ws_id = AI_GATEWAY_URL.split("//")[-1].split(".")[0] if AI_GATEWAY_URL else ""
    if not ws_id.isdigit():
        return []
    days = int(window_days) if window_days and window_days > 0 else 30
    try:
        rows = run_sql(
            "SELECT endpoint_name, SUM(total_tokens) AS t FROM system.ai_gateway.usage "
            f"WHERE workspace_id = '{ws_id}' AND event_time > now() - INTERVAL {days} DAYS "
            "GROUP BY endpoint_name HAVING t > 0 ORDER BY t DESC",
            request=request,
        )
        return [{"endpoint": r[0] or "unknown", "tokens": int(r[1] or 0)} for r in rows]
    except Exception:  # noqa: BLE001 — system table unreadable for this principal
        return []


def _usage_stats_snapshot(window_days, source="snapshot", request=None):
    """Compute usage stats from the gateway_usage.json snapshot. Tokens-by-
    endpoint is sourced from REAL gateway usage where available (the snapshot's
    endpoint tags are synthetic)."""
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

    real_tokens = _real_tokens_by_endpoint(window_days, request) if request is not None else []
    tokens_by_endpoint = real_tokens or [
        {"endpoint": e, "tokens": t}
        for e, t in sorted(tokens_per_endpoint.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return {
        "source": source,
        "prompts_per_day": [{"date": d, "count": c} for d, c in sorted(per_day.items())],
        "prompts_per_user": [{"user": u, "count": c} for u, c in per_user.most_common(10)],
        "tokens_by_endpoint": tokens_by_endpoint,
        "tokens_source": "gateway_usage_system_table" if real_tokens else "synthetic_snapshot",
        "total_rows": len(rows),
        "window_days": window_days,
    }


# UC usage stats are cached in-process for 30s, keyed by window_days.
_UC_STATS_CACHE = {}  # window_days -> {"ts": float, "data": dict}
USAGE_TABLE = f"{CATALOG}.{SCHEMA}.gateway_usage"


def _usage_stats_uc(window_days, request):
    """Compute usage stats live from the {CATALOG}.{SCHEMA}.gateway_usage UC
    table. The window is relative to MAX(event_time) in the table (matching the
    snapshot's data-relative semantics). Raises on any failure (caller falls
    back to the snapshot)."""
    # Window filter expressed in SQL, relative to MAX(event_time).
    if window_days and window_days > 0:
        where = (
            f"WHERE event_time >= ("
            f"SELECT MAX(event_time) - INTERVAL {int(window_days)} DAYS FROM {USAGE_TABLE})"
        )
    else:
        where = ""

    per_day_rows = run_sql(
        f"SELECT CAST(event_time AS DATE) AS d, COUNT(*) AS c "
        f"FROM {USAGE_TABLE} {where} GROUP BY 1 ORDER BY 1",
        request=request,
    )
    per_user_rows = run_sql(
        f"SELECT user_email, COUNT(*) AS c FROM {USAGE_TABLE} {where} "
        f"GROUP BY user_email ORDER BY c DESC LIMIT 10",
        request=request,
    )
    total_rows = run_sql(f"SELECT COUNT(*) FROM {USAGE_TABLE} {where}", request=request)
    total = int(total_rows[0][0]) if total_rows and total_rows[0] else 0

    # Tokens-by-endpoint comes from REAL gateway usage (the corpus endpoint
    # column is synthetic); fall back to the corpus sum only if unreadable.
    real_tokens = _real_tokens_by_endpoint(window_days, request)
    if not real_tokens:
        tok_rows = run_sql(
            f"SELECT endpoint_name, SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) AS t "
            f"FROM {USAGE_TABLE} {where} GROUP BY endpoint_name ORDER BY t DESC",
            request=request,
        )
        real_tokens = [{"endpoint": r[0] or "unknown", "tokens": int(r[1] or 0)} for r in tok_rows]

    return {
        "source": "uc",
        "prompts_per_day": [
            {"date": str(r[0]), "count": int(r[1] or 0)} for r in per_day_rows
        ],
        "prompts_per_user": [
            {"user": r[0] or "unknown", "count": int(r[1] or 0)} for r in per_user_rows
        ],
        "tokens_by_endpoint": real_tokens,
        "tokens_source": "gateway_usage_system_table",
        "total_rows": total,
        "window_days": window_days,
    }


@app.get("/api/usage/stats")
def usage_stats(request: Request, window_days: int = 0, source: str = "snapshot"):
    if source != "uc":
        return _usage_stats_snapshot(window_days, request=request)

    now = time.time()
    cached = _UC_STATS_CACHE.get(window_days)
    if cached and (now - cached["ts"]) < 30:
        return cached["data"]
    try:
        data = _usage_stats_uc(window_days, request)
        _UC_STATS_CACHE[window_days] = {"ts": now, "data": data}
        return data
    except Exception:  # noqa: BLE001 — fall back to the snapshot computation
        return _usage_stats_snapshot(window_days, source="snapshot_fallback", request=request)


# --------------------------------------------------------------------------- #
# Test bench
# --------------------------------------------------------------------------- #
class TestSkillRequest(BaseModel):
    skill_id: str
    parameters: dict = {}
    raw_prompt: str | None = None


@app.post("/api/test_skill")
def test_skill(req: TestSkillRequest, request: Request):
    res, _ = load_user_state(request)
    if not isinstance(res, dict) or res.get("status") == "pending":
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
def _mining_overrides(request):
    """table_name -> enabled from the UC mining_config table ({} on failure)."""
    try:
        rows = run_sql(
            f"SELECT table_name, enabled FROM {MINING_TABLE}", request=request
        )
        return {r[0]: str(r[1]).lower() == "true" for r in rows if r and r[0]}
    except Exception:  # noqa: BLE001 — table may not exist yet
        return {}


def _enabled_mining_tables(request):
    """Inference tables refresh should mine: discovered tables (via the scan)
    merged with mining_config overrides. Default for a discovered table with no
    override is ENABLED. Falls back to the built-in table if the scan fails."""
    discovered = []
    try:
        payload = endpoints_scan(request)
        if isinstance(payload, dict):
            discovered = [
                e["inference_table"]
                for e in payload.get("endpoints", [])
                if e.get("inference_table")
            ]
    except Exception:  # noqa: BLE001
        pass
    if not discovered:
        discovered = [INFERENCE_TABLE]
    overrides = _mining_overrides(request)
    return [tbl for tbl in discovered if overrides.get(tbl, True)]


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
    # Unity AI Gateway (V2) plane: the authoritative inference-table linkage +
    # token volume live in system.ai_gateway.usage. Legacy per-endpoint config
    # (above) does NOT capture gateway-subdomain traffic, so V2 wins when present.
    try:
        # system.ai_gateway.usage is METASTORE-wide; shared FMAPI endpoint names
        # collide across workspaces, so filter to this workspace (id = the
        # gateway URL subdomain) or another workspace's config/tokens leak in.
        ws_id = AI_GATEWAY_URL.split("//")[-1].split(".")[0] if AI_GATEWAY_URL else ""
        if not ws_id.isdigit():  # workspace id is always numeric — don't splice anything else
            ws_id = ""
        v2rows = run_sql(
            "SELECT endpoint_name, max(endpoint_metadata.inference_table), "
            "sum(total_tokens) FROM system.ai_gateway.usage "
            f"WHERE workspace_id = '{ws_id}' "
            "AND event_time > now() - INTERVAL 7 DAYS GROUP BY endpoint_name",
            request=request,
        )
        v2 = {r[0]: {"table": r[1], "tokens": int(r[2] or 0)} for r in v2rows if r and r[0]}
    except Exception:  # noqa: BLE001 — system table may be unreadable for this principal
        v2 = {}
    # system.ai_gateway.usage lags ~1-2h, so a freshly enabled V2 table won't
    # surface there yet. Bridge: if SKILLFORGE_INFERENCE_TABLE is configured and
    # its table prefix matches an endpoint name, attribute it directly.
    cfg_tbl = INFERENCE_TABLE.replace("`", "")
    cfg_ep = cfg_tbl.rsplit(".", 1)[-1].removesuffix("_payload") if cfg_tbl else ""
    for ep in out:
        info = v2.get(ep["name"]) or {}
        ep["tokens_7d"] = info.get("tokens") or 0
        if info.get("table"):
            ep["inference_table"] = info["table"]
            ep["plane"] = "v2"
        elif ep["name"] == cfg_ep:
            ep["inference_table"] = cfg_tbl
            ep["plane"] = "v2"
        elif ep["inference_table"]:
            ep["plane"] = "legacy"
        else:
            ep["plane"] = None
        ep["gateway_page"] = f"{WORKSPACE_HOST}/ml/ai-gateway/{ep['name']}"

    out.sort(key=lambda e: (e["inference_table"] is None, -(e.get("tokens_7d") or 0), e["name"] or ""))
    payload = {
        "endpoints": out,
        "configured": sum(1 for e in out if e["inference_table"]),
        "total": len(out),
    }
    _SCAN_CACHE["ts"] = now
    _SCAN_CACHE["data"] = payload
    return payload


@app.get("/api/mining/config")
def mining_config(request: Request):
    """Discovered inference tables with their mine-or-not state."""
    payload = endpoints_scan(request)
    if not isinstance(payload, dict):
        return payload  # propagate scan error response
    overrides = _mining_overrides(request)
    tables = [
        {
            "endpoint": e["name"],
            "table": e["inference_table"],
            "enabled": overrides.get(e["inference_table"], True),
        }
        for e in payload.get("endpoints", [])
        if e.get("inference_table")
    ]
    return {"tables": tables}


@app.post("/api/inject/clear")
def inject_clear(request: Request):
    """Permanently clear the shared injected-prompt history (all users' rows).

    Destructive by design — the UI gates this behind an explicit warning
    dialog. Only the injected_prompts feed is cleared; gateway usage, the
    inference tables, and per-user analysis overlays are untouched.
    """
    who = (request_identity(request).get("email") or "app").replace("'", "")
    try:
        before = run_sql(f"SELECT count(*) FROM {INJECTED_TABLE}", request=request)
        n = int(before[0][0]) if before and before[0] else 0
        run_sql(f"DELETE FROM {INJECTED_TABLE}", request=request)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Could not clear history: {exc}"}, status_code=502)
    return {"cleared": n, "by": who}


class MiningToggle(BaseModel):
    table: str
    enabled: bool


@app.post("/api/mining/toggle")
def mining_toggle(req: MiningToggle, request: Request):
    """Persist a mine/don't-mine decision for an inference table (UC-backed)."""
    table = req.table.strip()
    # Only accept tables we actually discovered on serving endpoints.
    payload = endpoints_scan(request)
    known = set()
    if isinstance(payload, dict):
        known = {e["inference_table"] for e in payload.get("endpoints", []) if e.get("inference_table")}
    if table not in known:
        return JSONResponse({"error": f"Unknown inference table '{table}'."}, status_code=404)
    who = (request_identity(request).get("email") or "app").replace("'", "")
    try:
        run_sql(
            f"CREATE TABLE IF NOT EXISTS {MINING_TABLE} "
            "(table_name STRING, enabled BOOLEAN, updated_at TIMESTAMP, updated_by STRING)",
            request=request,
        )
        run_sql(f"DELETE FROM {MINING_TABLE} WHERE table_name = {sql_str(table)}", request=request)
        run_sql(
            f"INSERT INTO {MINING_TABLE} (table_name, enabled, updated_at, updated_by) "
            f"VALUES ({sql_str(table)}, {str(req.enabled).upper()}, now(), {sql_str(who)})",
            request=request,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Could not persist toggle: {exc}"}, status_code=502)
    return {"table": table, "enabled": req.enabled, "updated_by": who}


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

    # (iii) live inference tables — every discovered table not toggled off in
    # mining_config; parse request JSON, take last user-role message
    for _mine_tbl in _enabled_mining_tables(request):
      try:
        _safe_tbl = safe_table_ident(_mine_tbl)  # reject hostile scan-derived names
        rows = run_sql(
            f"SELECT request, response FROM {_safe_tbl} WHERE status_code = 200",
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
      except Exception:  # noqa: BLE001 — table may not exist yet → skip this feed
        continue

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


# Background refresh job state. Guarded by _REFRESH_LOCK for all mutation.
_REFRESH_LOCK = threading.Lock()
_REFRESH_STATUS = {
    "state": "idle",  # idle | running | done | error
    "phase": "",
    "batches_done": 0,
    "batches_total": 0,
    "classified": 0,
    "total_candidates": 0,
    "started_at": None,  # epoch seconds
    "elapsed_s": 0,
    "result": None,
    "error": None,
    "user": None,  # user_key that owns the in-flight / last refresh
}


def _set_status(**kw):
    with _REFRESH_LOCK:
        _REFRESH_STATUS.update(kw)


def _run_refresh(token_shim, window_days, user_k):
    """Core re-classification logic. Runs in a daemon thread; updates the
    module-level _REFRESH_STATUS as it progresses. `token_shim` is a request-like
    object carrying the captured OBO token (or empty headers → SP fallback).
    `user_k` is the captured user_key for whom we re-classify + persist (the
    thread has no Request, so the caller resolves identity up front)."""
    request = token_shim
    try:
        # Load THIS user's current overlay (cache or UC), falling back to the
        # shared baseline. load_user_state derives its key from request identity,
        # but the shim carries no email — so seed the cache under user_k first
        # if it's missing, then read it back.
        with _USER_STATE_LOCK:
            cached = _USER_STATE_CACHE.get(user_k)
        if cached is not None:
            res, assignments = cached["results"], cached["assignments"]
        else:
            # Cache miss (e.g. after an app restart / on another replica): the
            # user may STILL have a persisted overlay in UC. Read it before
            # falling back to baseline — otherwise this refresh would start from
            # baseline and save_user_state would clobber their saved overlay.
            res, assignments = None, None
            try:
                rows = run_sql(
                    f"SELECT results_doc, assignments_doc FROM {USER_STATE_TABLE} "
                    f"WHERE user_email = {sql_str(user_k)}",
                    request=request,
                )
                if rows and rows[0]:
                    res = _parse_doc(rows[0][0], None)
                    assignments = _parse_doc(rows[0][1] if len(rows[0]) > 1 else None, None)
            except Exception:  # noqa: BLE001 — table unreachable → baseline
                res, assignments = None, None
            if res is None:
                res, assignments = load_results(), (assignments or load_assignments())
        if not isinstance(res, dict) or res.get("status") == "pending":
            res = {"patterns": [], "skills": [], "summary": {}, "overview": {}}
        if not isinstance(assignments, dict):
            assignments = {}

        _set_status(phase="gathering candidates")
        candidates = _gather_candidates(request, window_days)
        new_cands = [c for c in candidates if c["h"] not in assignments]
        _set_status(total_candidates=len(new_cands))

        if not new_cands:
            result = {
                "new_prompts": 0,
                "assigned": {},
                "unassigned": 0,
                "new_patterns": [],
                "new_skills": [],
                "window_days": window_days,
                "user": user_k,
            }
            _set_status(state="done", phase="done", result=result)
            return

        patterns = res.get("patterns", [])
        # Classify (chunk into batches of 150 to keep the FMAPI prompt manageable).
        assign_map = {}  # global candidate index -> pattern_id|none
        BATCH = 150
        batches_total = (len(new_cands) + BATCH - 1) // BATCH
        _set_status(phase="classifying", batches_total=batches_total)
        for bi, start in enumerate(range(0, len(new_cands), BATCH)):
            chunk = new_cands[start : start + BATCH]
            local = _classify_batch(patterns, chunk, request)
            for k, v in local.items():
                try:
                    gi = start + int(k)
                except (ValueError, TypeError):
                    continue
                if 0 <= gi < len(new_cands):
                    assign_map[gi] = v
            _set_status(batches_done=bi + 1, classified=min(start + len(chunk), len(new_cands)))

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
            _set_status(phase="detecting emerging patterns")
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
                _set_status(phase="designing skills")
                try:
                    skill = _design_skill(pattern, prompts_txt, len(users), window_days, request, res)
                    if skill:
                        res.setdefault("skills", []).append(skill)
                        new_skills_named.append(skill.get("title") or skill.get("name"))
                except Exception:  # noqa: BLE001 — skill design is best-effort
                    pass

        # Recompute summary consolidation vs new total prompt count. The
        # "prompts analyzed" KPI comes from overview.total_prompts, so bump it
        # by the candidates we just classified (previously it stayed stale).
        total_prompts = sum(p.get("prompt_count") or 0 for p in patterns)
        consolidated = total_prompts  # all clustered prompts are "consolidated"
        summary = res.setdefault("summary", {})
        overview = res.setdefault("overview", {})
        # Count only the NEWLY classified prompts — `candidates` is the full
        # deduped union of all feeds (mostly already-counted baseline prompts),
        # so adding len(candidates) double-counted and exploded the KPI.
        overview["total_prompts"] = (overview.get("total_prompts") or 0) + len(new_cands)
        grand_total = max(overview["total_prompts"], total_prompts)
        summary["prompts_consolidated"] = consolidated
        if grand_total:
            summary["prompts_consolidated_pct"] = round(100.0 * consolidated / grand_total, 1)
        summary["skills_recommended"] = len(res.get("skills", []))

        res["patterns"] = patterns
        res["refreshed_at"] = datetime.utcnow().isoformat() + "Z"

        # Persist this user's overlay (cache + UC upsert). The baseline
        # results.json is NEVER written — it stays the shared read-only start.
        _set_status(phase="persisting")
        persist_warning = save_user_state(request, res, assignments, key=user_k)

        result = {
            "new_prompts": len(new_cands),
            "assigned": dict(assigned_counts),
            "unassigned": len(none_idxs) - absorbed_into_new,
            "new_patterns": new_patterns_named,
            "new_skills": new_skills_named,
            "window_days": window_days,
            "user": user_k,
        }
        if persist_warning:
            result["persist_warning"] = persist_warning
        _set_status(state="done", phase="done", result=result)
    except Exception as exc:  # noqa: BLE001 — record failure for the status poller
        _set_status(state="error", phase="error", error=str(exc)[:400])


@app.post("/api/refresh")
def refresh(request: Request, window_days: int = 14):
    # Resolve the requesting user up front — the background thread has no
    # Request, so we capture both the OBO token AND the user_key here. The job
    # status / lock stay global-single-job (fine for the demo) but we record
    # which user owns the in-flight refresh so /status can report it.
    uk = user_key(request)
    # Only one refresh at a time.
    with _REFRESH_LOCK:
        if _REFRESH_STATUS["state"] == "running":
            return JSONResponse(
                {"error": "refresh already running", "user": _REFRESH_STATUS.get("user")},
                status_code=409,
            )
        job_id = sha1(str(time.time()) + str(threading.get_ident()))[:12]
        _REFRESH_STATUS.update(
            {
                "state": "running",
                "phase": "starting",
                "batches_done": 0,
                "batches_total": 0,
                "classified": 0,
                "total_candidates": 0,
                "started_at": time.time(),
                "elapsed_s": 0,
                "result": None,
                "error": None,
                "job_id": job_id,
                "user": uk,
            }
        )
    # Capture the OBO token BEFORE the thread starts — the thread has no Request.
    obo = request.headers.get("x-forwarded-access-token") if request is not None else None
    shim = _TokenShim(obo)
    t = threading.Thread(target=_run_refresh, args=(shim, window_days, uk), daemon=True)
    t.start()
    return JSONResponse({"job_id": job_id, "state": "running", "user": uk}, status_code=202)


@app.get("/api/refresh/status")
def refresh_status():
    with _REFRESH_LOCK:
        st = dict(_REFRESH_STATUS)
    started = st.get("started_at")
    if started:
        st["elapsed_s"] = round(time.time() - started, 1)
    return st


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
# Skill export
# --------------------------------------------------------------------------- #
def _find_skill(skill_id, res):
    skills = res.get("skills", []) if isinstance(res, dict) else []
    return next((s for s in skills if s.get("id") == skill_id), None)


def _skill_markdown(skill, res):
    """Render a skill as a ready-to-install Claude Code skill markdown doc."""
    name = skill.get("name") or skill.get("id") or "skill"
    title = skill.get("title") or name
    description = skill.get("description") or ""
    template = skill.get("template") or ""
    example = skill.get("example_invocation") or ""
    pattern_name = ""
    pid = skill.get("pattern_id")
    if pid and isinstance(res, dict):
        pat = next((p for p in res.get("patterns", []) if p.get("id") == pid), None)
        if pat:
            pattern_name = pat.get("name", "")

    lines = []
    # YAML frontmatter
    lines.append("---")
    lines.append(f"name: {name}")
    # keep description on one line for valid frontmatter
    one_line_desc = " ".join(description.splitlines()).strip()
    lines.append(f"description: {one_line_desc}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    lines.append("## When to use")
    lines.append("")
    when = description
    if pattern_name:
        when = f"{description}\n\nDerived from the usage pattern: **{pattern_name}**."
    lines.append(when)
    lines.append("")
    lines.append("## Template")
    lines.append("")
    lines.append("```")
    lines.append(template)
    lines.append("```")
    lines.append("")
    lines.append("## Parameters")
    lines.append("")
    params = skill.get("parameters") or []
    if params:
        lines.append("| Name | Description |")
        lines.append("| --- | --- |")
        for p in params:
            pn = str(p.get("name", "")).replace("|", "\\|")
            pd = " ".join(str(p.get("description", "")).splitlines()).replace("|", "\\|")
            lines.append(f"| `{pn}` | {pd} |")
    else:
        lines.append("_No parameters._")
    lines.append("")
    lines.append("## Example")
    lines.append("")
    lines.append("```")
    lines.append(example)
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


@app.get("/api/skills/{skill_id}/export")
def export_skill(skill_id: str, request: Request, format: str = "markdown"):
    res, _ = load_user_state(request)
    skill = _find_skill(skill_id, res)
    if skill is None:
        return JSONResponse({"error": f"Skill '{skill_id}' not found."}, status_code=404)
    name = skill.get("name") or skill.get("id") or "skill"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "skill"
    if format == "json":
        body = json.dumps(skill, indent=2)
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{safe}.json"'},
        )
    md = _skill_markdown(skill, res)
    return Response(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{safe}.md"'},
    )


# --------------------------------------------------------------------------- #
# Mining history (populated by the scheduled skillforge-auto-mine Job)
# --------------------------------------------------------------------------- #
HISTORY_TABLE = f"{CATALOG}.{SCHEMA}.mining_history"


@app.get("/api/history")
def mining_history(request: Request):
    try:
        rows = run_sql(
            "SELECT cast(snapshot_at as string), total_prompts, pattern_count, "
            f"skill_count, consolidated_pct FROM {HISTORY_TABLE} ORDER BY snapshot_at",
            request=request)
        return {"runs": [{"at": r[0], "prompts": r[1], "patterns": r[2],
                "skills": r[3], "consolidated_pct": r[4]} for r in rows]}
    except Exception:  # noqa: BLE001 — table exists only once auto-mine has run
        return {"runs": []}


# --------------------------------------------------------------------------- #
# Adoption tracking
#
# Once a skill is published to Genie Code, are people actually USING it? We
# can't see skill invocations directly, but we can read the live gateway feed:
# of recent prompts that match the skill's pattern, how many follow the
# published template's structure (= adopters) vs hand-rolled ad-hoc prompts.
# adoption_pct drives the REALIZED (not just estimated) token savings.
# --------------------------------------------------------------------------- #
ADOPTION_TABLE = f"{CATALOG}.{SCHEMA}.adoption_metrics"


@app.post("/api/skills/{skill_id}/adoption")
def measure_adoption(skill_id: str, request: Request, window_days: int = 14):
    res, _ = load_user_state(request)
    skill = _find_skill(skill_id, res)
    if skill is None:
        return JSONResponse({"error": f"Skill '{skill_id}' not found."}, status_code=404)
    pat = next((p for p in res.get("patterns", []) if p.get("id") == skill.get("pattern_id")), {})
    template = skill.get("template") or ""
    cands = _gather_candidates(request, window_days)
    if not cands:
        return {"skill_id": skill_id, "pattern_matches": 0, "adopted": 0,
                "adoption_pct": 0.0, "realized_monthly_token_savings": 0, "note": "no recent prompts to measure"}

    matches = adopted = 0
    BATCH = 80
    for start in range(0, len(cands), BATCH):
        chunk = cands[start:start + BATCH]
        numbered = "\n".join(f"{i}. {c['prompt'][:500]}" for i, c in enumerate(chunk))
        sys_msg = (
            "You judge prompt adoption of a published skill. For each prompt decide: "
            "matches_pattern (is it the same task as the skill) and uses_template (does it "
            "follow the skill's structured template — role/sections/named params — rather than "
            "an ad-hoc phrasing). Return ONLY JSON {\"r\": {\"<idx>\": {\"m\": bool, \"u\": bool}}}."
        )
        user_msg = (
            f"SKILL: {skill.get('title')}\nPATTERN: {pat.get('name','')} — {pat.get('description','')}\n"
            f"TEMPLATE:\n{template[:1200]}\n\nPROMPTS:\n{numbered}"
        )
        try:
            out = parse_json_loose(call_fmapi_chat(
                [{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}],
                request=request, model=FMAPI_SONNET, temperature=0.1, max_tokens=4000))
            for k, vv in (out.get("r") or {}).items():
                if vv.get("m"):
                    matches += 1
                    if vv.get("u"):
                        adopted += 1
        except Exception:  # noqa: BLE001 — skip a bad batch rather than fail the whole measure
            continue

    pct = round(100.0 * adopted / matches, 1) if matches else 0.0
    est_saved = int((skill.get("value", {}) or {}).get("est_monthly_token_savings") or 0)
    realized = int(est_saved * (adopted / matches)) if matches else 0
    try:
        run_sql(f"DELETE FROM {ADOPTION_TABLE} WHERE skill_id = {sql_str(skill_id)}", request=request)
        run_sql(
            f"INSERT INTO {ADOPTION_TABLE} (skill_id, measured_at, window_days, pattern_matches, adopted, adoption_pct, realized_monthly_token_savings) "
            f"VALUES ({sql_str(skill_id)}, now(), {int(window_days)}, {matches}, {adopted}, {pct}, {realized})",
            request=request)
    except Exception:  # noqa: BLE001
        pass
    return {"skill_id": skill_id, "pattern_matches": matches, "adopted": adopted,
            "adoption_pct": pct, "realized_monthly_token_savings": realized,
            "note": "Adoption = recent pattern-matching prompts that follow the published template's structure."}


@app.get("/api/adoption")
def list_adoption(request: Request):
    try:
        rows = run_sql(
            f"SELECT skill_id, adoption_pct, adopted, pattern_matches, realized_monthly_token_savings, cast(measured_at as string) FROM {ADOPTION_TABLE}",
            request=request)
        return {"adoption": {r[0]: {"pct": r[1], "adopted": r[2], "matches": r[3],
                "realized": r[4], "at": r[5]} for r in rows if r and r[0]}}
    except Exception:  # noqa: BLE001
        return {"adoption": {}}


# --------------------------------------------------------------------------- #
# Cost & ROI analytics
# --------------------------------------------------------------------------- #
# Blended $ per 1M input tokens. Pay-per-token FMAPI is DBU-priced and varies by
# model/contract, so this is an ILLUSTRATIVE default — override per deployment
# with SKILLFORGE_PRICE_PER_1M, or per request with ?price_per_1m=.
DEFAULT_PRICE_PER_1M = float(os.environ.get("SKILLFORGE_PRICE_PER_1M", "2.50"))


@app.get("/api/cost")
def cost(request: Request, price_per_1m: float | None = None, scale: float | None = None):
    price = price_per_1m if price_per_1m and price_per_1m > 0 else DEFAULT_PRICE_PER_1M
    # Org-projection multiplier: the mined corpus is a sample; `scale` extrapolates
    # to real traffic volume (e.g. if this is ~1% of prod, scale=100). Default 1.
    scale = scale if scale and scale > 0 else 1.0
    res, _ = load_user_state(request)
    rows, tot_saved, tot_spend = [], 0, 0
    for s in res.get("skills", []):
        v = s.get("value", {}) or {}
        saved = int(v.get("est_monthly_token_savings") or 0)
        spend = int(v.get("est_monthly_tokens") or 0)
        rows.append({
            "id": s.get("id"), "title": s.get("title") or s.get("name"),
            "priority": v.get("priority"),
            "monthly_tokens": int(spend * scale), "monthly_tokens_saved": int(saved * scale),
            "monthly_cost": round(spend * scale / 1e6 * price, 2),
            "monthly_savings": round(saved * scale / 1e6 * price, 2),
        })
        tot_saved += saved
        tot_spend += spend
    monthly = round(tot_saved * scale / 1e6 * price, 2)
    return {
        "price_per_1m": price, "scale": scale, "currency": "USD",
        "monthly_pattern_spend": round(tot_spend * scale / 1e6 * price, 2),
        "monthly_savings": monthly, "annual_savings": round(monthly * 12, 2),
        "skills": sorted(rows, key=lambda r: -r["monthly_savings"]),
        "note": "Illustrative blended rate × org-projection scale. Defaults are the mined sample at $%.2f/1M." % price,
    }


# --------------------------------------------------------------------------- #
# Publish to Genie Code (native skills: SKILL.md under .assistant/skills/)
# --------------------------------------------------------------------------- #
PUBLISHED_TABLE = f"{CATALOG}.{SCHEMA}.published_skills"
# Workspace-files root where Genie Code auto-discovers skills in Agent mode.
SKILLS_ROOT = os.environ.get("SKILLFORGE_SKILLS_ROOT", "/Workspace/.assistant/skills")


def _ws_api(method, path, body, request):
    """Call a Workspace REST API with OBO->SP token fallback."""
    resp = None
    for token in token_candidates(request):
        resp = requests.request(
            method, WORKSPACE_HOST + path, json=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code not in (401, 403):
            break
    try:
        return resp.status_code, resp.json()
    except Exception:  # noqa: BLE001
        return resp.status_code, resp.text[:200]


@app.post("/api/skills/{skill_id}/publish")
def publish_skill(skill_id: str, request: Request):
    """Write the skill as a Genie Code SKILL.md into the workspace skills dir so
    Genie Code Agent mode auto-discovers it; record it in published_skills."""
    import base64
    res, _ = load_user_state(request)
    skill = _find_skill(skill_id, res)
    if skill is None:
        return JSONResponse({"error": f"Skill '{skill_id}' not found."}, status_code=404)
    name = skill.get("name") or skill.get("id") or "skill"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "skill"
    folder = f"{SKILLS_ROOT}/{safe}"
    md = _skill_markdown(skill, res)

    # mkdirs (idempotent — ok if it already exists) then import the SKILL.md.
    _ws_api("POST", "/api/2.0/workspace/mkdirs", {"path": folder}, request)
    c, b = _ws_api("POST", "/api/2.0/workspace/import", {
        "path": f"{folder}/SKILL.md",
        "format": "RAW",
        "content": base64.b64encode(md.encode()).decode(),
        "overwrite": True,
    }, request)
    if c not in (200, 201):
        return JSONResponse({"error": f"import failed: {b}"}, status_code=502)

    who = (request_identity(request).get("email") or "app").replace("'", "")
    path = f"{folder}/SKILL.md"
    try:
        run_sql(
            f"CREATE TABLE IF NOT EXISTS {PUBLISHED_TABLE} (skill_id STRING, name STRING, "
            "title STRING, workspace_path STRING, version INT, published_at TIMESTAMP, published_by STRING)",
            request=request,
        )
        prev = run_sql(f"SELECT max(version) FROM {PUBLISHED_TABLE} WHERE skill_id = {sql_str(skill_id)}", request=request)
        ver = (int(prev[0][0]) + 1) if prev and prev[0] and prev[0][0] is not None else 1
        run_sql(f"DELETE FROM {PUBLISHED_TABLE} WHERE skill_id = {sql_str(skill_id)}", request=request)
        run_sql(
            f"INSERT INTO {PUBLISHED_TABLE} (skill_id, name, title, workspace_path, version, published_at, published_by) "
            f"VALUES ({sql_str(skill_id)}, {sql_str(name)}, {sql_str(skill.get('title') or name)}, "
            f"{sql_str(path)}, {ver}, now(), {sql_str(who)})",
            request=request,
        )
    except Exception as exc:  # noqa: BLE001 — file is written; tracking is best-effort
        return {"published": True, "path": path, "version": None, "persist_warning": str(exc)[:200]}
    return {"published": True, "path": path, "version": ver, "by": who}


@app.get("/api/published")
def list_published(request: Request):
    """skill_id -> {version, path, published_at} for published-state badges."""
    try:
        rows = run_sql(
            f"SELECT skill_id, version, workspace_path, cast(published_at as string) FROM {PUBLISHED_TABLE}",
            request=request,
        )
        return {"published": {r[0]: {"version": r[1], "path": r[2], "at": r[3]} for r in rows if r and r[0]}}
    except Exception:  # noqa: BLE001 — table may not exist yet
        return {"published": {}}


# --------------------------------------------------------------------------- #
# On-demand quality A/B
# --------------------------------------------------------------------------- #
class QualityABRequest(BaseModel):
    raw_prompt: str | None = None


@app.post("/api/skills/{skill_id}/quality_ab")
def quality_ab(skill_id: str, req: QualityABRequest, request: Request):
    res, assignments = load_user_state(request)
    if not isinstance(res, dict) or res.get("status") == "pending":
        return JSONResponse({"error": "Engine has not run yet."}, status_code=409)
    skills = res.get("skills", [])
    skill = next((s for s in skills if s.get("id") == skill_id), None)
    if skill is None:
        return JSONResponse({"error": f"Skill '{skill_id}' not found."}, status_code=404)

    # Raw arm: provided raw_prompt, else the pattern's first example_prompt,
    # else any example we can find.
    raw_prompt = (req.raw_prompt or "").strip()
    if not raw_prompt:
        pid = skill.get("pattern_id")
        pat = next((p for p in res.get("patterns", []) if p.get("id") == pid), None)
        examples = (pat.get("example_prompts") if pat else None) or []
        if not examples:
            # fallback: any pattern's example
            for p in res.get("patterns", []):
                if p.get("example_prompts"):
                    examples = p["example_prompts"]
                    break
        raw_prompt = examples[0] if examples else (skill.get("example_invocation") or "")

    skill_prompt = skill.get("example_invocation") or skill.get("template") or ""

    try:
        raw_answer, _ = call_fmapi(
            raw_prompt, request=request, model=FMAPI_HAIKU, max_tokens=700
        )
        skill_answer, _ = call_fmapi(
            skill_prompt, request=request, model=FMAPI_HAIKU, max_tokens=700
        )
        judge_sys = (
            "You are a strict evaluator of LLM answer quality. Score each of two "
            "answers on a 1-10 scale considering completeness, structure, and "
            "actionability (10 = best). Return ONLY JSON of the form "
            '{"raw_score": <int>, "skill_score": <int>, "rationale": "<one or two '
            'sentences comparing them>"}.'
        )
        judge_user = (
            f"RAW PROMPT:\n{raw_prompt}\n\nANSWER A (raw):\n{raw_answer}\n\n"
            f"SKILL PROMPT:\n{skill_prompt}\n\nANSWER B (skill):\n{skill_answer}\n\n"
            "Score ANSWER A as raw_score and ANSWER B as skill_score. Return the JSON now."
        )
        judged = call_fmapi_chat(
            [{"role": "system", "content": judge_sys},
             {"role": "user", "content": judge_user}],
            request=request, model=FMAPI_SONNET, max_tokens=700, temperature=0.2,
        )
        verdict = parse_json_loose(judged)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"FMAPI call failed: {exc}"}, status_code=502)

    ab = {
        "raw_score": verdict.get("raw_score"),
        "skill_score": verdict.get("skill_score"),
        "rationale": verdict.get("rationale", ""),
        "raw_prompt": raw_prompt[:600],
        "skill_prompt": skill_prompt[:600],
        "raw_answer": (raw_answer or "")[:600],
        "skill_answer": (skill_answer or "")[:600],
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

    # Persist into the skill's quality_ab in the USER's overlay (cache + UC).
    # This is the user's first mutation if they were on the shared baseline —
    # save_user_state copies the (possibly baseline-derived) doc into their row.
    skill["quality_ab"] = ab
    persist_warning = save_user_state(request, res, assignments)
    if persist_warning:
        return {**ab, "persist_warning": persist_warning}
    return ab


# --------------------------------------------------------------------------- #
# Per-user state reset
# --------------------------------------------------------------------------- #
@app.post("/api/state/reset")
def state_reset(request: Request):
    """Drop the current user's overlay (cache + UC row) so they fall back to the
    shared baseline view. Idempotent — a no-op if no overlay exists."""
    persist_warning = reset_user_state(request)
    out = {"reset": True, "user": user_key(request)}
    if persist_warning:
        out["persist_warning"] = persist_warning
    return out


# --------------------------------------------------------------------------- #
# Static
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(STATIC_DIR)), name="static")
