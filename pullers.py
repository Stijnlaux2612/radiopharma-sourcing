"""Source pullers. Each returns a list of normalised records:

    {"date": "YYYY-MM-DD", "source": str, "ref": str, "text": str, "in_universe": bool}

Every puller parses defensively. Public APIs change shape without notice and a
KeyError on a Tuesday morning should not take down the whole run.
"""

import csv
import datetime as dt
import io
import re
import zipfile

import requests

from config import (
    EMA_MEDICINES_XLSX,
    EMA_MIN_LOOKBACK_DAYS,
    CMS_HCPCS_PAGE,
    CMS_MIN_LOOKBACK_DAYS,
    DEVICE_TERMS,
    LOOKBACK_DAYS,
    RADIOPHARMA_TERMS,
    REQUEST_TIMEOUT,
    UNIVERSE,
    USER_AGENT,
)
from store import get_trial_states, upsert_trial_states

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
    # drugsfda abbreviates sponsors ('GE HLTHCARE INC'), so the canonical
    # spelling alone would never match the string the API actually returns.
    "GE HealthCare": ["ge hlthcare", "ge healthcare inc"],   # NOT bare 'ge'
    "Sinotau Pharmaceutical Group": ["sinotau"],
    "QSAM Therapeutics": ["qsam"],
    # hyphen-less spelling is equally common in sponsor fields
    "Full-Life Technologies": ["full life technologies"],
    # EMA lists the EU entity, which shares no phrase with the canonical name
    "SHINE Technologies": ["shine europe"],
    "ITM Isotope Technologies": ["itm medical isotopes"],
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
CTG_PAGE_SIZE = 100
CTG_MAX_PAGES = 20      # runaway guard: 2000 studies per term is far beyond normal


# --------------------------------------------------------------------------- #
# Relevance guard
#
# `query.term` is a full-text search over the WHOLE study record — title,
# summary, eligibility, outcome measures. It already ANDs its tokens, so the
# query is not malformed; the problem is that some of our terms are ordinary
# English words. "targeted alpha therapy" returned 30 studies of which 29 were
# irrelevant: a physiotherapy trial reporting Cronbach's *alpha* contains all
# three tokens legitimately.
#
# Quoting the phrase is NOT the fix — it drops the concept-pair terms that work
# ("PSMA radionuclide" 4 -> 0, "FAP targeted radionuclide" 2 -> 0), because
# those aren't literal phrases anyone writes in a protocol.
#
# So the query stays broad (the discovery net is the point) and results are
# validated instead: a study is kept only if an isotope notation or a genuine
# radio-modality word appears in its title, summary or intervention names.
# Deliberately does NOT accept bare "PET"/"SPECT"/"F-18"/"Ga-68" — per config.py
# those flood the net with routine imaging.
# --------------------------------------------------------------------------- #

# Isotope notation. CASE-SENSITIVE and tightly bound on purpose: element symbols
# are always properly capitalised, and allowing whitespace between the mass number
# and the symbol produced real false positives — "day -4 at 14.5 mg/kg" matched as
# mass-4 astatine, and a case-insensitive "30min" would match as indium.
_SYM = "Lu|Ac|Pb|Cu|Tb|Tc|Ga|Zr|Sc|Sm|Ho|Re|Er|In|At|Bi|Ra|Y|I|F"
# C/N/O are valid only in brackets, for the short-lived PET nuclides ([11C], [13N],
# [15O]). C-14 and tritium are excluded: those labels mark mass-balance ADME
# studies, which every oral drug programme runs — a lab technique, never a
# radiopharma product.
_SYM_BRACKET = _SYM + "|C|N|O"
_ISOTOPE_RX = re.compile(
    r"\[(?!14C\]|3H\]|2H\])\d{1,3}m?(?:" + _SYM_BRACKET + r")\]"   # [177Lu], [68Ga], [11C]
    r"|\b\d{1,3}m?(?:" + _SYM + r")\b"        # 177Lu, 99mTc, 223Ra  (no space)
    r"|\b(?:" + _SYM + r")-\d{1,3}\b"         # Lu-177, Ac-225       (no space)
)

# CMS Addendum B descriptors are capped at 28 characters and write isotopes with
# no separator and in lower case ('Flotufolastat f18 diag 1 mci'), which none of
# the patterns above match. A space is deliberately NOT allowed here: 'In 100 ml'
# would otherwise read as indium-100.
_ISOTOPE_COMPACT_RX = re.compile(
    r"\b(?:F|Ga|Lu|Ac|Tc|Cu|Zr|Ra|Pb|At|Sm|Ho|Tb|Sc|Bi|Er|Re|Y|I)-?\d{2,3}\b",
    re.I,
)

