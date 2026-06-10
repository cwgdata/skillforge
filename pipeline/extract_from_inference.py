"""Rebuild gateway_usage.json FROM the real AI Gateway inference table.

Reads the endpoint's payload table (created by inference_table_config), parses
each request's last user message, and joins back to the original
gateway_usage UC table on exact prompt text to recover the synthetic user
identity and ground-truth label. Output: the same gateway_usage.json schema
the engine consumes — so mine_and_design.py runs unchanged, but its input now
provably came from the inference table.

Env: SKILLFORGE_PAYLOAD_TABLE (default <catalog>.<schema>.fmapi_haiku_payload)
"""
import json
import os
import sys
from pathlib import Path

from common import CATALOG, SCHEMA, run_sql

OUT = Path(__file__).resolve().parent.parent / "app" / "data" / "gateway_usage.json"
PAYLOAD_TABLE = os.environ.get(
    "SKILLFORGE_PAYLOAD_TABLE", f"{CATALOG}.{SCHEMA}.fmapi_haiku_payload"
)
MAPPING_TABLE = f"{CATALOG}.{SCHEMA}.gateway_usage"


def main():
    rows = run_sql(f"""
        SELECT request_id, request, request_time
          FROM {PAYLOAD_TABLE}
         WHERE status_code = 200
         ORDER BY request_time
    """)
    print(f"{len(rows)} payload rows in {PAYLOAD_TABLE}")

    # Extract the last user-message content from each request payload.
    prompts = {}  # prompt text -> (inference request_id, request_time)
    for req_id, request_json, req_time in rows:
        try:
            req = json.loads(request_json)
            user_msgs = [m for m in req.get("messages", []) if m.get("role") == "user"]
            if user_msgs:
                prompts[user_msgs[-1]["content"]] = (req_id, str(req_time))
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
    print(f"{len(prompts)} distinct prompts extracted from payloads")

    # Join to the mapping table for synthetic identity + token counts + label.
    mapping = run_sql(
        f"SELECT request_id, event_time, user_email, endpoint_name, prompt, "
        f"input_tokens, output_tokens FROM {MAPPING_TABLE}"
    )
    out, missed = [], 0
    for req_id, event_time, user, endpoint, prompt, in_tok, out_tok in mapping:
        hit = prompts.get(prompt)
        if not hit:
            missed += 1
            continue
        out.append({
            "request_id": hit[0],            # the REAL inference-table request id
            "event_time": str(event_time),
            "user_email": user, "endpoint_name": endpoint, "prompt": prompt,
            "input_tokens": int(in_tok), "output_tokens": int(out_tok),
            "latent_pattern": "unknown",     # ground truth lives only in the original JSON
            "source": "inference_table",
        })
    print(f"matched {len(out)} prompts to identities ({missed} unmatched)")
    if len(out) < 200:
        print("WARNING: fewer than expected — inference-table batching may not have "
              "flushed yet (10-30 min). Re-run later.")
        return 1

    # Preserve ground-truth labels from the original file for the purity check.
    if OUT.exists():
        truth = {r["prompt"]: r.get("latent_pattern", "unknown")
                 for r in json.loads(OUT.read_text())}
        for r in out:
            r["latent_pattern"] = truth.get(r["prompt"], "unknown")

    OUT.write_text(json.dumps(out, indent=1))
    print(f"wrote {len(out)} rows -> {OUT} (source: {PAYLOAD_TABLE})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
