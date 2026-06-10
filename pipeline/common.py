"""Shared helpers: auth, FMAPI-via-AI-Gateway, and SQL statement execution."""
import json
import os
import subprocess
import time

import requests

def _require(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise RuntimeError(f"Set {name} in the environment (see README: Configuration).")
    return v


HOST = _require("DATABRICKS_HOST").rstrip("/")
WORKSPACE_ID = _require("DATABRICKS_WORKSPACE_ID")
GATEWAY_URL = f"https://{WORKSPACE_ID}.ai-gateway.cloud.databricks.com/mlflow/v1"
WAREHOUSE_ID = _require("SQL_WAREHOUSE_ID")
CATALOG = os.environ.get("SKILLFORGE_CATALOG", "main")
SCHEMA = os.environ.get("SKILLFORGE_SCHEMA", "skillforge")

_token_cache = {"token": None, "ts": 0}


def token() -> str:
    # OAuth tokens last ~1h; refresh after 50 min
    if _token_cache["token"] and time.time() - _token_cache["ts"] < 3000:
        return _token_cache["token"]
    out = subprocess.run(
        ["databricks", "auth", "token", "--host", HOST],
        capture_output=True, text=True, check=True,
    )
    _token_cache["token"] = json.loads(out.stdout)["access_token"]
    _token_cache["ts"] = time.time()
    return _token_cache["token"]


def chat(messages, model="databricks-claude-haiku-4-5", max_tokens=4096, temperature=1.0, retries=3):
    """Chat completion through the AI Gateway (so calls land in system.ai_gateway.usage)."""
    for attempt in range(retries):
        r = requests.post(
            f"{GATEWAY_URL}/chat/completions",
            headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
            timeout=180,
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        if r.status_code in (429, 500, 502, 503) and attempt < retries - 1:
            time.sleep(5 * (attempt + 1))
            continue
        raise RuntimeError(f"FMAPI {r.status_code}: {r.text[:300]}")


def chat_json(messages, **kw):
    """Chat that must return a JSON document; strips code fences and parses."""
    text = chat(messages, **kw).strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def run_sql(statement: str, wait="50s"):
    r = requests.post(
        f"{HOST}/api/2.0/sql/statements",
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        json={"warehouse_id": WAREHOUSE_ID, "statement": statement, "wait_timeout": wait},
        timeout=120,
    )
    d = r.json()
    state = d.get("status", {}).get("state")
    # Poll if still pending
    while state in ("PENDING", "RUNNING"):
        time.sleep(3)
        sid = d["statement_id"]
        d = requests.get(
            f"{HOST}/api/2.0/sql/statements/{sid}",
            headers={"Authorization": f"Bearer {token()}"}, timeout=60,
        ).json()
        state = d.get("status", {}).get("state")
    if state != "SUCCEEDED":
        raise RuntimeError(f"SQL failed ({state}): {d.get('status',{}).get('error',{}).get('message','')[:300]}\n  stmt: {statement[:150]}")
    return d.get("result", {}).get("data_array") or []


def sql_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"
