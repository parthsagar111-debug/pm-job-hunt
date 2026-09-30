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
"""

CONFIG = {
    # Below this stated maximum CTC the job isn't worth an application. Only applies
    # when the JD states a number; most don't.
    "MIN_CTC_LPA": 15,

    # fit_score needed for Apply, by how close the domain is to the candidate's own.
    "FIT_APPLY_CORE": 65,
    "FIT_APPLY_ADJACENT": 80,
    # Below this, a non_core role isn't worth reading at all.
    "FIT_MAYBE_NON_CORE": 50,

    # Experience-range handling. years_max is the TOP of the range the JD states, so
    # a low ceiling means the role is aimed well below 9+ years.
    "YEARS_MAX_HARD_SKIP": 2,      # <= this: the role is junior, whatever the title says
    "YEARS_MAX_CORE_ONLY": 3,      # == this: core -> Maybe, anything else -> Skip
    "YEARS_MAX_BORDERLINE": (4, 5),  # core -> continue, else -> Maybe

    # Over-senior signals: worth a look, but not an Apply.
    "YEARS_MIN_OVER_SENIOR": 13,
    "MANAGING_PMS_OVER_SENIOR": 4,
}

# Prefixed onto the reason so a row that was never really read is obvious in the sheet.
UNVERIFIED_PREFIX = "UNVERIFIED JD — "

_JUNIOR_LEVELS = {"intern", "apm"}
_UNREADABLE_JD = {"garbage", "thin"}


def decide(features: dict, cfg: dict = CONFIG) -> tuple[str, str]:
    """(decision, reason_prefix) from extracted features. Pure — no I/O, no API.

    Rules are ordered: the first one that matches wins. Order matters, e.g. an
    unreadable JD is checked before is_pm_role, because "not a PM role" derived
    from a category menu is not a finding.
    """
    get = features.get

    # 1. Nothing trustworthy was read — never Skip on an absence of evidence.
    if get("jd_quality") in _UNREADABLE_JD:
        return "Maybe", UNVERIFIED_PREFIX

    # 2. Not a PM role at all (PMM, BA, scrum master, project/program manager...).
    if not get("is_pm_role"):
        return "Skip", ""

    # 3. Something explicitly required that the candidate doesn't have.
    if get("hard_blockers"):
        return "Skip", ""

    # 4. Title itself is junior — a years range alone never lands here (see rule 6).
    if get("title_level") in _JUNIOR_LEVELS:
        return "Skip", ""

    # 5. Stated pay below the floor.
    ctc = get("stated_max_ctc_lpa")
    if ctc is not None and ctc < cfg["MIN_CTC_LPA"]:
        return "Skip", ""

    domain = get("domain_class")
    is_core = domain == "core"

    # 6. Stated experience ceiling. Only fires on numbers the JD actually states —
    # the prompt forbids inferring years from title or seniority wording.
    years_max = get("years_max")
    if years_max is not None:
        if years_max <= cfg["YEARS_MAX_HARD_SKIP"]:
            return "Skip", ""
        if years_max == cfg["YEARS_MAX_CORE_ONLY"]:
            return ("Maybe", "") if is_core else ("Skip", "")
        if years_max in cfg["YEARS_MAX_BORDERLINE"] and not is_core:
            return "Maybe", ""
        # core with a 4-5 ceiling falls through to the fit test below.

    # 7. Over-senior: a VP-level or 13+ years or 4+ PM-managing role is a stretch,
    # not a rejection — unless the domain is unrelated too.
    years_min = get("years_min")
    managing = get("requires_managing_pms")
    over_senior = (
        get("title_level") == "vp_plus"
        or (years_min is not None and years_min >= cfg["YEARS_MIN_OVER_SENIOR"])
        or (managing is not None and managing >= cfg["MANAGING_PMS_OVER_SENIOR"])
    )
    if over_senior:
        return ("Skip", "") if domain == "non_core" else ("Maybe", "")

    # 8. Otherwise it comes down to fit, with the bar set by domain distance.
    fit = get("fit_score") or 0
    if is_core:
        return ("Apply", "") if fit >= cfg["FIT_APPLY_CORE"] else ("Maybe", "")
    if domain == "adjacent":
        return ("Apply", "") if fit >= cfg["FIT_APPLY_ADJACENT"] else ("Maybe", "")
    return ("Maybe", "") if fit >= cfg["FIT_MAYBE_NON_CORE"] else ("Skip", "")
