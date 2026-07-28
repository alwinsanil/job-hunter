"""
Fetch postings for companies with ats_type in {workday, custom} — i.e. everything
NOT on Greenhouse/Lever/Ashby. That's most of the watchlist (all banks, all
consulting firms, most Halifax-local companies).

There's no public API for these, so this script asks Claude (via the Messages
API + web_search tool) to search each company's OWN careers site and return
live SWE/DevOps/SRE postings as structured JSON. verify_posting.py re-checks
every result afterward — this script's job is discovery, not verification.

Requires: ANTHROPIC_API_KEY env var.
Also handles "beyond watchlist" discovery — open web search for roles at
companies NOT on the list, with a suggestion flag for watchlist additions.
"""
import json
import os
import sys
import time

import requests

from common import load_watchlist, make_posting, write_daily_raw, dedupe_new, append_seen

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"
TARGET_TITLES = ["Software Engineer", "Software Developer", "DevOps Engineer",
                  "Site Reliability Engineer", "SRE", "Platform Engineer"]

SYSTEM_PROMPT = """You search a single company's official careers site for live \
job postings matching a target title list. Only return postings that:
1. Are hosted on the company's own domain or their named ATS subdomain (e.g. \
company.wd3.myworkdayjobs.com) — never aggregator links (LinkedIn, Indeed, Glassdoor).
2. Are currently live (not expired, not a "no longer accepting applications" page).
3. Roughly match one of the target titles — new grad / early career SWE, DevOps, \
or SRE roles. Skip senior/staff/principal/director-level titles.

Respond ONLY with a JSON array, no prose, no markdown fences. Each item:
{"title": "...", "url": "...", "location": "..."}
If nothing matches, respond with an empty array: []
"""


def call_claude(user_prompt, max_tokens=1500):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    resp = requests.post(
        API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()

    # Pull the final text block(s); web_search runs may interleave tool_use/tool_result
    text_parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    raw_text = "\n".join(text_parts).strip()
    raw_text = raw_text.strip("`").replace("json\n", "", 1) if raw_text.startswith("```") else raw_text

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        print(f"  [warn] couldn't parse JSON from model response, skipping. Raw: {raw_text[:200]}")
        return []


def fetch_company(company_row, retries=1):
    prompt = (
        f"Search {company_row['careers_url']} for live postings at "
        f"{company_row['company']} matching: {', '.join(TARGET_TITLES)}. "
        f"Company category: {company_row.get('category', 'n/a')}."
    )
    for attempt in range(retries + 1):
        try:
            results = call_claude(prompt)
            break
        except (requests.RequestException, RuntimeError) as e:
            if attempt == retries:
                print(f"  [error] {company_row['company']}: {e}")
                return []
            time.sleep(2)

    postings = []
    for item in results:
        if not item.get("url") or not item.get("title"):
            continue
        postings.append(make_posting(
            company=company_row["company"],
            title=item["title"].strip(),
            url=item["url"].strip(),
            location=item.get("location", "unknown"),
            ats_source="web_search",
            raw={"careers_url": company_row["careers_url"]},
        ))
    return postings


def discover_beyond_watchlist(watchlist_companies):
    """One broader search for roles/companies not already on the watchlist.
    Returns (postings, suggested_new_companies)."""
    known_names = ", ".join(r["company"] for r in watchlist_companies)
    prompt = (
        "Search for new-grad / early-career Software Engineer, DevOps Engineer, "
        "or SRE job postings in Canada (remote or Halifax NS preferred, open to "
        "elsewhere in Canada) at companies NOT in this list: "
        f"{known_names}. Respond ONLY with a JSON object: "
        '{"postings": [{"company": "...", "title": "...", "url": "...", "location": "..."}], '
        '"suggested_companies": [{"company": "...", "careers_url": "...", "reason": "..."}]}'
    )
    try:
        raw = call_claude(prompt, max_tokens=2500)
    except (requests.RequestException, RuntimeError) as e:
        print(f"  [error] beyond-watchlist discovery: {e}")
        return [], []

    if isinstance(raw, dict):
        postings_raw = raw.get("postings", [])
        suggested = raw.get("suggested_companies", [])
    else:
        postings_raw, suggested = [], []

    postings = []
    for item in postings_raw:
        if not item.get("url") or not item.get("title") or not item.get("company"):
            continue
        postings.append(make_posting(
            company=item["company"],
            title=item["title"].strip(),
            url=item["url"].strip(),
            location=item.get("location", "unknown"),
            ats_source="web_search_discovery",
            raw={},
        ))
    return postings, suggested


def main():
    companies = load_watchlist() or []
    fallback_companies = [r for r in companies if r["ats_type"] in ("workday", "custom")]
    if not fallback_companies:
        print("No workday/custom companies in watchlist.csv")
        return

    all_postings = []
    for row in fallback_companies:
        postings = fetch_company(row)
        print(f"  {row['company']}: {len(postings)} candidate postings (unverified)")
        all_postings.extend(postings)
        time.sleep(1)  # be polite, these are real API calls with cost

    beyond_postings, suggested = discover_beyond_watchlist(companies)
    print(f"  Beyond watchlist: {len(beyond_postings)} candidate postings, "
          f"{len(suggested)} companies suggested for watchlist")
    all_postings.extend(beyond_postings)

    if suggested:
        suggestions_path = write_daily_raw(suggested, "watchlist_suggestions")
        print(f"  Suggestions written to {suggestions_path} — review and add manually")

    new, old = dedupe_new(all_postings)
    write_daily_raw(all_postings, "web_search")
    append_seen(new)

    print(f"\nWeb search fallback: {len(all_postings)} total, {len(new)} new "
          f"(UNVERIFIED — run verify_posting.py before scoring)")
    return new


if __name__ == "__main__":
    sys.exit(0 if main() is not None else 1)
