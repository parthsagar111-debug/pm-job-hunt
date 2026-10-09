"""
decision_rules.py — the Apply/Maybe/Skip decision, as code rather than model judgment.

An audit of 461 classified jobs found 81 (17.6%) in the wrong tab. The model was
being asked to do two jobs at once: read the JD, and apply a policy. It is good at
the first and inconsistent at the second — the same posting landed in Apply on one
run and Maybe on another, because the old prompt carried a distribution quota
(~20/35/45) and two "when in doubt pick Maybe" lines.

So the model now only extracts features (see FEATURES_TOOL_SCHEMA in
core_eval_hosted.py) and this module decides. Every threshold lives in CONFIG,
decide() is pure, and tests/test_decide.py covers each branch — change a number
there and you can see exactly which jobs move.

## Skip needs categorical evidence (2026-10-09)

A second audit, over 1,702 classified jobs (1,191 from a week of scheduled runs plus
a 511-job backfill), found the rules below rejecting good jobs on signals the model
cannot label reliably:

- mandatory_cs_or_engineering_degree appeared in 82 of 265 Skips and was the sole
  cause of 60. It is almost always boilerplate: of 40 live PM JDs sampled on
  2026-10-07, ZERO stated a CS or engineering degree as a hard requirement, and all
  9 that mentioned a degree said "or related field", "Business, Computer Science, or
  related", "or equivalent", or "preferred". The prompt already forbids treating
  "preferred" as a blocker; the model does it anyway, so the code must stop acting on
  it. Casualties included Zeta, Amazon Alexa Ads, MobiKwik, Justdial and a Ferns N
  Petals GPM — all core or adjacent, fit 45-78.
- title_level "apm" was a hard reject. Flexiple's Product Owner was evaluated ten
  times and the model called it apm/fit-45 five times and pm/fit-62 five times — the
  identical role, half Skipped and half not, on a label that flips. A Zepto PM role
  (core, fit 62, no blockers) was Skipped on this alone.

  Softening that alone was wrong in the other direction: the 2026-10-09 backfill put
  29 genuine associate postings ("Associate Product Manager @ BigBasket", "APM -
  toolkits") into Maybe. The model's label conflated two things. The published TITLE
  now settles it — title_is_junior, computed in code, rejects an associate posting,
  while the model's "apm" on an ordinary PM title only caps the outcome at Maybe.
  Of the 41 jobs it called apm that day, 29 had a junior title and 12 did not.
- The non_core fit floor was 50, which sits between two adjacent steps of the model's
  own fit vocabulary (… 35, 42, 45, 48, 50, 62 …). The same role has been seen to
  swing 37 points between runs, so a 2-point gap cannot be a tab boundary.

The principle the rules now follow: **Skip only on evidence that cannot be a judgment
call.** A domain the candidate categorically lacks, an internship, a stated pay floor,
a stated junior experience ceiling. Everything softer caps at Maybe and says why in
the reason, because a Skip is effectively permanent — dedup is URL-based, so a row
written to Skip is never re-judged.
"""

CONFIG = {
    # Below this stated maximum CTC the job isn't worth an application. Only applies
    # when the JD states a number; most don't.
    "MIN_CTC_LPA": 15,

    # fit_score needed for Apply, by how close the domain is to the candidate's own.
    "FIT_APPLY_CORE": 65,
    "FIT_APPLY_ADJACENT": 80,
    # Below this, a non_core role isn't worth reading at all. Deliberately well under
    # the Apply bars: it only has to separate "unrelated" from "worth a glance", and
    # it must sit below the model's run-to-run fit wobble, not inside it.
    "FIT_MAYBE_NON_CORE": 35,

    # Experience-range handling. years_max is the TOP of the range the JD states, so
    # a low ceiling means the role is aimed well below 9+ years.
    "YEARS_MAX_HARD_SKIP": 2,      # <= this: the role is junior, whatever the title says
    "YEARS_MAX_CORE_ONLY": 3,      # == this: core -> Maybe, anything else -> Skip
    "YEARS_MAX_BORDERLINE": (4, 5),  # core -> continue, else -> Maybe

    # Over-senior signals: worth a look, but not an Apply.
    "YEARS_MIN_OVER_SENIOR": 13,
    "MANAGING_PMS_OVER_SENIOR": 4,
}

# Blockers worth rejecting on: a domain whose depth isn't learnable in a notice
# period, and which no amount of PM craft substitutes for. Mislabelling one of these
# requires the model to misread the whole JD, not just one line of it.
CATEGORICAL_BLOCKERS = {
    "deep_infra_security_networking",
    "clinical_or_payer_healthcare_ops",
    "post_trade_aml_compliance_accounting",
    "manufacturing_erp_industrial",
    "specialist_hardware",
}

# Blockers that only ever add a note. Each of these hangs on one line of a JD, and
# that line is usually a preference written as though it were a rule — see the module
# docstring for the measurements. "pure_supply_chain_logistics_saas" is here because
# the prompt itself says a supply-chain role inside a consumer e-commerce company is
# adjacent, not blocked, and the rule kept contradicting that.
ADVISORY_BLOCKERS = {
    "mandatory_cs_or_engineering_degree",
    "other_mandatory_degree",
    "mandatory_platform_certification",
    "pure_supply_chain_logistics_saas",
}

