"""
Regional job boards (Digital Nova Scotia, WorkPEI, JobsPEI, CareerBeacon) list
postings from many employers on one domain — that's the whole point of the
site, not aggregator noise to filter out. Different model from watchlist.csv:
one row per BOARD, not per company, and verify_posting.py treats postings
sourced here differently (skips the single-company-domain match check).

No public JSON API on any of these — uses Claude + web_search, restricted to
each board's own domain via a site: query. Costs API tokens, same tier as
web_search_fallback.py. Run it the same cadence.

Requires: ANTHROPIC_API_KEY env var.
"""
import csv
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from common import REPO_ROOT, make_posting, write_daily_raw, dedupe_new, append_seen

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"
BOARDS_PATH = REPO_ROOT / "regional_boards.csv"
TARGET_TITLES = ["Software Engineer", "Software Developer", "DevOps Engineer",
                  "Site Reliability Engineer", "SRE", "Platform Engineer"]

SYSTEM_PROMPT = """You search ONE specific job board website (via a site: \
restricted search) for live postings matching a target title list. Only \
return postings actually hosted on or linked directly from that board \
domain, currently live (not expired). Respond ONLY with a JSON array, no \
prose: [{"company": "...", "title": "...", "url": "...", "location": "..."}]
If nothing matches, respond with an empty array: []
"""


def call_claude(prompt):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    resp = requests.post(
        API_URL,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                  "content-type": "application/json"},
        json={
            "model": MODEL, "max_tokens": 2000, "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    text = "\n".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()
    if text.startswith("```"):
        text = text.strip("`").replace("json\n", "", 1)
    return json.loads(text)


def load_boards():
    with open(BOARDS_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fetch_board(board_row):
    domain = urlparse(board_row["url"]).netloc
    prompt = (
        f"site:{domain} search for live postings matching: "
        f"{', '.join(TARGET_TITLES)}. Region: {board_row['region']}."
    )
    try:
        results = call_claude(prompt)
    except (requests.RequestException, RuntimeError, json.JSONDecodeError) as e:
        print(f"  [error] {board_row['board_name']}: {e}")
        return []

    postings = []
    for item in results:
        if not item.get("url") or not item.get("title"):
            continue
        postings.append(make_posting(
            company=item.get("company", "unknown"),
            title=item["title"].strip(),
            url=item["url"].strip(),
            location=item.get("location", board_row["region"]),
            ats_source="regional_board",  # verify_posting.py checks this to skip domain-match logic
            raw={"board": board_row["board_name"]},
        ))
    return postings


def main():
    boards = load_boards()
    all_postings = []
    for row in boards:
        postings = fetch_board(row)
        print(f"  {row['board_name']}: {len(postings)} candidate postings (unverified)")
        all_postings.extend(postings)
        time.sleep(1)

    new, old = dedupe_new(all_postings)
    write_daily_raw(all_postings, "regional_boards")
    append_seen(new)

    print(f"\nRegional boards: {len(all_postings)} total, {len(new)} new "
          f"(UNVERIFIED — run verify_posting.py before scoring)")
    return new


if __name__ == "__main__":
    sys.exit(0 if main() is not None else 1)
