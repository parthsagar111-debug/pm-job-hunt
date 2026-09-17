"""JD-derived rules: the mandatory-Arabic override and the visa excerpt builder.

Both decide outcomes without asking Claude, so their false positives are
expensive: the first version of the Arabic check wrongly moved Apparel Group
and Tabby out of Apply. Those exact JD extracts are pinned here.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import global_visa_hosted as gv
import gulf_eval_hosted as gulf


MANDATORY = [
    ("Sanabil Studio",
     "THE PROFILE\n8+ years building consumer products at scale.\n"
     "You are fluent in both Arabic and English.\nBonus:\nFintech background"),
    ("Stryker",
     "What You Will Need\nBachelor's degree in business or a related field.\n"
     "Fluency in English and Arabic, both written and spoken.\nPreferred\nMBA"),
    ("Kinetic Riyadh",
     "Requirements:\nNative Arabic speaker is a must\nExcellent organizational skills."),
    ("Kinetic Dubai",
     "To be successful you will need to meet the following:\n"
     "Excellent verbal and written communication skills in English & Arabic."),
    ("Malaa",
     "Required:\n6+ years of product management experience in fintech\n"
     "Professional written and spoken Arabic and English.\nPreferred:\nSQL"),
    ("ideal-candidate heading", "The Ideal Candidate:\nFluent in Arabic and English"),
    ("explicit on the line", "Arabic fluency is mandatory for this client-facing role"),
]

NOT_MANDATORY = [
    ("SiFi — no skill word",
     "Requirements\nSaudi or MENA market experience, and Arabic. You've felt finance-ops pain."),
    ("Prospex — Arabic-first product, not a language requirement",
     "Our Client is an Arabic-first commerce platform for merchants in Saudi Arabia\n"
     "Comfort with Arabic-first merchant experiences"),
    ("a plus", "Requirements\n7+ years of product management\nArabic language skills are a plus"),
    ("nice-to-have heading",
     "Nice to have:\nPrior experience in the GCC building payments products at scale\nFluency in Arabic"),
    ("preferred heading", "Preferred Qualifications\nFluency in Arabic and English"),
    ("preferred on the line", "Requirements\nFluent English; Arabic preferred"),
    ("Apparel Group — a duty, not a requirement",
     "About the Role\nWe are looking for a Product Manager – E-Commerce.\nKey Responsibilities\n"
     "• Manage a multi-currency, multi-language storefront (including Arabic/RTL) across the GCC"),
    ("Tabby — under Bonus points, split as B / onus points",
     "Fluency in unit economics and analytics\nFluent English\nB\nonus points\n"
     "Credit card domain knowledge\nGCC market experience; Arabic language skills"),
    ("Imploy — NLP skill under Recommended Skills",
     "Recommended Skills\nExperience in AI product development.\n"
     "Experience with Arabic Language Processing."),
    ("responsibility, not requirement",
     "Responsibilities\nCommunicate with Arabic-speaking stakeholders across the region"),
    ("no mention at all", "Requirements\n5+ years of product management\nStrong SQL"),
    ("empty", ""),
]


@pytest.mark.parametrize("label,jd", MANDATORY, ids=[c[0] for c in MANDATORY])
def test_arabic_required_is_detected(label, jd):
    assert gulf.mandatory_arabic_line(jd) != ""


@pytest.mark.parametrize("label,jd", NOT_MANDATORY, ids=[c[0] for c in NOT_MANDATORY])
def test_arabic_not_required_is_left_alone(label, jd):
    assert gulf.mandatory_arabic_line(jd) == ""


def test_override_only_touches_non_skip_decisions():
    jd = MANDATORY[1][1]
    jobs = [
        {"title": "a", "company": "A", "evaluation": {"decision": "Maybe", "reason": "x", "gap": "y", "jd": jd}},
        {"title": "b", "company": "B", "evaluation": {"decision": "Skip", "reason": "original", "gap": "y", "jd": jd}},
        {"title": "c", "company": "C", "evaluation": {"decision": "Apply", "reason": "x", "gap": "y",
                                                      "jd": NOT_MANDATORY[0][1]}},
    ]
    assert gulf.apply_arabic_override(jobs) == 1
    assert jobs[0]["evaluation"]["decision"] == "Skip"
    assert "Arabic required" in jobs[0]["evaluation"]["reason"]
    assert jobs[1]["evaluation"]["reason"] == "original"   # untouched
    assert jobs[2]["evaluation"]["decision"] == "Apply"    # no evidence, left alone


# ── visa excerpt: what actually reaches Claude
def test_excerpt_keeps_the_visa_line_with_context():
    jd = ("About the role\nWe are hiring a Senior PM.\nYou will own the roadmap.\n"
          "Benefits\nA relocation package with visa support for those who need it.\n"
          "Free lunch\nAbout us\nWe build things.")
    excerpt = gv.visa_excerpt(jd)
    assert "visa support" in excerpt
    assert len(excerpt) < len(jd)


def test_excerpt_is_empty_when_visa_is_never_mentioned():
    assert gv.visa_excerpt("We offer relocation assistance and free lunch.") == ""


def test_relocation_alone_never_reaches_claude():
    """Agreed rule: relocation support without a visa mention doesn't qualify, so
    it must not even be sent for classification."""
    assert gv._VISA_TERMS.search("Generous relocation assistance provided") is None


def test_excerpt_caps_length():
    jd = "\n".join(["visa sponsorship available"] * 5000)
    assert len(gv.visa_excerpt(jd)) <= gv.EXCERPT_MAX_CHARS