# Radiopharmaceutical INN stems. Short descriptors often name the agent without
# ever mentioning its isotope, so neither isotope pattern can catch them.
# NOTE: these stems are deliberately NOT anchored with \b. They occur mid-word —
# 'flortaucipir', 'flotufolastat', 'oxodotreotide' — so a leading \b would never
# match, which is exactly why A9601 (Tauvid) was missed on the first attempt.
# Only the short, ambiguous tokens keep boundaries.
_AGENT_RX = re.compile(
    r"folastat"                            # piflufolastat, flotufolastat
    r"|taucipir|vipivotide"                # flortaucipir, Pluvicto
    r"|dotatate|dotatoc|dotreotide"
    r"|ioflupane|fluciclovine|fluoroestradiol|fluorodopa|flurpiridaz"
    r"|florbeta|flutemetamol|flortaucipir"
    r"|pentixafor|pentixather"
    r"|exametazime|bicisate|sestamibi|mertiatide|tilmanocept"
    r"|medronate|oxidronate|pyrophosphate"
    r"|\bfapi\b|\bpsma\b|\bmibg\b",
    re.I,
)


def _looks_radiopharma(text: str) -> bool:
    """Single relevance test shared by every source."""
    return bool(_ISOTOPE_RX.search(text) or _MODALITY_RX.search(text)
                or _ISOTOPE_COMPACT_RX.search(text) or _AGENT_RX.search(text))

# Elements that essentially only appear in a nuclear-medicine context.
_ELEM_STRONG = "lutetium|actinium|astatine|terbium|radium|bismuth|technetium"
# Elements with heavy non-nuclear usage (copper metabolism, iodine supplementation,
# contrast media). Require an adjacent mass number before trusting them.
_ELEM_WEAK = ("copper|iodine|gallium|yttrium|samarium|holmium|rhenium|erbium|"
              "scandium|zirconium|indium|fluorine")

_MODALITY_RX = re.compile(
    r"\b(?:" + _ELEM_STRONG + r")\b"
    r"|\b(?:" + _ELEM_WEAK + r")[- ]?\d{1,3}\b"
    # radio-* modality words. NOT "radioactiv*" — that matches the "radioactive
    # seeds" boilerplate in ordinary radiation-oncology trials. NOT "brachytherapy"
    # either: sealed-source radiation is a different modality from radiopharma.
    # NOT plain "radiother*" either — external-beam radiotherapy is a linac, not a
    # drug. Genuine radiopharma trials that mention it also carry isotope notation.
    r"|\bradio(?:ligand|nuclide|pharmaceutic|immunother|conjugat|iodin|label|isotop)\w*"
    r"|\btheranostic\w*|\bpsma\b|\bdotatate\b|\bdotatoc\b"
    r"|\blutathera\b|\bpluvicto\b|\bprrt\b"
    r"|\balpha[- ]emitt\w*|\bscintigraph\w*",
    re.I,
)


# --------------------------------------------------------------------------- #
# Commercial-sponsor filter
#
# Academic and government sponsors run large volumes of investigator-initiated
# imaging and treatment studies. They are on-topic but they are not company
# catalysts — a university PET study does not move a position.
#
# Escape hatch: a record that matches the universe is ALWAYS kept regardless of
# sponsor, so an academic trial that names a watchlist company still surfaces.
# This filter is deliberately a positive match on academic/government markers
# rather than a "must look like a company" test — the latter would suppress
# exactly the unknown new sponsors the discovery net exists to find.
# --------------------------------------------------------------------------- #

_NONCOMMERCIAL_RX = re.compile(
    r"\buniversit\w*|\buniversidad\b|\buniversitair\w*|\buniversitet\w*"
    r"|\bcollege\b|\bschool of medicine\b|\bacadem\w*"
    r"|\bhospital\w*|\bhopital\b|\bhôpital\b|\bhospices\b|\bziekenhuis\b|\bklinik\w*"
    r"|\bcentre hospitalier\b|\bmedical cent\w+|\bcancer cent\w+|\bhealth system\b"
    r"|\bclinic\b|\bpolyclinic\b|\binfirmary\b"
    r"|\binstitut\w*|\bnational institute\w*|\bnih\b|\bnci\b|\bnhs\b|\binserm\b|\bcnrs\b"
    r"|\bfoundation\b|\bfondazione\b|\bfundaci\w+|\bstiftung\b|\btrust\b"
    r"|\bministry\b|\bdepartment of health\b|\bva \b|\bveterans\b"
    r"|\bconsortium\b|\bcooperative group\b|\bsociety\b|\bassociation\b"
    r"|\bcharit\w+|\bassistance publique\b|\bcancer research\b"
    # public health authorities and research alliances that carry none of the
    # words above — all observed leaks from the first live run
    r"|\bcancer control\b|\bazienda\b|\birccs\b|\bausl\b|\busl\b|\bahs\b"
    r"|\balianza\b|\balliance\b|\bcentro\b|\bcentre de\b|\bregional health\b",
    re.I,
)


