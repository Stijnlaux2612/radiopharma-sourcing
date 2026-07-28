"""Deterministic ordering for digest.txt. NOT the screening layer.

CLAUDE.md draws a firm line: ranking belongs to the React screening artifact and
no LLM call may enter this pipeline. This module respects both. It computes only
the arithmetic half of the formula —

    base x 2^(-age / half_life) x segment_weight x novelty

— from constants in config.py, so any ordering can be explained from those
numbers alone and is reproducible from the same input.

The thesis-fit term is absent on purpose. Fit is judgement, it is set by a human
in the screening artifact, and nothing here may substitute for it. Consequently
these scores are ORDERING AIDS, not the screening score: expect the artifact to
reorder rows once fit is applied.

feed.txt is untouched — it stays in source order, as the screening tool expects.
"""

import datetime as dt
import re

from config import (
    CATALYST_WEIGHTS,
    LARGE_PHARMA,
    NOVELTY_WEIGHTS,
    SEGMENT_WEIGHTS,
    SUPPLY_CHAIN,
)

# Ordered longest-prefix-first so "STOPPED — strategic" cannot be swallowed by a
# broader rule. Mirrors classify() in the screening artifact.
_PREFIX_TYPES = [
    ("PHASE TRANSITION",              None),   # resolved below (Ph3 vs other)
    ("READOUT POSTED",                "readout_posted"),
    ("READOUT DUE",                   "readout_due"),
    ("STOPPED — EFFICACY/SAFETY",     "stopped_efficacy"),
    ("STOPPED — strategic",           "stopped_strategic"),
    ("stopped — operational",         "stopped_operational"),
    ("FUNDING:",                      "funding"),
    ("PASS-THROUGH:",                 "cms_passthrough_gain"),
    ("pass-through expiring",         "cms_passthrough_expiry"),
    ("New HCPCS code",                "cms_new_code"),
    ("HCPCS code effective",          "cms_new_code"),
    ("HCPCS code TERMINATING",        "cms_code_terminating"),
    ("CHMP OPINION",                  "ema_chmp_opinion"),
    ("EU marketing authorisation REFUSED", "ema_refusal"),
    ("EU marketing authorisation",    "ema_authorisation"),
    ("EU application WITHDRAWN",      "ema_withdrawal"),
    ("EC decision",                   "ema_ec_decision"),
    ("Approval action",               "fda_approval"),
    ("510(k)",                        "fda_510k"),
]

_NOVELTY_RX = re.compile(r"\b(NEW|recent|established)\s*\([\d.]+y since approval\)")


def classify(text: str) -> str:
    t = (text or "").strip()
    for prefix, kind in _PREFIX_TYPES:
        if t.startswith(prefix):
            if kind is None:      # phase transition — which direction?
                return ("phase_transition_ph3"
                        if re.search(r"→\s*Ph3|->\s*Ph3", t) else "phase_transition")
            return kind
    return "trial_update"


def segment_of(universe_name) -> str:
    if not universe_name:
        return "none"
    if universe_name in LARGE_PHARMA:
        return "large"
    if universe_name in SUPPLY_CHAIN:
        return "cdmo"
    return "pure"


def novelty_of(text: str) -> str:
    m = _NOVELTY_RX.search(text or "")
    if m:
        return m.group(1)
    return "unknown" if "novelty unknown" in (text or "") else ""


def score(record, universe_name=None, today=None) -> dict:
    """Deterministic ordering score plus the terms that produced it."""
    today = today or dt.date.today()
    kind = classify(record["text"])
    base, half_life = CATALYST_WEIGHTS.get(kind, CATALYST_WEIGHTS["trial_update"])

    try:
        age = max(0, (today - dt.date.fromisoformat(record["date"])).days)
    except (ValueError, TypeError):
        age = 0

    decay = 2 ** (-age / half_life)
    seg = segment_of(universe_name)
    weight = SEGMENT_WEIGHTS.get(seg, 1.0)
    nov = novelty_of(record["text"])
    nov_w = NOVELTY_WEIGHTS.get(nov, 1.0) if nov else 1.0

    return {
        "score": base * decay * weight * nov_w,
        "kind": kind, "base": base, "half_life": half_life,
        "age": age, "decay": decay, "segment": seg, "weight": weight,
        "novelty": nov, "novelty_weight": nov_w,
    }
