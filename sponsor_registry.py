"""
sponsor_registry.py — "is this employer licensed to sponsor a work visa?"

The global feed can only see sponsorship when a JD says so in writing, which is
rare: 1,503 roles on 2026-09-18 produced 253 JDs that mentioned a visa term and
zero that offered one. Two governments publish the employer side of that fact,
so a company can be checked even when its listing is silent:

  UK  — Register of Licensed Sponsors (Worker and Temporary Worker).
        Free CSV, refreshed daily. 143,168 organisations on 2026-09-18, of which
        123,144 hold the Skilled Worker route.
  NL  — IND Public Register of Recognised Sponsors (regular labour / highly
        skilled migrants). 12,984 organisations, HTML table only, no CSV.

This is a SEPARATE SIGNAL, never a decision: a licence means the employer *can*
sponsor, not that they will for this role. Parth's call — it goes in its own
Sheet column to filter on, and never promotes a job into the Listings tab.

Matching is the weak point and is deliberately conservative. Registry names are
legal names ("ROOFOODS LTD T/A DELIVEROO" for Deliveroo), while LinkedIn shows
brands, so exact matching misses and loose matching invents: "Starling" alone
also matches an equine vet and a film company. See _match() for the rules.
"""

import csv
import io
import re
from typing import Iterable

from core_eval_hosted import _norm, location_country

UK_PUBLICATION = "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"
NL_REGISTER    = ("https://ind.nl/en/public-register-recognised-sponsors/"
                  "public-register-regular-labour-and-highly-skilled-migrants")

# Routes worth flagging. Skilled Worker is the one a PM would actually be hired on;
# Global Business Mobility is an intra-company transfer, useless to an outside applicant.
UK_ROUTES_KEPT = {"skilled worker"}

# Legal forms only. "Group", "Holdings", "International" and country words are part of
# real company names (Chalhoub Group, Apparel Group), and stripping them made a LinkedIn
# employer called "Starling" match the registry's "STARLING GROUP LTD".
_SUFFIXES = re.compile(
    r"\b(ltd|limited|plc|llp|llc|inc|incorporated|corp|corporation|gmbh|bv|b v|nv|n v|"
    r"sarl|srl|aps|spa|sas|pte|pty)\b", re.I)

# Words too generic to identify a company on their own.
_GENERIC = {"the", "and", "of", "for", "group", "services", "solutions", "technologies",
            "technology", "consulting", "digital", "systems", "software", "partners",
            "capital", "ventures", "labs", "media", "health", "energy", "bank", "finance",
            "financial", "products", "people", "work", "jobs", "care", "group"}


def _canonical(name: str) -> str:
    """Registry and LinkedIn names, reduced to something comparable."""
    text = _norm(name.replace('"', " "))           # the NL register wraps some names in ""
    # _norm has already turned the slash into a space, so "T/A" arrives here as "t a".
    text = re.sub(r"\bt a\b", " ", text)           # "ROOFOODS LTD T/A DELIVEROO"
    text = _SUFFIXES.sub(" ", text)
    return " ".join(text.split())


def _index(names: Iterable[str]) -> dict[str, str]:
    """canonical name -> the original registry spelling (first one wins)."""
    out = {}
    for raw in names:
        key = _canonical(raw)
        if key and key not in out:
            out[key] = raw.strip()
    return out


class SponsorRegistry:
    """Lazy, per-run. Nothing is downloaded until a UK or NL job actually shows up,
    and each register is fetched at most once per process."""

    def __init__(self):
        self._uk: dict[str, str] | None = None
        self._nl: dict[str, str] | None = None
        self.errors: list[str] = []

    # ── loading
    def _get(self, url: str, timeout: int = 60):
        from core_eval_hosted import _LI_GET, HEADERS   # same impersonating client
        return _LI_GET(url, headers=HEADERS, timeout=timeout)

    def _load_uk(self) -> dict[str, str]:
        if self._uk is not None:
            return self._uk
        self._uk = {}
        try:
            from bs4 import BeautifulSoup
            page = self._get(UK_PUBLICATION, timeout=40)
            soup = BeautifulSoup(page.text, "html.parser")
            csv_url = next((a["href"] for a in soup.find_all("a", href=True)
                            if a["href"].lower().endswith(".csv")), "")
            if not csv_url:
                raise RuntimeError("no CSV link on the gov.uk publication page")
            # The filename carries the publication date, so it changes daily — always
            # follow the page rather than hardcoding a URL.
            rows = list(csv.reader(io.StringIO(self._get(csv_url).text)))
            header, body = rows[0], rows[1:]
            name_i  = header.index("Organisation Name")
            route_i = header.index("Route") if "Route" in header else None
            kept = []
            for row in body:
                if len(row) <= name_i:
                    continue
                if route_i is not None and len(row) > route_i:
                    if row[route_i].strip().lower() not in UK_ROUTES_KEPT:
                        continue
                kept.append(row[name_i])
            self._uk = _index(kept)
            print(f"  [sponsors] UK register: {len(self._uk)} Skilled Worker sponsors")
        except Exception as e:
            self.errors.append(f"UK register unavailable ({type(e).__name__}: {e})")
            print(f"  [sponsors] Warning: UK register unavailable — {type(e).__name__}: {e}")
        return self._uk

    def _load_nl(self) -> dict[str, str]:
        if self._nl is not None:
            return self._nl
        self._nl = {}
        try:
            from bs4 import BeautifulSoup
            soup  = BeautifulSoup(self._get(NL_REGISTER).text, "html.parser")
            table = soup.find("table")
            names = []
            for tr in (table.find_all("tr") if table else []):
                # The organisation is a <th> and the KvK number a <td>, so both are needed.
                cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])]
                name = next((c for c in cells if c and not c.replace(" ", "").isdigit()), "")
                if name:
                    names.append(name)
            self._nl = _index(names)
            print(f"  [sponsors] NL IND register: {len(self._nl)} recognised sponsors")
        except Exception as e:
            self.errors.append(f"NL register unavailable ({type(e).__name__}: {e})")
            print(f"  [sponsors] Warning: NL register unavailable — {type(e).__name__}: {e}")
        return self._nl

    # ── matching
    @staticmethod
    def _match(company: str, index: dict[str, str]) -> str:
        """Returns the registry spelling of the match, or "".

        Exact canonical match first. Then a containment check, but only when the
        company name carries at least one distinctive word — otherwise "Starling"
        would match "Starling Equine Vets" and "Wise" would match "Wise Home Care".
        """
        key = _canonical(company)
        if not key:
            return ""
        if key in index:
            return index[key]

        words = [w for w in key.split() if w not in _GENERIC and len(w) > 3]
        if not words:
            return ""
        # Require the full company phrase to appear as a word-boundary run inside the
        # registry entry (catches "roofoods deliveroo" for "deliveroo"), and require
        # that phrase to be distinctive on its own.
        if len(key) < 5:
            return ""
        pattern = re.compile(rf"(?:^|\s){re.escape(key)}(?:\s|$)")
        hits = [orig for canon, orig in index.items() if pattern.search(canon)]
        return hits[0] if len(hits) == 1 else ""

    def lookup(self, company: str, location: str) -> str:
        """Sheet-column text: "" when unknown, otherwise a short human-readable note."""
        country = location_country(location)
        if country == "united kingdom":
            hit = self._match(company, self._load_uk())
            return f"UK Skilled Worker licence — {hit}" if hit else ""
        if country == "netherlands":
            hit = self._match(company, self._load_nl())
            return f"NL recognised sponsor — {hit}" if hit else ""
        return ""   # no public register verified for the other countries
