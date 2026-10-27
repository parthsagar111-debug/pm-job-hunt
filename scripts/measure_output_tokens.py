"""Where do the 286 output tokens per job actually go?

Output is 49.8% of inference spend, measured on a 515-job run, and only ~35 of those
tokens are accounted for by the schema's key names. This ablates the schema to find
the rest, so the next optimisation targets the big part rather than the visible part.

Makes real API calls — about 12, a few cents. Prints token counts and key names only,
never prompt text and never the reason/gap prose, because this repo's logs are public.
"""

import copy
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core_eval_hosted as core

REPEATS = 3

# ~4,500 characters, the measured average JD length.
JD = ("Responsibilities: Own the roadmap for checkout and payments across web and app. "
      "Partner with engineering, design and analytics to ship experiments that move "
      "conversion. Define PRDs, run A/B tests and own funnel metrics end to end. "
      "Requirements: 6-9 years in product management at a consumer internet company. "
      "Strong SQL and experimentation background. Experience with UPI, cards and "
      "wallets preferred. Bachelor's degree in engineering, business or a related "
      "field. ") * 7

JOB_LINE = ("Title: Senior Product Manager, Payments\nCompany: ExampleCo\n"
            "Location: Bengaluru\nSource: LinkedIn\n\nJD:\n")

SHORT_KEYS = {
    "is_pm_role": "p", "title_level": "l", "years_min": "y0", "years_max": "y1",
    "domain_class": "d", "hard_blockers": "b", "requires_managing_pms": "m",
    "jd_quality": "q", "fit_score": "f", "reason": "r", "gap": "g",
}


def rename(schema: dict, mapping: dict) -> dict:
    out = copy.deepcopy(schema)
    out["properties"] = {mapping.get(k, k): v for k, v in out["properties"].items()}
    out["required"] = [mapping.get(k, k) for k in out["required"]]
    return out


def drop(schema: dict, fields: set) -> dict:
    out = copy.deepcopy(schema)
    out["properties"] = {k: v for k, v in out["properties"].items() if k not in fields}
    out["required"] = [k for k in out["required"] if k not in fields]
    return out


def run(label: str, schema: dict, prefix: str) -> dict:
    outs, payload_chars, keys = [], [], None
    for _ in range(REPEATS):
        data, usage = core.claude_structured(
            JOB_LINE + JD, "extract_job_features",
            "Extract the features needed to classify this job.", schema,
            max_tokens=400, temperature=0, cached_prefix=prefix)
        outs.append(usage.get("output_tokens", 0))
        payload_chars.append(len(json.dumps(data, separators=(",", ":"))))
        keys = sorted(data)
    return {"label": label, "out": statistics.mean(outs), "raw": outs,
            "chars": statistics.mean(payload_chars), "keys": keys}


def main() -> None:
    schema = core.FEATURES_TOOL_SCHEMA
    prefix = core.build_feature_prompt()        # never printed

    variants = [
        ("1. production schema, as shipped", schema),
        ("2. short keys (p/l/d/b/f...)", rename(schema, SHORT_KEYS)),
        ("3. no reason, no gap", drop(schema, {"reason", "gap"})),
        ("4. short keys AND no prose", rename(drop(schema, {"reason", "gap"}), SHORT_KEYS)),
        ("5. decision-critical fields only", drop(schema, {
            "reason", "gap", "requires_managing_pms", "years_min", "years_max"})),
    ]

    print(f"prefix: {len(prefix):,} chars (cached, billed at 0.1x — not the subject here)")
    print(f"JD    : {len(JD):,} chars, the measured average\n")

    results = [run(label, s, prefix) for label, s in variants]
    base = results[0]["out"]

    print(f"{'variant':<36} {'output tok':>10} {'json chars':>11} {'vs base':>9}")
    print("-" * 70)
    for r in results:
        delta = "" if r is results[0] else f"{100*(r['out']-base)/base:+.1f}%"
        print(f"{r['label']:<36} {r['out']:>10.1f} {r['chars']:>11.0f} {delta:>9}   {r['raw']}")

    print()
    print(f"keys the model emitted (production): {results[0]['keys']}")
    print()
    # Output is 49.8% of total spend; convert each saving to a monthly figure on $15.
    for r in results[1:]:
        saved = (base - r["out"]) / base
        print(f"{r['label']:<36} -> {100*0.498*saved:5.1f}% of spend, "
              f"${15*0.498*saved:5.2f}/month")


if __name__ == "__main__":
    main()
