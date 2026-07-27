"""Source pullers. Each returns a list of normalised records:

    {"date": "YYYY-MM-DD", "source": str, "ref": str, "text": str, "in_universe": bool}

Every puller parses defensively. Public APIs change shape without notice and a
KeyError on a Tuesday morning should not take down the whole run.
"""

import datetime as dt
import re

import requests

from config import (
    DEVICE_TERMS,
    LOOKBACK_DAYS,
    RADIOPHARMA_TERMS,
    REQUEST_TIMEOUT,
    UNIVERSE,
    USER_AGENT,
)

HEADERS = {"User-Agent": USER_AGENT}


def _since(days=LOOKBACK_DAYS):
    return dt.date.today() - dt.timedelta(days=days)


# --------------------------------------------------------------------------- #
# Universe matching
#
# Matches a company by whole-phrase, word-boundary search — NOT by first token.
# First-token matching had two failures: it collided ("ABX advanced biochemical
# compounds" and "ABX-CRO" both reduced to "abx") and it silently dropped any
# name whose first token was <=3 chars (ITM, ABX never matched at all).
#
# Each company is matched on its full normalised name, plus a short alias only
# where that alias is distinctive enough to be safe. Generic short forms (bare
# "Fusion", "Perspective", "Convergent") are deliberately NOT aliased — precision
# over recall: better to miss a rare abbreviated mention than mislabel a record.
# --------------------------------------------------------------------------- #

def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^\w\s-]", " ", s)      # keep word chars, whitespace, hyphen
    return re.sub(r"\s+", " ", s).strip()


# Safe short aliases only. Full canonical names are always matched, so these
# purely ADD recall for common abbreviations that appear in sponsor fields.
_SHORT_ALIASES = {
    "Telix Pharmaceuticals": ["telix"],
    "Y-mAbs Therapeutics": ["y-mabs"],
    "Eli Lilly": ["lilly"],
    "Bristol Myers Squibb": ["bristol-myers squibb", "bms"],
    "TerraPower Isotopes": ["terrapower"],
    "Jubilant Radiopharma": ["jubilant draximage", "jubilant"],
    "ITM Isotope Technologies": ["itm radiopharma", "itm isotopen", "itm"],
    "AtomVie Global Radiopharma": ["atomvie"],
    "Seibersdorf Laboratories": ["seibersdorf"],
    "NorthStar Medical Radioisotopes": ["northstar"],
    "ABX advanced biochemical compounds": ["abx gmbh"],  # NOT bare 'abx' (collides w/ ABX-CRO)
}


def _build_patterns():
    pats = []  # (canonical, compiled_regex, phrase_len)
    for name in UNIVERSE:
        phrases = {_norm(name)}
        phrases.update(_norm(a) for a in _SHORT_ALIASES.get(name, []))
        for ph in phrases:
            if not ph:
                continue
            # (?<!\w) / (?!\w) behave like \b but handle hyphenated forms cleanly
            rx = re.compile(r"(?<!\w)" + re.escape(ph) + r"(?!\w)")
            pats.append((name, rx, len(ph)))
    # longest phrase first, so a specific full name wins over any shorter alias
    pats.sort(key=lambda t: t[2], reverse=True)
    return pats


_PATTERNS = _build_patterns()


def _hits_universe(text: str):
    norm = _norm(text)
    for name, rx, _ in _PATTERNS:
        if rx.search(norm):
            return name
    return None


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


# --------------------------------------------------------------------------- #
# ClinicalTrials.gov API v2
# --------------------------------------------------------------------------- #

CTG_URL = "https://clinicaltrials.gov/api/v2/studies"


