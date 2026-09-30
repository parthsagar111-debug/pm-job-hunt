"""
candidate_profile.py — loads the candidate profile the classifier scores against.

THIS FILE CONTAINS NO PERSONAL DATA, AND MUST NOT.

This repository is public. The profile — career history, metrics, and the explicit
list of what the candidate does and doesn't have — lives in the CANDIDATE_PROFILE
GitHub secret, rendered from job-automation's resume_facts.json by
build_candidate_profile.py in that (private) repo. resume_facts.json stays the
single source of truth; this module only reads the rendered result.

Re-run that helper and update the secret whenever the resume changes.

Never print, log, or echo the value. Per-job feature JSON is fine to log; the
profile itself is not.
"""

import os

ENV_VAR = "CANDIDATE_PROFILE"

# A real profile is thousands of characters. Anything this short means the secret
# was truncated or holds a placeholder — better to fail than to classify ~1,500
# jobs a day against a profile that silently says almost nothing.
MIN_PROFILE_CHARS = 200

_cached: str | None = None


class CandidateProfileMissing(RuntimeError):
    """Raised when the secret is absent or obviously not a real profile."""


def load_candidate_profile() -> str:
    """The profile text, cached per process.

    Raises CandidateProfileMissing rather than falling back to a default: a
    silently empty profile would let the model invent gaps from nothing, which is
    the exact failure this whole change exists to fix.
    """
    global _cached
    if _cached is not None:
        return _cached

    value = (os.environ.get(ENV_VAR) or "").strip()
    if not value:
        raise CandidateProfileMissing(
            f"{ENV_VAR} is not set. It holds the candidate profile the classifier "
            f"scores against, rendered from resume_facts.json by job-automation's "
            f"build_candidate_profile.py. Add it as a GitHub Actions secret (and to "
            f"your local environment for local runs)."
        )
    if len(value) < MIN_PROFILE_CHARS:
        raise CandidateProfileMissing(
            f"{ENV_VAR} is only {len(value)} characters — expected at least "
            f"{MIN_PROFILE_CHARS}. It looks truncated or like a placeholder; refusing "
            f"to classify against it."
        )

    _cached = value
    return _cached


def reset_cache() -> None:
    """Test hook only — production reads the secret once per process."""
    global _cached
    _cached = None
