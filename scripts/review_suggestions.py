"""
Review today's watchlist suggestions (from web_search_fallback.py's
discover_beyond_watchlist) and interactively add accepted ones to watchlist.csv.

Run this after web_search_fallback.py, whenever you feel like growing the list.
Not part of the daily pipeline — deliberately a manual gate, so junk suggestions
don't silently bloat your watchlist and start burning API calls on their own.
"""
import csv
import glob
import json
import sys

from common import REPO_ROOT, WATCHLIST_PATH, today_str


def load_existing_companies():
    with open(WATCHLIST_PATH, newline="", encoding="utf-8") as f:
        return {row["company"].lower() for row in csv.DictReader(f)}


def load_todays_suggestions():
    path = REPO_ROOT / "data" / "scored" / today_str() / "watchlist_suggestions.json"
    if not path.exists():
        # fall back to most recent suggestions file if today's doesn't exist
        candidates = sorted(glob.glob(str(REPO_ROOT / "data" / "scored" / "*" / "watchlist_suggestions.json")))
        if not candidates:
            return []
        path = candidates[-1]
        print(f"No suggestions for today — using most recent: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def append_to_watchlist(company, careers_url, category, notes):
    with open(WATCHLIST_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([company, "custom", "", careers_url, category or "tech",
                    "canada_wide", notes or "added via review_suggestions.py"])


def main():
    suggestions = load_todays_suggestions()
    if not suggestions:
        print("No suggestions to review.")
        return

    existing = load_existing_companies()
    added, skipped = 0, 0

    for s in suggestions:
        name = s.get("company", "").strip()
        if not name or name.lower() in existing:
            continue

        print(f"\n--- {name} ---")
        print(f"  careers_url: {s.get('careers_url', 'n/a')}")
        print(f"  reason: {s.get('reason', 'n/a')}")
        choice = input("  Add to watchlist? [y/N/q to quit]: ").strip().lower()

        if choice == "q":
            break
        if choice == "y":
            append_to_watchlist(name, s.get("careers_url", ""), s.get("category", ""),
                                 s.get("reason", ""))
            existing.add(name.lower())
            added += 1
            print(f"  added.")
        else:
            skipped += 1

    print(f"\nAdded {added}, skipped {skipped}.")
    if added:
        print("New rows have ats_type=custom (unverified) — they'll route through "
              "web_search_fallback.py until you confirm a real board_token, same as "
              "any manually-added company.")


if __name__ == "__main__":
    sys.exit(main())
