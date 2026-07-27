"""Weekly run: pull every source, dedupe into SQLite, write the unscreened feed.

    python run.py                 # all sources
    python run.py clinicaltrials  # one source
    python run.py --days 30       # widen the window

Output goes to feed.txt in the pipe-delimited format the screening tool reads.
Paste it in, screen it, and the ranking happens there.
"""

import sys

from config import LOOKBACK_DAYS
from pullers import PULLERS
from store import insert_many, mark_screened, to_feed_lines, unscreened


def main(argv):
    days = LOOKBACK_DAYS
    if "--days" in argv:
        i = argv.index("--days")
        days = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]

    names = [a for a in argv if not a.startswith("-")] or list(PULLERS)

    total = 0
    for name in names:
        if name not in PULLERS:
            print(f"unknown source: {name}")
            continue
        print(f"pulling {name} (last {days} days)...")
        try:
            records = PULLERS[name](days=days)
        except TypeError:
            records = PULLERS[name]()
        new = insert_many(records)
        total += new
        print(f"  {len(records)} fetched, {new} new")

    pending = unscreened()
    if not pending:
        print("\nnothing new to screen")
        return

    with open("feed.txt", "w") as f:
        f.write(to_feed_lines(pending))

    hits = sum(1 for r in pending if r["in_universe"])
    print(f"\n{total} new records this run")
    print(f"{len(pending)} awaiting screening ({hits} match the universe)")
    print("written to feed.txt")

    if "--mark" in sys.argv:
        mark_screened([r["hash"] for r in pending])
        print("marked as screened")


if __name__ == "__main__":
    main(sys.argv[1:])
