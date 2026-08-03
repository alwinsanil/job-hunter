"""
Fetch live postings from Lever for every watchlist company with ats_type=lever.

Lever public API: no auth needed.
  GET https://api.lever.co/v0/postings/{company_slug}?mode=json

company_slug is usually the careers-page slug, e.g.
  https://jobs.lever.co/vidyard  ->  slug = "vidyard"
"""
import sys
import time
import requests

from common import load_watchlist, make_posting, write_daily_raw, dedupe_new, append_seen

API_BASE = "https://api.lever.co/v0/postings/{slug}"
TIMEOUT = 15
RETRY = 2


def slugify(company_name):
    return company_name.strip().lower().replace(" ", "").replace(".", "").replace(",", "")


def fetch_company(company_row):
    slug = company_row.get("board_token") or slugify(company_row["company"])
    url = API_BASE.format(slug=slug)

    for attempt in range(RETRY + 1):
        try:
            resp = requests.get(url, params={"mode": "json"}, timeout=TIMEOUT)
            if resp.status_code == 404:
                print(f"  [skip] {company_row['company']}: no Lever board at slug '{slug}'")
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
    for job in data:
        categories = job.get("categories", {})
        # workplaceType is a real, documented Lever field (remote/hybrid/
        # on-site) — more reliable for remote detection than parsing the
        # word "remote" out of a location string. Captured here even though
        # classify_location() doesn't consume it yet, so it's available if
        # a real posting turns out to need it.
        workplace_type = job.get("workplaceType", "")
        postings.append(make_posting(
            company=company_row["company"],
            title=job.get("text", "").strip(),
            url=job.get("hostedUrl", ""),
            location=categories.get("location", "unknown"),
            ats_source="lever",
            raw={"id": job.get("id"), "createdAt": job.get("createdAt"),
                 "jd_html": job.get("description", ""),
                 "jd_plain": job.get("descriptionPlain", ""),
                 "jd_lists_raw": job.get("lists", []),  # requirements/responsibilities sections
                 "workplace_type": workplace_type},
        ))
    return postings


def main():
    companies = load_watchlist(ats_filter="lever")
    if not companies:
        print("No lever-type companies in watchlist.csv")
        return

    all_postings = []
    for row in companies:
        postings = fetch_company(row)
        print(f"  {row['company']}: {len(postings)} live postings")
        all_postings.extend(postings)

    new, old = dedupe_new(all_postings)
    write_daily_raw(new, "lever")
    append_seen(new)

    print(f"\nLever: {len(all_postings)} total, {len(new)} new, {len(old)} already seen")
    return new


if __name__ == "__main__":
    sys.exit(0 if main() is not None else 1)