def _is_commercial(sponsor: str) -> bool:
    return not _NONCOMMERCIAL_RX.search(sponsor or "")


# --------------------------------------------------------------------------- #
# Phase / status transitions
#
# A trial sitting in the lookback window is re-reported every week with its
# CURRENT phase, which says nothing about movement. The signal worth having is
# the change: Ph2 -> Ph3, or RECRUITING -> TERMINATED. That requires memory, so
# the last-seen phase/status per NCT lives in store.trial_state and each pull is
# diffed against it.
#
# A trial seen for the first time is NOT a transition — there is no prior value
# to have moved from, and emitting one would fire a false transition for every
# trial on the very first run.
# --------------------------------------------------------------------------- #

_PHASE_LABEL = {
    "EARLY_PHASE1": "EarlyPh1", "PHASE1": "Ph1", "PHASE2": "Ph2",
    "PHASE3": "Ph3", "PHASE4": "Ph4", "NA": "N/A",
}


def _pretty_phase(raw: str) -> str:
    """'PHASE1, PHASE2' -> 'Ph1, Ph2'. Unknown values pass through unchanged."""
    if not raw:
        return "N/A"
    return ", ".join(_PHASE_LABEL.get(p.strip(), p.strip()) for p in raw.split(","))


def _describe_transition(prior: dict, phase: str, status: str) -> str:
    """Human-readable transition, or '' if nothing moved."""
    parts = []
    if prior.get("phase") != phase:
        parts.append(f"{_pretty_phase(prior.get('phase'))} → {_pretty_phase(phase)}")
    if prior.get("status") != status:
        parts.append(f"{prior.get('status') or 'unknown'} → {status}")
    return "; ".join(parts)


def _relevance_blob(ps: dict) -> str:
    """Title + summary + intervention names — the fields that name the agent."""
    ident = ps.get("identificationModule", {}) or {}
    desc = ps.get("descriptionModule", {}) or {}
    parts = [
        ident.get("briefTitle", ""),
        ident.get("officialTitle", ""),
        desc.get("briefSummary", ""),
    ]
    arms = ps.get("armsInterventionsModule", {}) or {}
    for iv in arms.get("interventions", []) or []:
        parts.append(iv.get("name", "") or "")
        parts.append(iv.get("description", "") or "")
    return " ".join(p for p in parts if p)


def _is_radiopharma(ps: dict) -> bool:
    return _looks_radiopharma(_relevance_blob(ps))


