"""promptgen — standalone synthetic-prompt generation, importable + CLI.

The single home for example-prompt generation so other workflows can reuse it
without touching the usage-data scripts:

    from promptgen import generate_prompts
    prompts = generate_prompts("asking an LLM to review Terraform plans", 25)

or from a shell / another tool:

    python3 promptgen.py --brief "asking an LLM to review Terraform plans" -n 25 --json

Style defaults to ROUGH (how people actually type: typos, shorthand,
fragments, ~1-in-8 blended/hybrid asks). Pass rough=False / --smooth for
clean, well-formed prompts. Uses FMAPI via the AI Gateway through
common.chat_json, so the usual env (DATABRICKS_HOST, DATABRICKS_WORKSPACE_ID)
must be set — see setup.env.example.
"""
import argparse
import json
import sys

from common import chat_json

ROUGH_STYLE = (
    "Make them ROUGH and real, the way people actually type into a chat box at "
    "work: 5-90 words; many start lowercase or mid-thought ('ok so', 'hey quick "
    "one', 'plz'); ~1 in 4 has a typo or misspelling; shorthand everywhere (w/, "
    "b/c, asap, idk, thx); some are terse fragments ('regex for ipv4 in nginx "
    "logs?'), others ramble with irrelevant backstory or apologize; a few "
    "include partial pasted junk (half a stack trace, a stray URL, '[image]'); "
    "punctuation optional. Roughly 1 in 8 should BLEND a secondary adjacent ask "
    "into the same prompt (e.g. 'triage this ticket and also draft the reply', "
    "'summarize the meeting notes and gimme the sql for that metric we "
    "discussed') while the PRIMARY ask stays this scenario."
)
SMOOTH_STYLE = (
    "Each prompt 15-80 words, first-person, clearly written and well-formed, "
    "as a careful employee would type."
)


def generate_prompts(brief, n, rough=True, model="databricks-claude-haiku-4-5", chunk=30):
    """Generate n distinct workplace prompts for a scenario brief via FMAPI.

    Chunks large requests (the model truncates long JSON arrays) and returns a
    flat list of strings, possibly slightly more/fewer than n if the model
    over/under-delivers a chunk.
    """
    style = ROUGH_STYLE if rough else SMOOTH_STYLE
    out = []
    remaining = n
    while remaining > 0:
        k = min(remaining, chunk)
        out += chat_json(
            [{"role": "user", "content": (
                f"Generate {k} distinct, realistic prompts that different employees "
                f"would send to an internal LLM endpoint. Scenario: {brief} {style} "
                f"Include pasted-content placeholders like <ticket text>, <diff>, "
                f"<notes> where natural. No two alike in structure. "
                f"Return ONLY a JSON array of {k} strings.")}],
            model=model,
            max_tokens=8000,
        )
        remaining -= k
    return out


def main():
    ap = argparse.ArgumentParser(description="Generate synthetic workplace prompts via FMAPI.")
    ap.add_argument("--brief", required=True, help="Scenario description, e.g. 'asking an LLM to review Terraform plans'")
    ap.add_argument("-n", type=int, default=20, help="How many prompts (default 20)")
    ap.add_argument("--smooth", action="store_true", help="Clean, well-formed prompts instead of rough ones")
    ap.add_argument("--model", default="databricks-claude-haiku-4-5")
    ap.add_argument("--json", action="store_true", help="Emit a JSON array instead of one prompt per line")
    args = ap.parse_args()

    prompts = generate_prompts(args.brief, args.n, rough=not args.smooth, model=args.model)
    if args.json:
        print(json.dumps(prompts, indent=1))
    else:
        for p in prompts:
            print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
