# Radiopharma catalyst sourcing agent

## What this is
A weekly sourcing tool for a healthcare buy-side investor. It pulls catalyst
events from public APIs, dedupes them into SQLite, and emits a feed that a
separate classifier/scoring layer ranks. The goal is **precision, not recall**:
five leads worth opening beats forty that get ignored. False positives are
costly in an investment workflow — bias every decision toward fewer, higher-signal
results.

## The person you're working with
Directs the work and reviews it; does not want to hand-write Python. Explain what
you changed and why in plain terms. When an API call fails, diagnose and patch it
yourself, then report what was wrong — don't hand back a stack trace to debug.

## Architecture
- `config.py` — universe list, search terms, thresholds. **The universe list is
  where precision comes from.** Matching a record to a named company is a strong
  signal. Treat expanding it as high-value, not busywork.
- `store.py` — SQLite persistence. Dedupe is on a SHA1 content hash. This matters:
  the same trial update reappears in every weekly pull while it sits in the lookback
  window, so without hashing we re-screen (and re-pay for) the same records.
- `pullers.py` — one function per source, each returning normalised records. Parse
  defensively: a malformed record must be skipped, never crash the run.
- `run.py` — orchestrates pull → dedupe → write `feed.txt`.

## Scoring philosophy (do not violate)
Scoring is **deterministic and auditable**: `base × 2^(−age / half_life) × fit × weight`.
Only the classification and thesis-fit come from the model; the score itself is
rules-based so any ranking can be explained. Do not move scoring logic into an LLM
call — auditability is the point.

## Data sources
Live: ClinicalTrials.gov v2, openFDA (510(k) + drugsfda).
Not yet built: CMS (HCPCS / pass-through / NTAP — no clean API, needs scraping),
EMA CHMP monthly agendas, funding rounds (no free source; awkward).

## Current state (as of last edit)
- Universe watchlist: 71 curated names across large pharma, acquired subsidiaries
  (kept as match targets), commercial mid-caps, earlier-stage therapeutics,
  diagnostics, isotope supply/production, CDMO/CRO, and radiopharma-native chelators.
- Discovery terms: 24 intervention/modality/target terms. This is the discovery net —
  widening it catches new sponsors; the watchlist only boosts known ones.
- Matcher: whole-phrase, word-boundary matching with a safe-alias dict in pullers.py
  (`_SHORT_ALIASES`). Fixed the old first-token collisions (ABX vs ABX-CRO) and
  short-name drops (ITM). The alias dict is a LIVING thing — extend it as real
  sponsor strings appear that should have matched but didn't.

## Known first-run issues
The pullers were written but **not tested against live endpoints**. Expect field
paths to need adjustment on the first real run, especially openFDA's query syntax.
Run one source at a time first (`python run.py clinicaltrials`) so failures localise.

## Scoring lives elsewhere
Classification and thesis-fit scoring run in a separate React tool (the screening
artifact), not in this Python. This pipeline's job ends at writing `feed.txt`. Keep
it that way — don't add LLM scoring here.

## Highest-value next steps (see RUNBOOK.md for sequenced tasks)
1. **Phase-transition detection.** The ClinicalTrials puller reports current phase
   per update. Turning that into "moved Ph2 → Ph3" means storing prior phase per
   NCT ID and diffing on each run. Single most valuable signal in the set.
2. CMS puller (HCPCS / pass-through / NTAP — no clean API, needs scraping).
3. EMA CHMP monthly agenda + opinion parser.
4. Funding feed (press-release RSS + targeted news; noisy, lowest priority).

## How to run
```
pip install requests
python run.py                 # all sources
python run.py clinicaltrials  # one source, for debugging
python run.py --days 30       # widen the window
```
Output is `feed.txt`, pipe-delimited: `date | source | text`.
