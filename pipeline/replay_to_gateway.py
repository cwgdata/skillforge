"""Replay the synthetic prompts through the AI Gateway so they land in the
endpoint's REAL inference table (payload logging must be enabled first).

Identity note: every replayed call is made by whoever runs this script, so the
inference table's `requester` column won't carry the 30 synthetic users. We
recover identity downstream by joining the payload's prompt text back to the
gateway_usage mapping table (exact-match on prompt). max_tokens is kept tiny —
the engine only mines the *prompts*; responses are irrelevant.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from common import GATEWAY_URL, token

USAGE = Path(__file__).resolve().parent.parent / "app" / "data" / "gateway_usage.json"
MODEL = "databricks-claude-haiku-4-5"
WORKERS = 8


def send(row):
    r = requests.post(
        f"{GATEWAY_URL}/chat/completions",
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        json={"model": MODEL, "messages": [{"role": "user", "content": row["prompt"]}],
              "max_tokens": 16},
        timeout=120,
    )
    return row["request_id"], r.status_code


def main():
    rows = json.loads(USAGE.read_text())
    print(f"replaying {len(rows)} prompts through {GATEWAY_URL} ({WORKERS} workers)")
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
    print(f"done: ok={ok} fail={fail} in {time.time()-t0:.0f}s")
    print("NOTE: inference-table writes are batched — expect 10-30 min before rows appear.")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
