"""Sponsor-register matching and the (dormant) Adzuna source.

The matcher is the risky part: registry entries are legal names ("ROOFOODS LTD
T/A DELIVEROO") while LinkedIn shows brands, so a loose rule invents matches.
Cases below come from the live registers on 2026-09-18.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import adzuna_source
import sponsor_registry as sr


# ── name canonicalisation
@pytest.mark.parametrize("raw,expected", [
    ("Revolut Ltd", "revolut"),
    ("Monzo Bank Ltd", "monzo bank"),
    ("Booking.com B.V.", "booking com"),
    ("Adyen N.V.", "adyen"),
    ("ROOFOODS LTD T/A DELIVEROO", "roofoods deliveroo"),
    ('""AAE"" Advanced Automated Equipment B.V.', "aae advanced automated equipment"),
])
def test_canonical_strips_legal_forms_not_names(raw, expected):
    assert sr._canonical(raw) == expected


def test_canonical_keeps_group_and_holdings():
    """Stripping these made a company called "Starling" match "STARLING GROUP LTD"."""
    assert sr._canonical("Chalhoub Group") == "chalhoub group"
    assert sr._canonical("Starling Group Ltd") == "starling group"


# ── matching
@pytest.fixture
def registry():
    reg = sr.SponsorRegistry()
    reg._uk = sr._index(["Revolut Ltd", "Monzo Bank Ltd", "ROOFOODS LTD T/A DELIVEROO",
                         "STARLING GROUP LTD", "Starling Equine Vets Ltd",
                         "Octopus Energy Limited"])
    reg._nl = sr._index(["Booking.com B.V.", "Adyen N.V."])
    return reg


@pytest.mark.parametrize("company,expected_fragment", [
    ("Revolut", "Revolut Ltd"),
    ("Monzo Bank", "Monzo Bank Ltd"),
    ("Deliveroo", "DELIVEROO"),          # only reachable through the T/A name
    ("Octopus Energy", "Octopus Energy"),
])
def test_uk_matches(registry, company, expected_fragment):
    assert expected_fragment.lower() in registry.lookup(company, "London, United Kingdom").lower()


def test_ambiguous_name_matches_nothing(registry):
    """Two registry entries start with "Starling" — a guess here would be a false claim."""
    assert registry.lookup("Starling", "London, United Kingdom") == ""


def test_unknown_company_matches_nothing(registry):
    assert registry.lookup("Definitely Not A Real Company Xyz", "London, United Kingdom") == ""


def test_netherlands_uses_the_ind_register(registry):
    assert "Booking.com" in registry.lookup("Booking.com", "Amsterdam, Netherlands")
    assert "Adyen" in registry.lookup("Adyen", "Amsterdam, North Holland, Netherlands")


def test_countries_without_a_register_return_blank(registry):
    """No public register was verified outside the UK and NL — so no claim is made."""
    for location in ["Seattle, WA", "Berlin, Germany", "Toronto, Ontario, Canada",
                     "Paris, France", "Warsaw, Poland", "Stockholm, Sweden"]:
        assert registry.lookup("Revolut", location) == ""


def test_uk_register_only_keeps_the_skilled_worker_route():
    """Global Business Mobility is an intra-company transfer — useless to an applicant."""
    assert sr.UK_ROUTES_KEPT == {"skilled worker"}


def test_registry_failure_is_recorded_not_raised(monkeypatch):
    reg = sr.SponsorRegistry()
    monkeypatch.setattr(reg, "_get", lambda url, timeout=60: (_ for _ in ()).throw(RuntimeError("gov.uk down")))
    assert reg.lookup("Revolut", "London, United Kingdom") == ""
    assert reg.errors and "UK register unavailable" in reg.errors[0]


# ── Adzuna: dormant without credentials
def test_dormant_without_credentials(monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    assert adzuna_source.is_configured() is False
    assert adzuna_source.fetch("United Kingdom") == []


def test_configured_only_when_both_secrets_present(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    assert adzuna_source.is_configured() is False
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    assert adzuna_source.is_configured() is True


def test_unhosted_country_is_skipped(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    assert "Cyprus" not in adzuna_source.COUNTRY_CODES
    assert adzuna_source.fetch("Cyprus") == []


def test_response_maps_into_the_shared_job_shape(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")

    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"results": [{
                "id": 5354823947,
                "title": "Senior <strong>Product Manager</strong>",
                "company": {"display_name": "ACME Ltd"},
                "location": {"display_name": "London, UK"},
                "created": "2026-09-18T09:00:00Z",
                "redirect_url": "https://www.adzuna.co.uk/land/ad/5354823947",
                "description": "We are hiring a product manager...",
            }]}

    monkeypatch.setattr(adzuna_source.requests, "get", lambda *a, **k: R())
    jobs = adzuna_source.fetch("United Kingdom")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_id"] == "az_5354823947"
    assert job["title"] == "Senior Product Manager"      # markup stripped
    assert job["source"] == "Adzuna"
    assert job["jd_is_snippet"] is True                  # truncated, not a full JD


def test_api_failure_returns_empty_not_raise(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    def boom(*a, **k):
        raise ConnectionError("adzuna down")
    monkeypatch.setattr(adzuna_source.requests, "get", boom)
    assert adzuna_source.fetch("United Kingdom") == []


# ── country aliases: an unmapped spelling silently means "no licence check"
@pytest.mark.parametrize("location,expected", [
    ("London, UK", "united kingdom"),                 # Adzuna's spelling
    ("Cambridge, England, United Kingdom", "united kingdom"),
    ("Edinburgh, Scotland", "united kingdom"),
    ("Amsterdam, Noord-Holland", "netherlands"),
    ("Rotterdam, Holland", "netherlands"),
    ("Berlin, Deutschland", "germany"),
    ("Madrid, Espana", "spain"),
    ("Seattle, WA", "united states"),
])
def test_country_aliases(location, expected):
    from core_eval_hosted import location_country
    assert location_country(location) == expected


def test_adzuna_location_carries_the_country(monkeypatch):
    """Without this the UK register never fires on Adzuna rows: "London, UK" alone
    resolves to a country token the registry doesn't key on."""
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")

    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"results": [
                {"id": 1, "title": "Product Manager", "company": {"display_name": "ACME"},
                 "location": {"display_name": "Manchester, Greater Manchester"},
                 "created": "2026-09-18T09:00:00Z", "redirect_url": "https://www.adzuna.co.uk/land/ad/1",
                 "description": "..."},
                {"id": 2, "title": "Product Manager", "company": {"display_name": "ACME"},
                 "location": {"display_name": "London, United Kingdom"},
                 "created": "2026-09-18T09:00:00Z", "redirect_url": "https://www.adzuna.co.uk/land/ad/2",
                 "description": "..."},
            ]}

    monkeypatch.setattr(adzuna_source.requests, "get", lambda *a, **k: R())
    jobs = adzuna_source.fetch("United Kingdom")
    from core_eval_hosted import location_country
    assert all(location_country(j["location"]) == "united kingdom" for j in jobs)
    assert jobs[1]["location"] == "London, United Kingdom"   # not duplicated
