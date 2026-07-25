"""
Shared utilities for the job-hunt pipeline.
All fetch_*.py scripts import from here to keep output schema consistent.
"""
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = REPO_ROOT / "watchlist.csv"
DATA_DIR = REPO_ROOT / "data"
SEEN_PATH = DATA_DIR / "seen_postings.json"
VERIFIED_DIR = DATA_DIR / "verified"
FINAL_DIR = DATA_DIR / "final"
DIGEST_DIR = REPO_ROOT / "digests"

DATA_DIR.mkdir(exist_ok=True)
VERIFIED_DIR.mkdir(exist_ok=True)
FINAL_DIR.mkdir(exist_ok=True)
DIGEST_DIR.mkdir(exist_ok=True)


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_watchlist(ats_filter=None):
    """
    Read watchlist.csv, optionally filtered to a single ats_type
    (e.g. "greenhouse", "lever", "ashby", "custom", "workday").
    Returns list of dicts, one per company row.
    """
    if not WATCHLIST_PATH.exists():
        raise FileNotFoundError(f"watchlist.csv not found at {WATCHLIST_PATH}")

    rows = []
    with open(WATCHLIST_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if ats_filter and row["ats_type"].strip().lower() != ats_filter.lower():
                continue
            rows.append(row)
    return rows


def make_posting(company, title, url, location, ats_source, raw=None):
    """Normalized posting schema every fetch script should emit."""
    return {
        "company": company,
        "title": title,
        "url": url,
        "location": location,
        "ats_source": ats_source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "raw": raw or {},
    }


def load_seen():
    """URLs already scored in a previous run — used to dedupe."""
    if not SEEN_PATH.exists():
        return set()
    with open(SEEN_PATH, encoding="utf-8") as f:
        return set(json.load(f))


def save_seen(seen_urls):
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_urls), f, indent=2)


def dedupe_new(postings):
    """Split postings into (new, already_seen) based on URL."""
    seen = load_seen()
    new, old = [], []
    for p in postings:
        (new if p["url"] not in seen else old).append(p)
    return new, old


def append_seen(postings):
    seen = load_seen()
    seen.update(p["url"] for p in postings)
    save_seen(seen)


def write_daily_raw(postings, source_name):
    """Dump raw fetch output for the day — one file per source, easy to debug."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = DATA_DIR / "scored" / today
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{source_name}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(postings, f, indent=2)
    return out_path