def pull_clinicaltrials(terms=None, days=LOOKBACK_DAYS):
    """Studies updated in the lookback window matching radiopharma terms.

    Phase transitions are the signal worth having here. This puller reports the
    current phase and status on each update; comparing against the prior stored
    record for the same NCT ID is what turns it into a transition event.
    """
    terms = terms or RADIOPHARMA_TERMS
    since = _since(days).isoformat()
    out = []
    dropped = 0
    non_commercial = 0
    current = {}          # nct -> (phase, status) as seen in THIS run

    for term in terms:
        studies = []
        token = None
        # Paginate. Previously a single 50-result page was taken and the rest of
        # the term's matches were discarded silently — no error, just missing
        # records. The page cap is a runaway guard, not an expected limit.
        for _page in range(CTG_MAX_PAGES):
            params = {
                "query.term": term,
                "filter.advanced": f"AREA[LastUpdatePostDate]RANGE[{since},MAX]",
                "pageSize": CTG_PAGE_SIZE,
                "countTotal": "true",
            }
            if token:
                params["pageToken"] = token
            try:
                r = requests.get(CTG_URL, params=params, headers=HEADERS,
                                 timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                payload = r.json()
            except Exception as e:  # noqa: BLE001
                print(f"  [ctg] '{term}' failed: {e}")
                break
            studies.extend(payload.get("studies", []) or [])
            token = payload.get("nextPageToken")
            if not token:
                break
        else:
            print(f"  [ctg] '{term}' hit the {CTG_MAX_PAGES}-page cap; may be truncated")

        for s in studies:
            ps = s.get("protocolSection", {})

            # Full-text search returns unrelated studies that merely share common
            # words with a term; require a real radiopharma signal in the record.
            if not _is_radiopharma(ps):
                dropped += 1
                continue

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

            hit = _hits_universe(f"{lead} {title}")

            # Academic/government sponsors are dropped unless the record names a
            # watchlist company — a university PET study is not a company catalyst.
            if not hit and not _is_commercial(lead):
                non_commercial += 1
                continue

            text = f"{title} | phase: {phases} | status: {state} | sponsor: {lead}"
            current[nct] = (phases, state)
            out.append({
                "date": date,
                "source": "ClinicalTrials.gov",
                "ref": nct,
                "text": text,
                "in_universe": bool(hit),
            })

    # --- transition detection -------------------------------------------- #
    # Diff against the last-seen state BEFORE writing the new one, otherwise
    # every trial compares against itself and nothing ever looks like a change.
    priors = get_trial_states(list(current))
    # A trial matched by several search terms appears in `out` more than once,
    # so count distinct NCTs — counting rows overstates it (10 rows, 3 trials).
    transitioned = set()
    for r in out:
        prior = priors.get(r["ref"])
        if not prior:                     # first sighting: not a transition
            continue
        phase, state = current[r["ref"]]
        moved = _describe_transition(prior, phase, state)
        if moved:
            transitioned.add(r["ref"])
            r["text"] = f"PHASE TRANSITION: {moved} | {r['text']}"
    transitions = len(transitioned)
    upsert_trial_states((n, p, s) for n, (p, s) in current.items())

    if dropped:
        print(f"  [ctg] relevance guard dropped {dropped} off-topic hits")
    if non_commercial:
        print(f"  [ctg] sponsor filter dropped {non_commercial} academic/government records")
    print(f"  [ctg] {len(current)} trials tracked, {transitions} transition(s) detected")
    return out


# --------------------------------------------------------------------------- #
# openFDA — 510(k) clearances and drug approvals
# --------------------------------------------------------------------------- #

FDA_510K = "https://api.fda.gov/device/510k.json"
FDA_DRUG = "https://api.fda.gov/drug/drugsfda.json"
FDA_PAGE_SIZE = 100
FDA_MAX_RECORDS = 2000   # runaway guard; a 10-day window is ~40 applications


def pull_fda_510k(terms=None, days=LOOKBACK_DAYS):
    """Device clearances — the dosimetry and imaging-adjacent side of the space."""
    terms = terms or DEVICE_TERMS
    since = _since(days).strftime("%Y%m%d")
    today = dt.date.today().strftime("%Y%m%d")
    out = []

    for term in terms:
        # The range separator must be a REAL SPACE here, not a literal '+'.
        # openFDA's documented syntax is [start+TO+end] because '+' encodes a
        # space in a raw URL — but requests percent-encodes a literal '+' to
        # %2B, which produced '%2BTO%2B' and a 500 on every single query.
        params = {
            "search": f'device_name:"{term}" AND decision_date:[{since} TO {today}]',
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
    """Approval actions in the window, by date — not by sponsor.

    The previous version looped over all 71 universe names querying
    sponsor_name:"<name>" and returned zero every time, silently. Two reasons:
    drugsfda stores sponsors UPPERCASE and matches case-sensitively, and it
    matches the WHOLE stored value rather than a sub-phrase — the real strings
    are abbreviated corporate forms ('NOVARTIS', 'TELIX', 'ELI LILLY AND CO'),
    so 'Telix Pharmaceuticals' could never have matched anything.

    Its docstring also claimed drugsfda has no date filter. It does:
    submissions.submission_status_date takes a range, ~41 applications over a
    10-day window. So this now runs ONE paginated date query instead of 71
    sponsor queries, and — unlike the old version — can discover approvals from
    sponsors that are not yet on the watchlist.
    """
    since = _since(days)
    since_s = since.strftime("%Y%m%d")
    today = dt.date.today().strftime("%Y%m%d")
    search = (f'submissions.submission_status_date:[{since_s} TO {today}]'
              f' AND submissions.submission_status:"AP"')
    out = []
    skip = 0

    while skip < FDA_MAX_RECORDS:
        params = {"search": search, "limit": FDA_PAGE_SIZE, "skip": skip}
        try:
            r = requests.get(FDA_DRUG, params=params, headers=HEADERS,
                             timeout=REQUEST_TIMEOUT)
            if r.status_code == 404:      # openFDA returns 404 for zero results
                break
            r.raise_for_status()
            results = r.json().get("results", []) or []
        except Exception as e:  # noqa: BLE001
            print(f"  [drugsfda] page at skip={skip} failed: {e}")
            break
        if not results:
            break

        for app in results:
            sponsor = _clean(app.get("sponsor_name", "")) or "Undisclosed"
            products = app.get("products") or []
            brand = _clean(products[0].get("brand_name", "")) if products else ""
            generic = _clean(products[0].get("active_ingredients", [{}])[0]
                             .get("name", "")) if products else ""
            brand = brand or "Undisclosed"

            # The date filter matches an application if ANY of its submissions
            # falls in range, so the specific AP action is re-checked below.
            hit = _hits_universe(f"{sponsor} {brand} {generic}")
            names = f"{brand} {generic}"

            # The DRUG must be radiopharma — a universe sponsor is not enough.
            # Large pharma is on the watchlist for its radioligand programmes,
            # but admitting on sponsor alone floods the feed with every Novartis
            # and BMS supplement (Tegretol, Eliquis, Revlimid): 65 of 67 records
            # over a year. config.py is explicit that the universe boosts a
            # record's rank rather than qualifying it, so it only sets
            # in_universe here.
            if not _looks_radiopharma(names):
                continue

            for sub in app.get("submissions", []) or []:
                raw = sub.get("submission_status_date", "")
                if len(raw) != 8 or sub.get("submission_status") != "AP":
                    continue
                date = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
                try:
                    if dt.date.fromisoformat(date) < since:
                        continue
                except ValueError:
                    continue
                out.append({
                    "date": date,
                    "source": "openFDA drugsfda",
                    "ref": app.get("application_number", ""),
                    "text": (f"Approval action: {brand} | sponsor: {sponsor} | "
                             f"type: {sub.get('submission_type', '')}"),
                    "in_universe": bool(hit),
                })

        if len(results) < FDA_PAGE_SIZE:
            break
        skip += FDA_PAGE_SIZE

    return out


# --------------------------------------------------------------------------- #
# CMS — HCPCS quarterly update
#
# Reimbursement often gates commercial viability in radiopharma more than
# efficacy does, so a new HCPCS code (or a termination date appearing on an
# existing one) is a real catalyst.
#
# There is no CMS API for this: data.cms.gov's catalogue carries spending
# datasets but not code issuance. The quarterly ZIP is the source of truth. It
# contains a fixed-width .txt plus the record layout that defines the offsets
# below, so no Excel dependency is needed.
#
# Deliberately NOT covered here:
#   * OPPS pass-through status (Addendum B) — the download links on cms.gov are
#     rendered client-side, so plain HTTP cannot reach the file.
#   * NTAP decisions — published only as PDFs.
# Both need a different mechanism than this puller; see RUNBOOK 3.1.
# --------------------------------------------------------------------------- #

# Offsets are 1-based in the CMS record layout; converted to slices here.
_HCPCS_FIELDS = {
    "code":  (0, 5),        # 1-5
    "long":  (11, 91),      # 12-91
    "short": (91, 119),     # 92-119
    "added": (268, 276),    # 269-276  YYYYMMDD
    "eff":   (276, 284),    # 277-284
    "term":  (284, 292),    # 285-292
}
_HCPCS_MIN_LEN = 292


def _hcpcs_latest_zip_url():
    """Newest quarterly ZIP link from the CMS page. Returns None on failure.

    The page lists newest first. Scraped rather than constructed from today's
    date because the filename has drifted ('...-hcpcs-file.zip' vs
    '...-hcpcs-files.zip') and a guessed URL silently 404s.
    """
    try:
        r = requests.get(CMS_HCPCS_PAGE, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"  [cms] could not load the HCPCS index page: {e}")
        return None
    links = re.findall(r'href="([^"]*alpha-numeric-hcpcs-files?\.zip)"', r.text, re.I)
    if not links:
        print("  [cms] no HCPCS ZIP link found — page structure may have changed")
        return None
    href = links[0]
    return href if href.startswith("http") else "https://www.cms.gov" + href


def _iso(raw: str):
    """'20240701' -> date, or None if absent/malformed."""
    raw = (raw or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return dt.date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
    except ValueError:
        return None


def pull_cms(days=LOOKBACK_DAYS):
    """New, newly-effective and terminating radiopharma HCPCS codes."""
    # Quarterly source: a 10-day window would always be empty, so widen.
    days = max(days, CMS_MIN_LOOKBACK_DAYS)
    since = _since(days)

    url = _hcpcs_latest_zip_url()
    if not url:
        return []
    try:
        r = requests.get(url, headers=HEADERS, timeout=max(REQUEST_TIMEOUT, 90))
        r.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = [n for n in zf.namelist()
                 if n.lower().endswith(".txt") and "recordlayout" not in n.lower()
                 and "proc_notes" not in n.lower()]
        if not names:
            print("  [cms] ZIP contained no data .txt — layout may have changed")
            return []
        raw = zf.read(names[0]).decode("latin-1", errors="replace")
    except Exception as e:  # noqa: BLE001
        print(f"  [cms] fetch/unpack failed: {e}")
        return []

    out = []
    scanned = matched = 0
    for line in raw.splitlines():
        if len(line) < _HCPCS_MIN_LEN:
            continue
        scanned += 1
        f = {k: line[a:b].strip() for k, (a, b) in _HCPCS_FIELDS.items()}
        desc = _clean(f["long"] or f["short"])
        if not (f["code"] and desc):
            continue
        if not _looks_radiopharma(desc):
            continue
        matched += 1

        added = _iso(f["added"])

        # One record per action in the window. A code can legitimately produce
        # more than one (added and terminated in the same quarter).
        for label, key in (("New HCPCS code", "added"),
                           ("HCPCS code effective", "eff"),
                           ("HCPCS code TERMINATING", "term")):
            d = _iso(f[key])
            if not d or d < since:
                continue
            # An effective date on a code added long ago is CMS re-dating its
            # back catalogue, not a catalyst — 14 of 15 hits in testing were a
            # bulk 2025-01-01 reset on decades-old Tc-99m codes. Only report an
            # effective date when the code itself is also new in this window.
            if key == "eff" and (not added or added < since):
                continue
            out.append({
                "date": d.isoformat(),
                "source": "CMS HCPCS",
                "ref": f["code"],
                "text": f"{label}: {f['code']} | {desc}",
                "in_universe": bool(_hits_universe(desc)),
            })

    print(f"  [cms] {scanned} codes scanned, {matched} radiopharma, "
          f"{len(out)} action(s) in the last {days}d")
    return out


# --------------------------------------------------------------------------- #
# CMS — OPPS Addendum B (transitional pass-through status)
#
# Pass-through gives a product separate OPPS payment for a limited period rather
# than bundling it into the procedure APC. For a radiopharmaceutical that is
# often the difference between a hospital adopting it and not, so both GAINING
# pass-through and its EXPIRY are commercially material.
#
# Status indicators: G = pass-through drug/biological, H = pass-through device.
#
# No state table is needed to spot changes. Records are content-hashed on
# source|ref|text (see store.content_hash), so a code whose status indicator,
# APC, payment rate or expiry changes produces different text and therefore a
# new row, while an unchanged quarter produces nothing. The first run emits the
# current pass-through set as a baseline; after that only movement appears.
#
# LICENSING: cms.gov fronts this ZIP with an AMA license click-through, because
# Addendum B carries CPT (Level I) descriptors that the AMA copyrights. This
# puller keeps ONLY Level II codes — the letter-prefixed A/C/J series that CMS
# itself maintains — and every radiopharmaceutical lives there. No CPT
# descriptor is parsed, stored or emitted. Raised with the user before building.
# --------------------------------------------------------------------------- #

CMS_ADDENDUM_PAGE = ("https://www.cms.gov/medicare/payment/prospective-payment-systems/"
                     "hospital-outpatient-pps/quarterly-addenda-updates")
_PASS_THROUGH_SI = {"G", "H"}


def _addendum_b_url():
    """Newest OPPS Addendum B ZIP. Returns (url, quarter_date) or (None, None).

    The link is only present on the page inside an AMA license interstitial URL
    (…/apps/ama/license.asp?file=/files/zip/<name>.zip), so the real path is
    extracted from that query string rather than from an href.
    """
    try:
        r = requests.get(CMS_ADDENDUM_PAGE, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"  [cms-pt] could not load the addenda index: {e}")
        return None, None

    months = {m: i for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"], start=1)}

    # Step 1: the index only links to per-quarter SUB-PAGES; pick the newest.
    best = None
    for href in re.findall(r'href="([^"]*quarterly-addenda-updates/[^"]*addendum-b[^"]*)"',
                           r.text, re.I):
        m = re.search(r'(' + "|".join(months) + r')-(\d{4})', href, re.I)
        if not m:
            continue
        d = dt.date(int(m.group(2)), months[m.group(1).lower()], 1)
        if best is None or d > best[1]:
            best = (href, d)
    if not best:
        print("  [cms-pt] no Addendum B sub-page found — index layout changed")
        return None, None

    href, quarter = best
    page = href if href.startswith("http") else "https://www.cms.gov" + href

    # Step 2: the ZIP path appears only inside the AMA license interstitial URL.
    try:
        r2 = requests.get(page, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r2.raise_for_status()
    except Exception as e:  # noqa: BLE001
        print(f"  [cms-pt] could not load {page}: {e}")
        return None, None
    zips = re.findall(r'/files/zip/([A-Za-z0-9._\-]*addendum-b[A-Za-z0-9._\-]*\.zip)',
                      r2.text, re.I)
    if not zips:
        print("  [cms-pt] no Addendum B ZIP on the quarter page — layout changed")
        return None, None
    return "https://www.cms.gov/files/zip/" + zips[0], quarter


def pull_cms_passthrough(days=LOOKBACK_DAYS):
    """Radiopharma HCPCS codes currently holding OPPS pass-through status."""
    url, quarter = _addendum_b_url()
    if not url:
        return []
    try:
        r = requests.get(url, headers=HEADERS, timeout=max(REQUEST_TIMEOUT, 90))
        r.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            print("  [cms-pt] ZIP has no CSV — only the xlsx variant was published")
            return []
        rows = list(csv.reader(io.StringIO(
            zf.read(names[0]).decode("latin-1", errors="replace"))))
    except Exception as e:  # noqa: BLE001
        print(f"  [cms-pt] fetch/unpack failed: {e}")
        return []

    hdr_i = next((i for i, row in enumerate(rows)
                  if row and row[0].strip() == "HCPCS Code"), None)
    if hdr_i is None:
        print("  [cms-pt] no 'HCPCS Code' header row — layout changed")
        return []
    hdr = [c.strip() for c in rows[hdr_i]]

    def col(*wanted, default=None):
        for w in wanted:
            for i, c in enumerate(hdr):
                if c.lower() == w.lower():
                    return i
        return default

    c_code = col("HCPCS Code", default=0)
    c_desc = col("Short Descriptor", default=1)
    c_si = col("SI", "Status Indicator", default=2)
    c_apc = col("APC", default=3)
    c_rate = col("Payment Rate")
    c_exp = col("Drug and Device Pass-Through Expiration during Calendar Year")

    out = []
    level2 = radio = 0
    for row in rows[hdr_i + 1:]:
        if not row or not row[0].strip():
            continue
        code = row[c_code].strip()
        # Level II only — see LICENSING note above.
        if not code[:1].isalpha():
            continue
        level2 += 1
        desc = _clean(row[c_desc] if len(row) > c_desc else "")
        if not _looks_radiopharma(f"{code} {desc}"):
            continue
        radio += 1
        si = (row[c_si].strip() if len(row) > c_si else "")
        exp = (row[c_exp].strip() if c_exp is not None and len(row) > c_exp else "")
        if si not in _PASS_THROUGH_SI and not exp:
            continue
        apc = (row[c_apc].strip() if len(row) > c_apc else "")
        rate = (row[c_rate].strip() if c_rate is not None and len(row) > c_rate else "")
        label = "PASS-THROUGH" if si in _PASS_THROUGH_SI else "pass-through expiring"
        out.append({
            "date": quarter.isoformat(),
            "source": "CMS OPPS pass-through",
            "ref": code,
            "text": (f"{label}: {code} | {desc} | SI: {si or '-'} | "
                     f"APC: {apc or '-'} | rate: {rate or '-'}"
                     + (f" | expires: {exp}" if exp else "")),
            "in_universe": bool(_hits_universe(desc)),
        })

    print(f"  [cms-pt] {quarter:%Y-%m} addendum: {level2} Level II codes, "
          f"{radio} radiopharma, {len(out)} with pass-through status")
    return out


# --------------------------------------------------------------------------- #
# EMA — CHMP opinions and EU regulatory actions
#
# Closes the Europe blind spot. It matters here more than the source count
# suggests: the isotope-supply layer of the watchlist is overwhelmingly European
# (ITM, Curium, Orano Med, IRE ELiT, Eckert & Ziegler, SHINE), and none of it
# shows up in an FDA-only regulatory feed.
#
# EMA publishes a structured medicines report, so no page scraping and no PDF
# parsing — the CHMP agendas themselves are PDFs and would be far more fragile.
#
# Relevance is decided by ATC code, not text: V09 is diagnostic
# radiopharmaceuticals and V10 therapeutic, which is exact where text matching
# is not. Text matching alone pulled in pneumococcal vaccines and a porcine
# vaccine ('Coliprotec F4/F18' reads as fluorine-18). It is kept only as a
# fallback for products whose ATC is genuinely not yet assigned — that is how
# Cuprymina (copper (64Cu) chloride) still qualifies.
# --------------------------------------------------------------------------- #

_EMA_COLS = {
    "name": 1, "status": 3, "inn": 6, "substance": 7,
    "atc": 11, "mah": 25, "ec_decision": 26, "opinion": 29,
    "withdrawal": 30, "authorisation": 31, "refusal": 32, "url": 38,
}

# (column, label) — one record per dated action that falls in the window.
_EMA_EVENTS = [
    ("opinion",       "CHMP OPINION adopted"),
    ("authorisation", "EU marketing authorisation"),
    ("refusal",       "EU marketing authorisation REFUSED"),
    ("withdrawal",    "EU application WITHDRAWN"),
    ("ec_decision",   "EC decision"),
]


def _ema_date(v):
    """EMA dates arrive as datetime or as 'DD/MM/YYYY'. Returns date or None."""
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v or "").strip()
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})$", s)
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def pull_ema(days=LOOKBACK_DAYS):
    """CHMP opinions and EU regulatory actions on radiopharmaceuticals."""
    try:
        import openpyxl
    except ImportError:
        print("  [ema] openpyxl not installed — run: pip install -r requirements.txt")
        return []

    days = max(days, EMA_MIN_LOOKBACK_DAYS)
    since = _since(days)

    try:
        r = requests.get(EMA_MEDICINES_XLSX, headers=HEADERS,
                         timeout=max(REQUEST_TIMEOUT, 120))
        r.raise_for_status()
        wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True,
                                    data_only=True)
    except Exception as e:  # noqa: BLE001
        print(f"  [ema] fetch failed: {e}")
        return []

    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    hdr_i = next((i for i, row in enumerate(rows)
                  if row and str(row[1] or "").strip() == "Name of medicine"), None)
    if hdr_i is None:
        print("  [ema] no 'Name of medicine' header — report layout changed")
        return []

    def cell(row, key):
        i = _EMA_COLS[key]
        return str(row[i]).strip() if i < len(row) and row[i] is not None else ""

    out = []
    total = radio = 0
    for row in rows[hdr_i + 1:]:
        if not row or not row[_EMA_COLS["name"]]:
            continue
        # Veterinary products share the report and produce false matches.
        if str(row[0] or "").strip().lower() != "human":
            continue
        total += 1

        atc = cell(row, "atc").upper()
        if atc.startswith(("V09", "V10")):
            pass
        elif atc and not atc.startswith("NOT YET"):
            continue          # a real, non-radiopharma ATC — exclude
        elif not _looks_radiopharma(" ".join(
                cell(row, k) for k in ("name", "inn", "substance"))):
            continue
        radio += 1

        name = cell(row, "name")
        mah = cell(row, "mah") or "Undisclosed"
        status = cell(row, "status")
        inn = cell(row, "inn")

        for key, label in _EMA_EVENTS:
            d = _ema_date(row[_EMA_COLS[key]] if _EMA_COLS[key] < len(row) else None)
            if not d or d < since or d > dt.date.today():
                continue
            out.append({
                "date": d.isoformat(),
                "source": "EMA",
                "ref": cell(row, "url") or name,
                "text": (f"{label}: {name}"
                         + (f" ({inn})" if inn and inn.lower() != name.lower() else "")
                         + f" | status: {status or '-'} | ATC: {atc or '-'}"
                         f" | MAH: {mah}"),
                "in_universe": bool(_hits_universe(f"{mah} {name} {inn}")),
            })

    print(f"  [ema] {total} human medicines, {radio} radiopharma, "
          f"{len(out)} action(s) in the last {days}d")
    return out


PULLERS = {
    "clinicaltrials": pull_clinicaltrials,
    "fda_510k": pull_fda_510k,
    "fda_approvals": pull_fda_approvals,
    "cms": pull_cms,
    "cms_passthrough": pull_cms_passthrough,
    "ema": pull_ema,
}
