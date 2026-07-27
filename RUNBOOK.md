# Runbook — driving Claude Code through the build

Work top to bottom. Each task is a self-contained prompt you can paste into the
Claude Code session. Don't skip ahead — the live-test phase surfaces the field-path
fixes that everything else depends on. Approve file edits as they come; on the iPad,
glance at *which file* is being touched even if you skim the diff.

Legend: 🟢 do now · 🔵 after live data works · ⚪ operational polish

---

## PHASE 0 — Orientation

**0.1 🟢 Load context**
> Read CLAUDE.md in full, then config.py and pullers.py. Don't change anything yet.
> In two sentences, tell me what this tool is for and what the scoring philosophy is,
> so I know you've got it.

---

## PHASE 1 — Get the existing pipeline running live

The pullers were written but never hit the live APIs. This phase is where the
field-path guesses get corrected against reality. One source at a time.

**1.1 🟢 Dependencies + first source**
> Run `pip install requests`. Then run ONLY the ClinicalTrials puller:
> `python run.py clinicaltrials`. It's untested against the live endpoint, so when
> field paths break, diagnose and patch pullers.py yourself, re-run, and tell me
> plainly what was wrong — don't hand me a stack trace. Show me the first 10 lines
> of feed.txt when it works.

**1.2 🟢 openFDA — the fussy one**
> Now run `python run.py fda_510k`, then `python run.py fda_approvals`. openFDA's
> query syntax is the part most likely to be wrong — the search parameter format and
> date filters especially. Patch and re-run until both return cleanly (zero results
> is a valid outcome; a 404 from openFDA means no matches, not an error). Report what
> you changed.

**1.3 🟢 Full run + sanity check**
> Run all sources together: `python run.py`. Then show me feed.txt and tell me:
> how many records, how many matched the universe watchlist, and the spread across
> sources. Flag anything that looks like a parsing artifact rather than a real record.

---

## PHASE 2 — The highest-value signal

**2.1 🔵 Phase-transition detection**
> Right now the ClinicalTrials puller reports each trial's *current* phase on every
> update. I want it to detect *transitions* — a trial moving Ph1→Ph2, Ph2→Ph3, or
> into "Completed"/"Terminated". Implement this:
> - In store.py, keep the last-seen phase and status per NCT ID (add columns or a
>   small companion table; the NCT ID is the key).
> - In pullers.py, on each run, compare the incoming phase/status against the stored
>   prior value for that NCT. If it changed, emit a record whose text names the
>   transition explicitly, e.g. "PHASE TRANSITION: Ph2 → Ph3".
> - A first-seen trial with no prior record is NOT a transition — don't emit one.
> - Then update the stored value.
> Test it by running twice with a manually edited stored phase so I can see a
> transition fire. Walk me through how you verified it.

---

## PHASE 3 — New sources

**3.1 🔵 CMS reimbursement puller**
> Add a CMS puller. There's no clean API, so this scrapes. Target, in priority order:
> new/updated HCPCS codes for radiopharmaceuticals, OPPS transitional pass-through
> payment status, and NTAP applications/decisions. Reimbursement is disproportionately
> important in radiopharma — often it gates commercial viability more than efficacy —
> so this is a high-value source. Find the current CMS pages for these, confirm the
> URLs resolve, and parse defensively. Normalise output to the same record shape as
> the other pullers and wire it into run.py and PULLERS. Show me sample output.

**3.2 🔵 EMA CHMP puller**
> Add an EMA CHMP puller. CHMP publishes monthly meeting agendas and opinions on a
> predictable cycle. Pull new positive/negative opinions and any radiopharma-relevant
> agenda items, filtered against the discovery terms in config.py. Same record shape,
> wired into run.py. Defensive parsing — the page structure will change on you.

**3.3 ⚪ Funding feed (lowest priority — noisy)**
> Add a funding-round source. There's no free structured feed, so combine press-release
> RSS with targeted news search for the universe names. Expect noise; bias hard toward
> precision. If this turns out to add more noise than signal, tell me and we'll drop it.

---

## PHASE 4 — Precision hardening (do once real data is flowing)

**4.1 🔵 Extend the matcher from real misses**
> Here are sponsor/applicant strings from the last run that should have matched a
> universe name but didn't: [PASTE]. Add safe aliases to `_SHORT_ALIASES` in pullers.py
> for each. Keep the precision rule: no generic short forms that could mislabel. Re-run
> the matcher tests.

**4.2 🔵 Tune scoring weights**
> Now that I've seen real volume, the mix is off — [describe: too much of X, not enough
> Y]. The scoring taxonomy lives in the React screening tool, not here, but adjust the
> half-lives and base weights there per my notes: [PASTE].

---

## PHASE 5 — Make it run itself

**5.1 ⚪ Weekly automation**
> Set this up to run weekly without me. Pick the right mechanism for my machine
> (cron / launchd / a GitHub Action if I'm running it in a repo) and wire it so the
> run writes feed.txt and, ideally, emails or files the output. Walk me through what
> you set up and how to turn it off.

**5.2 ⚪ Digest polish**
> Improve the feed.txt output into a readable weekly digest: grouped by source,
> universe-matched leads flagged at the top, newest first. Keep the pipe-delimited
> raw feed too, since the screening tool consumes that format.

---

## Notes to self
- Scoring/ranking happens in the React screening artifact, not this pipeline. feed.txt
  is the handoff.
- The universe watchlist boosts known names; the discovery *terms* are what surface
  new companies. To widen coverage, add terms before adding names.
- Every discovery term is one API call per run. If runs get slow, trim the long tail.
