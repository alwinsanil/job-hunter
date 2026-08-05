"""
Fetch live postings from Greenhouse for every watchlist company with ats_type=greenhouse.

Greenhouse public API: no auth needed.
  GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

board_token is usually the company's careers-page slug, e.g.
  https://boards.greenhouse.io/shopify  ->  board_token = "shopify"
If a company's slug doesn't match its watchlist name, add a `careers_url` override
row or a dedicated `board_token` column to watchlist.csv later.
"""
import sys
import time
import requests

from common import load_watchlist, make_posting, write_daily_raw, dedupe_new, append_seen

API_BASE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
TIMEOUT = 15
RETRY = 2


def slugify(company_name):
    """Best-effort guess at a Greenhouse board token from company name."""
    return company_name.strip().lower().replace(" ", "").replace(".", "").replace(",", "")


def fetch_company(company_row):
    token = company_row.get("board_token") or slugify(company_row["company"])
    url = API_BASE.format(token=token)

    for attempt in range(RETRY + 1):
        try:
            resp = requests.get(url, params={"content": "true"}, timeout=TIMEOUT)
            if resp.status_code == 404:
                print(f"  [skip] {company_row['company']}: no Greenhouse board at token '{token}'")
                return []
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.RequestException as e:
            if attempt == RETRY:
                print(f"  [error] {company_row['company']}: {e}")
                return []
            time.sleep(1.5 * (attempt + 1))

    postings = []
    for job in data.get("jobs", []):
        # Same risk pattern found in Ashby's fetcher: Greenhouse also splits
        # a single display "location.name" field from a separate "offices"
        # array listing every office/region a job is actually posted to.
        # Reading only "location.name" can miss real eligible regions.
        primary = (job.get("location") or {}).get("name", "unknown")
        office_names = [o.get("name", "") for o in job.get("offices", []) if o.get("name")]
        # dedupe while preserving order, primary first
        seen_locs = []
        for loc in [primary] + office_names:
            loc = loc.strip()
            if loc and loc not in seen_locs:
                seen_locs.append(loc)
        location = "; ".join(seen_locs) if len(seen_locs) > 1 else primary

        postings.append(make_posting(
            company=company_row["company"],
            title=job.get("title", "").strip(),
            url=job.get("absolute_url", ""),
            location=location,
            ats_source="greenhouse",
            raw={"id": job.get("id"), "updated_at": job.get("updated_at"),
                 "jd_html": job.get("content", "")},  # full JD, already in this response
        ))
    return postings


def main():
    companies = load_watchlist(ats_filter="greenhouse")
    if not companies:
        print("No greenhouse-type companies in watchlist.csv")
        return

    all_postings = []
    for row in companies:
        postings = fetch_company(row)
        print(f"  {row['company']}: {len(postings)} live postings")
        all_postings.extend(postings)

    new, old = dedupe_new(all_postings)
    write_daily_raw(new, "greenhouse")
    append_seen(new)

    print(f"\nGreenhouse: {len(all_postings)} total, {len(new)} new, {len(old)} already seen")
    return new


if __name__ == "__main__":
    sys.exit(0 if main() is not None else 1)