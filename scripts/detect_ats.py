"""
Auto-detect each watchlist company's real ATS by scraping their careers_url
and pattern-matching known ATS URL signatures in the page source. Zero API
cost — pure requests + regex. Run this whenever you update a careers_url,
or periodically (companies migrate ATS platforms occasionally).

This replaces guessing board_token from search snippets, which was the
actual root cause of the wrong-token problem. Direct detection >> guessing.

Usage: python3 detect_ats.py            # scan + report, don't write
       python3 detect_ats.py --apply    # scan + write updates to watchlist.csv
"""
import csv
import re
import sys
import time

import requests

from common import WATCHLIST_PATH

TIMEOUT = 15
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}


def categorize_error(exc):
    """Distinguish 'blocked by WAF' from 'domain doesn't exist' from other failures —
    these need different human responses, not the same generic fetch_failed bucket."""
    msg = str(exc)
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else "?"
        if status in (403, 401):
            return f"blocked_by_waf (HTTP {status}) — site fingerprints scripted requests, likely unfixable without a real browser session"
        return f"http_error_{status}"
    if "NameResolutionError" in msg or "nodename nor servname" in msg:
        return "dead_domain — URL doesn't resolve, check if company site moved or company no longer exists at this URL"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout — site slow or unresponsive, may work on retry"
    return f"fetch_failed: {msg}"

# Each pattern captures the board token in group 1. Ordered by how confident
# a match is — first match wins.
ATS_PATTERNS = [
    ("greenhouse", r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-zA-Z0-9_-]+)"),
    ("greenhouse", r"greenhouse\.io/embed/job_app\?for=([a-zA-Z0-9_-]+)"),
    ("lever", r"jobs\.lever\.co/([a-zA-Z0-9_-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)"),
    ("smartrecruiters", r"careers\.smartrecruiters\.com/([a-zA-Z0-9_-]+)"),
    ("workable", r"apply\.workable\.com/([a-zA-Z0-9_-]+)"),
    ("recruitee", r"([a-zA-Z0-9_-]+)\.recruitee\.com"),
    # These have no public API but detecting them still stops "custom" guesswork
    ("workday", r"([a-zA-Z0-9_-]+)\.wd\d?\.myworkdayjobs\.com"),
    ("successfactors", r"([a-zA-Z0-9_-]+)\.successfactors\.(?:com|eu)"),
    ("icims", r"([a-zA-Z0-9_-]+)\.icims\.com"),
    ("bamboohr", r"([a-zA-Z0-9_-]+)\.bamboohr\.com"),
]

# ats_type values with a working public JSON API in this pipeline today
API_SUPPORTED = {"greenhouse", "lever", "ashby"}


def detect(careers_url):
    try:
        resp = requests.get(careers_url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        return None, None, categorize_error(e)

    text = resp.text
    for ats_name, pattern in ATS_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return ats_name, m.group(1), None

    return None, None, "no known ATS signature found"


def main():
    apply_changes = "--apply" in sys.argv

    with open(WATCHLIST_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    fieldnames = list(rows[0].keys())

    updated, unchanged, unresolved = 0, 0, 0
    for row in rows:
        if row["ats_type"] in API_SUPPORTED and row.get("board_token"):
            unchanged += 1
            continue  # already confirmed working, don't re-check every run

        ats_name, token, err = detect(row["careers_url"])
        time.sleep(0.5)

        if ats_name:
            confidence = "API-fetchable" if ats_name in API_SUPPORTED else "no public API, but now correctly labeled"
            print(f"  [detected] {row['company']}: {ats_name} (token={token}) — {confidence}")
            if apply_changes:
                row["ats_type"] = ats_name
                row["board_token"] = token if ats_name in API_SUPPORTED else ""
                row["notes"] = f"auto-detected via detect_ats.py"
            updated += 1
        else:
            print(f"  [unresolved] {row['company']}: {err}")
            unresolved += 1

    print(f"\nDetected: {updated}  Already confirmed: {unchanged}  Unresolved: {unresolved}")

    if apply_changes:
        with open(WATCHLIST_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        print(f"Written to {WATCHLIST_PATH}")
    else:
        print("Dry run — rerun with --apply to write these changes to watchlist.csv")


if __name__ == "__main__":
    main()
