"""Sponsor-register matching and country resolution.

The matcher is the risky part: registry entries are legal names ("ROOFOODS LTD
T/A DELIVEROO") while LinkedIn shows brands, so a loose rule invents matches and
a strict one misses. Cases below come from the live registers on 2026-09-18.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sponsor_registry as sr
from core_eval_hosted import location_country


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
    """gov.uk being down must cost a blank column, never the run."""
    reg = sr.SponsorRegistry()
    monkeypatch.setattr(reg, "_get",
                        lambda url, timeout=60: (_ for _ in ()).throw(RuntimeError("gov.uk down")))
    assert reg.lookup("Revolut", "London, United Kingdom") == ""
    assert reg.errors and "UK register unavailable" in reg.errors[0]


def test_registers_are_fetched_once_per_run(monkeypatch):
    calls = {"n": 0}

    def counted(url, timeout=60):
        calls["n"] += 1
        raise RuntimeError("offline")

    reg = sr.SponsorRegistry()
    monkeypatch.setattr(reg, "_get", counted)
    for _ in range(5):
        reg.lookup("Revolut", "London, United Kingdom")
    assert calls["n"] == 1     # the failed load is cached too, not retried per job


# ── country resolution: an unmapped spelling silently means "no licence check"
@pytest.mark.parametrize("location,expected", [
    ("Cambridge, England, United Kingdom", "united kingdom"),
    ("Edinburgh, Scotland", "united kingdom"),
    ("London, UK", "united kingdom"),
    ("Amsterdam, Noord-Holland", "netherlands"),
    ("Rotterdam, Holland", "netherlands"),
    ("Berlin, Deutschland", "germany"),
    ("Madrid, Espana", "spain"),
    ("Seattle, WA", "united states"),
    ("Paris, France", "france"),
])
def test_country_aliases(location, expected):
    assert location_country(location) == expected
