"""
eval_regression.py — does the new classifier agree with the expected tab?

Reads named rows from the existing Apply/Maybe/Skip tabs, re-runs the NEW feature
extraction + decide() on their STORED JD (no scraping), and prints old vs new.

WRITES NOTHING. Reads the Sheet, calls the Claude API (~1 call per row).

    python scripts/eval_regression.py            # the built-in expectation list
    python scripts/eval_regression.py --all      # every row on the three tabs

Needs ANTHROPIC_API_KEY, GOOGLE_SERVICE_ACCOUNT_JSON, PM_EVAL_SPREADSHEET_ID and
CANDIDATE_PROFILE.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from candidate_profile import load_candidate_profile
from core_eval_hosted import extract_features
from decision_rules import decide
from sheets_writer import TAB_APPLY, TAB_MAYBE, TAB_SKIP, _get_client, _with_retry

SPREADSHEET_ID = os.environ.get("PM_EVAL_SPREADSHEET_ID", "")

# (expected tab, title fragment, company fragment) — matched case-insensitively.
EXPECTATIONS = [
    ("Apply", "Senior Product Manager", "Snapmint"),
    ("Apply", "Associate Director of Product Management", "Magicbricks"),
    ("Apply", "Senior Product Manager", "PhysicsWallah"),
    ("Apply", "Product Manager", "Dentalkart"),
    ("Apply", "Senior Technical Product Manager", "Urbanfix"),
    ("Apply", "Brand Platform", "Purplle"),
    ("Apply", "Senior Product Manager", "Expedia"),
    ("Apply", "Personalisation", "Nykaa"),
    ("Apply", "Senior Product Manager", "PhonePe"),

    ("Maybe", "Platform & AI", "Wrike"),
    ("Maybe", "MCP/API Platform", "Payoneer"),
    ("Maybe", "Product Manager", "Policybazaar"),
    ("Maybe", "Senior Product Manager", "Cornerstone"),
    ("Maybe", "Lead Product Manager", "ADP"),
    ("Maybe", "Trading & Derivative", "INDmoney"),
    ("Maybe", "Supply Chain & Operations", "Nutrabay"),

    ("Skip", "Senior Product Manager", "Cohesity"),
    ("Skip", "ZTNA", "SonicWall"),
    ("Skip", "Clinical Data", "athenahealth"),
    ("Skip", "Fintech Domain", "Zeta"),
    ("Skip", "Growth", "Solar Square"),
    ("Skip", "Over-the-Road Visibility", "FourKites"),
    ("Skip", "Product Owner", "NielsenIQ"),
    ("Skip", "AML", "Visa"),
]


def _read_rows() -> list[dict]:
    client = _get_client()
    sh = _with_retry(client.open_by_key, SPREADSHEET_ID)
    rows = []
    for tab in (TAB_APPLY, TAB_MAYBE, TAB_SKIP):
        try:
            ws = sh.worksheet(tab)
        except Exception as e:
            print(f"  [sheets] {tab}: {e}")
            continue
        values = _with_retry(ws.get_all_values)
        if not values:
            continue
        headers = [h.strip() for h in values[0]]
        for raw in values[1:]:
            padded = raw + [""] * (len(headers) - len(raw))
            row = dict(zip(headers, padded))
            row["_tab"] = tab
            rows.append(row)
    return rows


def _matches(row: dict, title_fragment: str, company_fragment: str) -> bool:
    return (title_fragment.lower() in (row.get("Title", "") or "").lower()
            and company_fragment.lower() in (row.get("Company", "") or "").lower())


def _evaluate(row: dict) -> tuple[str, dict, str]:
    job = {
        "title": row.get("Title", ""), "company": row.get("Company", ""),
        "location": row.get("Location", ""), "source": row.get("Source", ""),
        "url": row.get("URL", ""), "stated_experience": "",
    }
    features, _jd = extract_features(job, jd_text=row.get("JD", ""))
    decision, prefix = decide(features)
    return decision, features, prefix


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true",
                        help="evaluate every row on the three tabs, not just the expectation list")
    args = parser.parse_args()

    if not SPREADSHEET_ID:
        sys.exit("PM_EVAL_SPREADSHEET_ID is not set.")
    load_candidate_profile()   # fail before spending anything

    rows = _read_rows()
    print(f"Read {len(rows)} row(s) from the Sheet.\n")

    targets = []
    if args.all:
        targets = [(row["_tab"], row) for row in rows]
    else:
        for expected_tab, title_fragment, company_fragment in EXPECTATIONS:
            found = [r for r in rows if _matches(r, title_fragment, company_fragment)]
            if not found:
                print(f"  NOT FOUND  {company_fragment} — {title_fragment}")
                continue
            # More than one row can legitimately match (a repost, or two similar
            # titles at one company) — evaluate and report every one, per Parth.
            if len(found) > 1:
                print(f"  {len(found)} rows match {company_fragment} — {title_fragment}; "
                      f"evaluating each")
            for row in found:
                targets.append((expected_tab, row))

    agreed = checked = 0
    misses = []
    for expected_tab, row in targets:
        title = (row.get("Title") or "")[:46]
        company = (row.get("Company") or "")[:22]
        jd = row.get("JD") or ""
        try:
            new_decision, features, prefix = _evaluate(row)
        except Exception as e:
            print(f"  ERROR  {company:<22} {title:<46} {type(e).__name__}: {e}")
            continue

        checked += 1
        old_decision = row["_tab"]
        ok = new_decision == expected_tab
        agreed += ok
        flag = "ok  " if ok else "MISS"
        print(f"  {flag}  expected {expected_tab:<5} old {old_decision:<5} new {new_decision:<5} "
              f"| {company:<22} {title:<46} | jd={len(jd)}c | {prefix}{features.get('reason','')}")
        if not ok:
            misses.append((expected_tab, new_decision, row, features))

    if checked:
        print(f"\nMatch rate: {agreed}/{checked} ({100 * agreed // checked}%)")

    if misses:
        print("\nMisses — features that produced them:")
        for expected_tab, new_decision, row, features in misses:
            print(f"\n  {row.get('Company','')} — {row.get('Title','')}")
            print(f"    expected {expected_tab}, got {new_decision}")
            for key in ("is_pm_role", "title_level", "years_min", "years_max", "domain_class",
                        "hard_blockers", "requires_managing_pms", "stated_max_ctc_lpa",
                        "jd_quality", "fit_score", "gap"):
                print(f"      {key}: {features.get(key)!r}")
        print("\n  For each: if the FEATURES are wrong, the prompt needs work; if the features "
              "are right and the tab isn't, a CONFIG threshold in decision_rules.py does.")

    print("\nNothing was written to the Sheet.")


if __name__ == "__main__":
    main()
