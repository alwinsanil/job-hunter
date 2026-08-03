"""
Fetch live postings from Ashby for every watchlist company with ats_type=ashby.

Ashby public API: no auth needed.
  GET https://api.ashbyhq.com/posting-api/job-board/{job_board_name}

job_board_name is the careers-page slug, e.g.
  https://jobs.ashbyhq.com/company-name  ->  job_board_name = "company-name"
"""
import sys
import time
import requests

from common import load_watchlist, make_posting, write_daily_raw, dedupe_new, append_seen

API_BASE = "https://api.ashbyhq.com/posting-api/job-board/{name}"
TIMEOUT = 15
RETRY = 2


def slugify(company_name):
    return company_name.strip().lower().replace(" ", "-").replace(".", "").replace(",", "")


def fetch_company(company_row):
    name = company_row.get("board_token") or slugify(company_row["company"])
    url = API_BASE.format(name=name)

    for attempt in range(RETRY + 1):
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            if resp.status_code == 404:
                print(f"  [skip] {company_row['company']}: no Ashby board at '{name}'")
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
        # Ashby returns a single primary "location" field plus a separate
        # "secondaryLocations" array for multi-region postings. Only
        # reading the primary field was silently dropping every other
        # eligible location — including "Canada" specifically — from
        # postings that were actually open to Canada-based candidates.
        primary = job.get("location", "unknown")
        secondary = [loc.get("location", "") for loc in job.get("secondaryLocations", []) if loc.get("location")]
        location = "; ".join([primary] + secondary) if secondary else primary

        postings.append(make_posting(
            company=company_row["company"],
            title=job.get("title", "").strip(),
            url=job.get("jobUrl", ""),
            location=location,
            ats_source="ashby",
            raw={"id": job.get("id"), "publishedAt": job.get("publishedAt"),
                 "jd_html": job.get("descriptionHtml", ""),
                 "jd_plain": job.get("descriptionPlain", "")},
        ))
    return postings


def main():
    companies = load_watchlist(ats_filter="ashby")
    if not companies:
        print("No ashby-type companies in watchlist.csv")
        return

    all_postings = []
    for row in companies:
        postings = fetch_company(row)
        print(f"  {row['company']}: {len(postings)} live postings")
        all_postings.extend(postings)

    new, old = dedupe_new(all_postings)
    write_daily_raw(new, "ashby")
    append_seen(new)

    print(f"\nAshby: {len(all_postings)} total, {len(new)} new, {len(old)} already seen")
    return new


if __name__ == "__main__":
    sys.exit(0 if main() is not None else 1)