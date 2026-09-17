# Radiopharma catalyst sourcing

A weekly sourcing tool for healthcare buy-side investing, focused on
radiopharmaceuticals. It pulls catalyst events from public sources, dedupes
them into a local database, and ranks them with a transparent, rules-based
score — so a small number of high-signal leads surface each Monday instead of
a firehose.

**Design principle: precision over recall.** Five leads worth opening beats
forty that get ignored. Every choice below — the curated watchlist, the
signal-gating on trial updates, the short lookback windows — biases toward
fewer, better results.

See [`examples/digest-2026-07-28.txt`](examples/digest-2026-07-28.txt) for
what a real weekly output looks like.

## What it watches

| Source | What counts as a catalyst |
|---|---|
| ClinicalTrials.gov v2 | Phase transitions (Ph2 → Ph3), results posted, trials stopped *with the stated reason*, primary completion just passed (readout due). A trial that merely restates its current phase is **not** emitted. |
| openFDA | Drug approvals (drugs@FDA) and 510(k) clearances for radiopharma products. |
| CMS | New HCPCS codes and OPPS transitional pass-through status. Reimbursement often gates commercial viability in radiopharma more than efficacy does, so this is scraped despite there being no clean API. |
| EMA | CHMP opinions, authorisations, refusals and withdrawals from the monthly medicines report. |
| Funding | Curated Google News RSS queries for financing rounds. Noisiest source; short window and hard gating by design. |

Discovery is driven by ~35 intervention/modality/target terms (Lu-177, Ac-225,
Pb-212, PSMA, FAP, radioligand therapy, targeted alpha therapy, named
diagnostic tracers …), not by company name — so sponsors nobody has heard of
still surface. A curated ~80-name universe watchlist then boosts records that
match a known company, using whole-phrase word-boundary matching with an alias
table for the awkward cases (ITM, ABX vs ABX-CRO).

## How ranking works

Scoring is **deterministic and auditable**:

```
score = base × 2^(−age / half_life) × segment_weight × novelty_weight
```

- `base` and `half_life` come from the catalyst type (a Ph3 transition is
  worth 100 with a 60-day half-life; a routine trial update is worth 10).
- `segment_weight` reflects materiality to the issuer, not familiarity: the
  same Pluvicto update that barely moves Novartis (×0.8) is a real event for a
  pure-play (×1.6).
- `novelty_weight` discounts reimbursement events on long-established products.

Every row in the digest prints its own breakdown, so any ranking can be
explained from the numbers alone. Thesis fit — the judgement call — is applied
in a separate screening tool downstream, deliberately kept out of this
pipeline. No LLM call exists anywhere in this code.

## Outputs

- `digest.txt` — human-readable, ordered by score, one block per lead with its
  score breakdown and a source link. The thing to actually read.
- `feed.txt` — pipe-delimited `date | source | text`, in source order. The
  machine contract with the screening tool.

Records are hashed (SHA1 of content) into SQLite so the same trial update,
which reappears in every weekly pull while it sits in the lookback window, is
only screened once.

## Running it

No API keys required — every source is a free public endpoint.

```bash
pip install -r requirements.txt
python run.py                 # all sources
python run.py clinicaltrials  # one source, for debugging
python run.py --days 30       # widen the window
```

`run_weekly.sh` and the launchd plist template wire it to run automatically
each week on macOS.

## Layout

| File | Role |
|---|---|
| `config.py` | Universe watchlist, discovery terms, catalyst weights, thresholds. Where precision comes from. |
| `pullers.py` | One function per source, each returning normalised records. Parses defensively — a malformed record is skipped, never crashes the run. |
| `store.py` | SQLite persistence, content-hash dedupe, prior-phase tracking for transition detection. |
| `scoring.py` | The deterministic score, from constants in `config.py`. |
| `run.py` | Pull → dedupe → score → write `feed.txt` and `digest.txt`. |
| `RUNBOOK.md` | The sequenced build plan the project was driven from. |

## How it was built

The Python was written with Claude Code, working from `CLAUDE.md` and
`RUNBOOK.md` in this repo. I owned the problem framing, source selection,
universe curation, the precision-over-recall stance and the scoring
philosophy; the agent wrote and live-debugged the code against the real
endpoints.
