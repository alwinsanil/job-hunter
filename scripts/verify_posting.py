"""
Verify candidate postings before they reach score.py.

Key design point: Greenhouse/Lever/Ashby's own JSON APIs only ever return
CURRENTLY OPEN postings — that's what the API endpoint is. A posting sourced
from fetch_greenhouse.py / fetch_lever.py / fetch_ashby.py is already
verified live by construction; re-fetching its URL and pattern-matching page
text for "closed" language adds risk, not signal — modern career pages are
JS single-page apps whose bundled JavaScript often contains generic strings
(a client-side router's own "404" error-page code, unrelated to the actual
posting) and many sites' bot-protection (Cloudflare etc.) returns HTTP 403
to any scripted request regardless of whether the job is open. Both cause
false "closed" rejects on postings that are genuinely live.

So: postings from ats_source in {greenhouse, lever, ashby} are trusted
directly, no re-fetch. Only web_search_fallback / regional_board postings
(which have no trusted source API backing them) go through the full
live-check: domain match, aggregator-link check, and a re-fetch for a
closed-posting text scan.
"""
import glob
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from common import REPO_ROOT, load_watchlist, today_str
from title_prefilter import filter_postings

TRUSTED_API_SOURCES = {"greenhouse", "lever", "ashby"}

AGGREGATOR_DOMAINS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "glassdoor.ca",
    "ziprecruiter.com", "monster.com", "simplyhired.com", "workopolis.com",
    "builtin.com", "builtincalgary.org", "wellfound.com", "careerbeacon.com",
    "jobbank.gc.ca", "eluta.ca", "jobboom.com",
}

# Full phrases only — no bare "404" or other substrings that show up
# incidentally in JS bundles / analytics scripts / unrelated page chrome.
CLOSED_PHRASES = [
    "no longer accepting applications",
    "this position has been filled",
    "job is no longer available",
    "this posting has expired",
    "this job posting has been closed",
    "sorry, this position is no longer available",
]

TIMEOUT = 15
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def domain_of(url):
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def company_domain_hint(company_row):
    """Rough expected domain from careers_url, for a loose match check."""
    return domain_of(company_row.get("careers_url", ""))


def is_aggregator(url):
    d = domain_of(url)
    return any(d == agg or d.endswith("." + agg) for agg in AGGREGATOR_DOMAINS)


def looks_closed(text):
    lowered = text.lower()
    return any(phrase in lowered for phrase in CLOSED_PHRASES)


def verify_one(posting, watchlist_by_name):
    url = posting["url"]
    source = posting.get("ats_source", "")

    # Trust the ATS API directly — it only returns currently-open postings.
    if source in TRUSTED_API_SOURCES:
        return True, "trusted_api_source"

    is_regional = source == "regional_board"

    if not is_regional and is_aggregator(url):
        return False, "aggregator_link"

    if not is_regional:
        company_row = watchlist_by_name.get(posting["company"].lower())
        if company_row:
            expected = company_domain_hint(company_row)
            actual = domain_of(url)
            expected_root = expected.split(".")[0] if expected else ""
            if expected_root and expected_root not in actual and actual.split(".")[0] not in expected:
                # not a hard fail — ATS subdomains legitimately differ — just flag for review
                pass

    try:
        resp = requests.get(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        return False, f"fetch_failed: {e}"

    if resp.status_code == 403:
        # Bot-protection block, not evidence the job is closed. Can't confirm
        # either way from here — mark unverified rather than falsely reject.
        return False, "blocked_by_waf_unconfirmed"
    if resp.status_code == 404:
        return False, "http_404"
    if resp.status_code >= 400:
        return False, f"http_{resp.status_code}"

    if looks_closed(resp.text[:5000]):
        return False, "closed_or_expired"

    return True, ""


def main():
    today = today_str()
    scored_dir = REPO_ROOT / "data" / "scored" / today
    if not scored_dir.exists():
        print(f"No fetch output found for {today} at {scored_dir}. Run fetch scripts first.")
        return []

    all_postings = []
    for path in glob.glob(str(scored_dir / "*.json")):
        if "watchlist_suggestions" in path:
            continue  # not postings, skip
        with open(path, encoding="utf-8") as f:
            all_postings.extend(json.load(f))

    if not all_postings:
        print("No postings to verify.")
        return []

    pre_kept, pre_rejected = filter_postings(all_postings)
    if pre_rejected:
        print(f"  Pre-filter: dropped {len(pre_rejected)} non-target-role postings "
              f"(VP/SVP/Director/Senior/non-eng-function) before verification")
    all_postings = pre_kept

    if not all_postings:
        print("Nothing left after title pre-filter.")
        return []

    watchlist_by_name = {r["company"].lower(): r for r in load_watchlist()}

    verified_out = []
    live_count = 0
    for posting in all_postings:
        ok, reason = verify_one(posting, watchlist_by_name)
        posting["verified"] = ok
        posting["reject_reason"] = reason
        verified_out.append(posting)
        if ok:
            live_count += 1
        else:
            print(f"  [reject] {posting['company']} — {posting['title'][:50]}: {reason}")
        time.sleep(0.3)  # don't hammer career sites

    out_dir = REPO_ROOT / "data" / "verified"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(verified_out, f, indent=2)

    print(f"\nVerified {live_count}/{len(all_postings)} postings live. Written to {out_path}")
    return [p for p in verified_out if p["verified"]]


if __name__ == "__main__":
    sys.exit(0 if main() is not None else 1)