def pull_clinicaltrials(terms=None, days=LOOKBACK_DAYS):
    """Studies updated in the lookback window matching radiopharma terms.

    Phase transitions are the signal worth having here. This puller reports the
    current phase and status on each update; comparing against the prior stored
    record for the same NCT ID is what turns it into a transition event.
    """
    terms = terms or RADIOPHARMA_TERMS
    since = _since(days).isoformat()
    out = []

    for term in terms:
        params = {
            "query.term": term,
            "filter.advanced": f"AREA[LastUpdatePostDate]RANGE[{since},MAX]",
            "pageSize": 50,
            "countTotal": "true",
        }
        try:
            r = requests.get(CTG_URL, params=params, headers=HEADERS,
                             timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            studies = r.json().get("studies", [])
        except Exception as e:  # noqa: BLE001
            print(f"  [ctg] '{term}' failed: {e}")
            continue

        for s in studies:
            ps = s.get("protocolSection", {})
            ident = ps.get("identificationModule", {})
            status = ps.get("statusModule", {})
            design = ps.get("designModule", {})
            sponsor = ps.get("sponsorCollaboratorsModule", {})

            nct = ident.get("nctId", "")
            title = _clean(ident.get("briefTitle", ""))
            phases = ", ".join(design.get("phases", []) or []) or "N/A"
            state = status.get("overallStatus", "")
            lead = (sponsor.get("leadSponsor") or {}).get("name", "Undisclosed")
            date = (status.get("lastUpdatePostDateStruct") or {}).get("date", "")

            if not (nct and date):
                continue
            if len(date) == 7:          # YYYY-MM occasionally appears
                date = f"{date}-01"

            text = f"{title} | phase: {phases} | status: {state} | sponsor: {lead}"
            out.append({
                "date": date,
                "source": "ClinicalTrials.gov",
                "ref": nct,
                "text": text,
                "in_universe": bool(_hits_universe(f"{lead} {title}")),
            })

    return out


# --------------------------------------------------------------------------- #
# openFDA — 510(k) clearances and drug approvals
# --------------------------------------------------------------------------- #

FDA_510K = "https://api.fda.gov/device/510k.json"
FDA_DRUG = "https://api.fda.gov/drug/drugsfda.json"


def pull_fda_510k(terms=None, days=LOOKBACK_DAYS):
    """Device clearances — the dosimetry and imaging-adjacent side of the space."""
    terms = terms or DEVICE_TERMS
    since = _since(days).strftime("%Y%m%d")
    today = dt.date.today().strftime("%Y%m%d")
    out = []

    for term in terms:
        params = {
            "search": f'device_name:"{term}" AND decision_date:[{since}+TO+{today}]',
            "limit": 50,
        }
        try:
            r = requests.get(FDA_510K, params=params, headers=HEADERS,
                             timeout=REQUEST_TIMEOUT)
            if r.status_code == 404:      # openFDA returns 404 for zero results
                continue
            r.raise_for_status()
            results = r.json().get("results", [])
        except Exception as e:  # noqa: BLE001
            print(f"  [510k] '{term}' failed: {e}")
            continue

        for k in results:
            raw_date = k.get("decision_date", "")
            if len(raw_date) == 8:
                date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
            else:
                date = raw_date
            applicant = _clean(k.get("applicant", "Undisclosed"))
            device = _clean(k.get("device_name", ""))
            if not date:
                continue
            out.append({
                "date": date,
                "source": "openFDA 510(k)",
                "ref": k.get("k_number", ""),
                "text": f"510(k) clearance: {device} | applicant: {applicant}",
                "in_universe": bool(_hits_universe(applicant)),
            })

    return out


def pull_fda_approvals(days=LOOKBACK_DAYS):
    """Drug approvals filtered to radiopharma-relevant sponsors and names.

    drugsfda has no clean date filter on submission status, so this pulls by
    sponsor from the universe and filters client-side. Narrow but high precision.
    """
    since = _since(days)
    out = []

    for name in UNIVERSE:
        params = {"search": f'sponsor_name:"{name}"', "limit": 20}
        try:
            r = requests.get(FDA_DRUG, params=params, headers=HEADERS,
                             timeout=REQUEST_TIMEOUT)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            results = r.json().get("results", [])
        except Exception as e:  # noqa: BLE001
            print(f"  [drugsfda] '{name}' failed: {e}")
            continue

        for app in results:
            brand = "Undisclosed"
            products = app.get("products") or []
            if products:
                brand = _clean(products[0].get("brand_name", "Undisclosed"))
            for sub in app.get("submissions", []) or []:
                raw = sub.get("submission_status_date", "")
                if len(raw) != 8:
                    continue
                date = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
                if dt.date.fromisoformat(date) < since:
                    continue
                if sub.get("submission_status") != "AP":
                    continue
                out.append({
                    "date": date,
                    "source": "openFDA drugsfda",
                    "ref": app.get("application_number", ""),
                    "text": (f"Approval action: {brand} | sponsor: {name} | "
                             f"type: {sub.get('submission_type', '')}"),
                    "in_universe": True,
                })

    return out


PULLERS = {
    "clinicaltrials": pull_clinicaltrials,
    "fda_510k": pull_fda_510k,
    "fda_approvals": pull_fda_approvals,
}