# Prefixed onto the reason so a row that was never really read is obvious in the sheet.
UNVERIFIED_PREFIX = "UNVERIFIED JD — "
# The JD states a qualification the candidate may not hold. Not a rejection: verify it.
CHECK_REQS_PREFIX = "CHECK REQS — "
# Read as an APM/associate posting. Kept out of Apply, kept out of the bin.
JUNIOR_TITLE_PREFIX = "JUNIOR TITLE — "

_UNREADABLE_JD = {"garbage", "thin"}


def decide(features: dict, cfg: dict = CONFIG) -> tuple[str, str]:
    """(decision, reason_prefix) from extracted features. Pure — no I/O, no API.

    Rules are ordered: the first one that matches wins. Order matters, e.g. an
    unreadable JD is checked before is_pm_role, because "not a PM role" derived
    from a category menu is not a finding.
    """
    # 0. The published title says associate/junior/intern. Checked before anything
    #    else, including the JD: it is computed in code from the scraped title, so it
    #    holds even when the JD never loaded. IIMJobs and Hirist JDs fail often enough
    #    that leaving this until after rule 1 would park associate postings in Maybe.
    if features.get("title_is_junior"):
        return "Skip", ""

    # 1. Nothing trustworthy was read — never Skip on an absence of evidence, and
    #    don't trust this reply's blockers or title reading either.
    if features.get("jd_quality") in _UNREADABLE_JD:
        return "Maybe", UNVERIFIED_PREFIX

    decision, notes = _classify(features, cfg)

    if decision != "Skip":
        # An APM posting is not worth an application, but the label is too unreliable
        # to reject on — so it caps the outcome instead of deciding it.
        if features.get("title_level") == "apm":
            if decision == "Apply":
                decision = "Maybe"
            notes.append(JUNIOR_TITLE_PREFIX)
        if set(features.get("hard_blockers") or []) & ADVISORY_BLOCKERS:
            notes.append(CHECK_REQS_PREFIX)

    return decision, "".join(notes)


def _classify(features: dict, cfg: dict) -> tuple[str, list]:
    """The ordered rules. Returns (decision, notes) — decide() applies the caps."""
    get = features.get
    notes = []
    blockers = set(get("hard_blockers") or [])
    domain = get("domain_class")
    is_core = domain == "core"
    fit = get("fit_score") or 0

    # 2. Not a PM role at all (PMM, BA, scrum master, project/program manager...).
    #    A high fit score beside this is not a reason to doubt it: fit_score is scored
    #    "as if the blockers did not exist" and says nothing about whether the job is
    #    product management. Overriding it on a strong domain fit put "Growth Manager
    #    @ FRND" (core, fit 72) into Maybe, and both cases it ever fired on were
    #    correct rejections.
    if not get("is_pm_role"):
        return "Skip", notes

    # 3. A domain the candidate categorically lacks. Advisory blockers are handled in
    #    decide() as a note — they are usually a preference phrased like a rule.
    if blockers & CATEGORICAL_BLOCKERS:
        return "Skip", notes

    # 4. The model read it as an internship. The published title is handled in
    #    decide() as rule 0, before the JD check.
    if get("title_level") == "intern":
        return "Skip", notes

    # 5. Stated pay below the floor.
    ctc = get("stated_max_ctc_lpa")
    if ctc is not None and ctc < cfg["MIN_CTC_LPA"]:
        return "Skip", notes

    # 6. Stated experience ceiling. Only fires on numbers the JD actually states —
    #    the prompt forbids inferring years from title or seniority wording.
    years_max = get("years_max")
    if years_max is not None:
        if years_max <= cfg["YEARS_MAX_HARD_SKIP"]:
            return "Skip", notes
        if years_max == cfg["YEARS_MAX_CORE_ONLY"]:
            return ("Maybe" if is_core else "Skip"), notes
        if years_max in cfg["YEARS_MAX_BORDERLINE"] and not is_core:
            return "Maybe", notes
        # core with a 4-5 ceiling falls through to the fit test below.

    # 7. Over-senior: a VP-level or 13+ years or 4+ PM-managing role is a stretch,
    #    not a rejection — unless the domain is unrelated too.
    years_min = get("years_min")
    managing = get("requires_managing_pms")
    over_senior = (
        get("title_level") == "vp_plus"
        or (years_min is not None and years_min >= cfg["YEARS_MIN_OVER_SENIOR"])
        or (managing is not None and managing >= cfg["MANAGING_PMS_OVER_SENIOR"])
    )
    if over_senior:
        return ("Skip" if domain == "non_core" else "Maybe"), notes

    # 8. Otherwise it comes down to fit, with the bar set by domain distance.
    if is_core:
        return ("Apply" if fit >= cfg["FIT_APPLY_CORE"] else "Maybe"), notes
    if domain == "adjacent":
        return ("Apply" if fit >= cfg["FIT_APPLY_ADJACENT"] else "Maybe"), notes
    return ("Maybe" if fit >= cfg["FIT_MAYBE_NON_CORE"] else "Skip"), notes
