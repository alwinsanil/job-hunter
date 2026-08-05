"""
Score every verified posting from today's run.

Two-stage design, deliberately:
  1. Claude extracts structured FACTS from the posting text (years required,
     stack overlap, seniority signal, sponsorship language, AI-in-workflow
     mention, location type). This is classification, which LLMs are good at.
  2. Python computes the final score by applying rubric.yaml's caps/weights
     to those facts. This is arithmetic, which Python is good at — never let
     the model just output a number, it'll be inconsistent run to run.

Requires: ANTHROPIC_API_KEY env var, pyyaml, requests.
Input: data/verified/<today>.json (from verify_posting.py)
Output: data/final/<today>.json — one row per posting with score + tier.
"""
import re
import json
import os
import sys
import time

import requests
import yaml

from common import REPO_ROOT, today_str

API_URL = "https://api.anthropic.com/v1/messages"
# Extraction is classification (pull fields from text), not open-ended reasoning —
# Haiku 4.5 handles it well at ~1/3 the cost of Sonnet. Override with env var if
# you want Sonnet's judgment on harder-to-parse postings.
MODEL = os.environ.get("SCORE_MODEL", "claude-haiku-4-5-20251001")
JD_TIMEOUT = 15
JD_MAX_CHARS = 6000


def get_jd_text(posting):
    """Prefer JD content already captured by the fetch_* scripts — Greenhouse,
    Lever, and Ashby all return the full job description in the SAME API
    response that listed the posting in the first place. No reason to hit
    the network again for those, which also sidesteps any WAF/bot-protection
    risk entirely for these three trusted sources.

    Falls back to a live scrape (fetch_jd_text) only for postings that don't
    have this — i.e. web_search_fallback / regional_board sourced postings,
    which never had a structured API response to pull from."""
    raw = posting.get("raw", {})
    html_content = raw.get("jd_html", "")
    plain = raw.get("jd_plain", "")
    lists_raw = raw.get("jd_lists_raw", [])

    if plain:
        text = plain
    elif html_content:
        # Greenhouse (and possibly others) return entity-escaped HTML inside
        # the JSON string — literal "&lt;div&gt;" rather than "<div>". The
        # tag-stripping regex below only matches real < > characters, so
        # without unescaping first, tags never get removed and the extracted
        # text is full of visible tag noise instead of clean prose.
        import html as html_module
        unescaped = html_module.unescape(html_content)
        text = re.sub(r"<[^>]+>", " ", unescaped)
        text = re.sub(r"\s+", " ", text).strip()
    else:
        text = ""

    # Lever splits JD content into description (just the company intro
    # blurb) + a separate structured "lists" array holding the actual
    # substance — requirements, tech stack, nice-to-haves. Without this,
    # get_jd_text() was silently returning only the generic company intro
    # and missing every real requirement/tech-stack line in the posting,
    # which is exactly why Magnet Forensics postings scored with zero
    # stack matches despite the JD listing .NET, C#, AWS, React, Python,
    # Docker, and Kubernetes further down.
    if lists_raw:
        import html as html_module
        section_texts = []
        for section in lists_raw:
            heading = section.get("text", "")
            content_html = section.get("content", "")
            content_plain = re.sub(r"<[^>]+>", " ", html_module.unescape(content_html))
            content_plain = re.sub(r"\s+", " ", content_plain).strip()
            section_texts.append(f"{heading}: {content_plain}")
        text = f"{text} {' '.join(section_texts)}".strip()

    if len(text) >= 200:
        return text[:JD_MAX_CHARS]

    # No usable stored content (or this posting came from a non-API source) —
    # fall back to live fetch.
    return fetch_jd_text(posting["url"])


def fetch_jd_text(url):
    """Best-effort plain-text scrape of the posting page.

    Tries a plain requests.get() first (fast, works for static pages —
    Greenhouse/Lever/Ashby content). Workday/SuccessFactors pages render
    via JS client-side, so requests.get() often returns a near-empty shell.
    If the scraped text looks too thin AND playwright is installed, falls
    back to a real headless browser render. If playwright isn't installed,
    degrades gracefully to the thin result (extraction prompt notes this).

    To enable the headless fallback:
      pip install playwright --break-system-packages
      playwright install chromium
    """
    text = _fetch_static(url)
    if len(text) >= 400:
        return text

    rendered = _fetch_rendered(url)
    return rendered if rendered else text


def _fetch_static(url):
    try:
        resp = requests.get(
            url, timeout=JD_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; job-hunt-scorer/1.0)"},
        )
        resp.raise_for_status()
    except requests.RequestException:
        return ""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", resp.text,
                  flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:JD_MAX_CHARS]


def _fetch_rendered(url):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""  # not installed — caller falls back to thin static text
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url, timeout=JD_TIMEOUT * 1000, wait_until="networkidle")
            text = page.inner_text("body")
            browser.close()
        text = re.sub(r"\s+", " ", text).strip()
        return text[:JD_MAX_CHARS]
    except Exception as e:
        print(f"  [warn] playwright render failed for {url}: {e}")
        return ""

EXTRACTION_SYSTEM = """Extract structured facts from a job posting for a rubric engine. Be literal; don't infer generosity not stated.

CANDIDATE: Canada-based, holds PGWP (open work permit) — already authorized for any Canadian employer, no sponsorship needed.

denies_sponsorship / requires_us_only_auth: true ONLY if posting explicitly denies sponsorship for a Canada-based candidate, or requires status PGWP doesn't cover (e.g. "must be US citizen/green card holder" for a US role). Ignore generic US-only boilerplate (E-Verify, I-9, US EEO notices) — irrelevant to a CA candidate.

requires_pr_or_citizenship: true ONLY if posting explicitly requires Canadian PR/citizenship or non-PGWP status (e.g. "must be US citizen/green card holder"). PGWP satisfies "legally authorized to work in Canada" — don't flag for that phrase alone.

requires_security_clearance: true ONLY for actual gov/military clearance (Secret, Top Secret, defense/intel contract eligibility). NOT for routine background/reference checks. Default false if unsure.

location_type: watch for ambiguous city names (e.g. "London" = London ON unless "UK"/state qualifier says otherwise; use "unclear" if no qualifier at all). Multi-country fields (e.g. "London; Canada; Europe; US") mean ANY listed region is eligible — scan ALL entries for a Canada match before picking outside_canada. Example: "Toronto; New York" → other_canada_onsite/remote_canada (Toronto is Canadian), NOT outside_canada — one non-CA city elsewhere doesn't disqualify. Only mark outside_canada when the field is genuinely single-country and that country isn't Canada (e.g. "Remote (United States)" alone).

employment_type: full_time | part_time | contract | unclear. hours_per_week: <number or null>.

JSON only, no prose:
{
  "years_required": <number|null>,
  "title_seniority": "new_grad"|"mid"|"senior"|"staff_or_above",
  "requires_us_only_auth": <bool>,
  "denies_sponsorship": <bool>,
  "requires_pr_or_citizenship": <bool>,
  "requires_security_clearance": <bool>,
  "location_type": "remote_canada"|"halifax_ns_onsite"|"other_canada_onsite"|"outside_canada"|"unclear",
  "employment_type": "full_time"|"part_time"|"contract"|"unclear",
  "hours_per_week": <number|null>,
  "matched_stack": [<subset of candidate stack list explicitly mentioned>],
  "unfamiliar_platforms_mentioned": [<subset of ramp-up list explicitly mentioned>],
  "ai_in_daily_workflow": <bool>,
  "accessibility_or_wcag_mentioned": <bool>,
  "small_team_full_ownership_signal": <bool>
}
"""


def call_claude(system, user_prompt, max_tokens=800):
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
            "system": system,
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    text = "\n".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    text = text.strip()
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(text)
    return obj


def extract_facts(posting, resume_text, rubric):
    stack_list = sum(rubric["stack_match"]["strong_signals"].values(), [])
    ramp_list = rubric["ramp_up_penalty"]["unfamiliar_platforms"]
    jd_text = get_jd_text(posting)
    jd_block = jd_text if jd_text else "(page text unavailable — infer conservatively from title/company/location only)"
    prompt = f"""RESUME:
{resume_text}

CANDIDATE STACK LIST (for matched_stack field): {stack_list}
RAMP-UP LIST (for unfamiliar_platforms_mentioned field): {ramp_list}

POSTING:
Company: {posting['company']}
Title: {posting['title']}
Location: {posting['location']}
URL: {posting['url']}

JOB DESCRIPTION TEXT (scraped, may be partial or noisy):
{jd_block}
"""
    return call_claude(EXTRACTION_SYSTEM, prompt)


def compute_score(facts, rubric, profile):
    score = rubric["base_score"]
    caps_triggered = []
    notes = []

    years_have = profile["years_experience"]

    # --- hard caps ---
    cap_ceiling = None

    if facts.get("years_required") is not None and facts["years_required"] > years_have + 1.5:
        cap_ceiling = min(cap_ceiling or 999, 40)
        caps_triggered.append("overqualified_years")

    if facts.get("requires_us_only_auth") or facts.get("denies_sponsorship"):
        cap_ceiling = min(cap_ceiling or 999, 15)
        caps_triggered.append("sponsorship_mismatch")

    if facts.get("title_seniority") in ("senior", "staff_or_above"):
        cap_ceiling = min(cap_ceiling or 999, 35)
        caps_triggered.append("seniority_mismatch")

    if facts.get("location_type") == "outside_canada":
        cap_ceiling = min(cap_ceiling or 999, 10)
        caps_triggered.append("no_canada_remote_or_onsite")

    if facts.get("requires_pr_or_citizenship"):
        cap_ceiling = min(cap_ceiling or 999, 10)
        caps_triggered.append("requires_pr_or_citizenship")

    if facts.get("requires_security_clearance"):
        cap_ceiling = min(cap_ceiling or 999, 10)
        caps_triggered.append("requires_security_clearance")

    stack_list = sum(rubric["stack_match"]["strong_signals"].values(), [])
    ramp_list = rubric["ramp_up_penalty"]["unfamiliar_platforms"]

    # --- stack match ---
    matched = [s for s in facts.get("matched_stack", []) if s in stack_list]
    stack_points = min(len(matched) * rubric["stack_match"]["points_per_hit"],
                        rubric["stack_match"]["weight_max"])
    score += stack_points
    if matched:
        notes.append(f"stack match: {', '.join(matched)} (+{stack_points})")

    # --- ramp-up penalty ---
    unfamiliar = [s for s in facts.get("unfamiliar_platforms_mentioned", []) if s in ramp_list]
    penalty = max(len(unfamiliar) * rubric["ramp_up_penalty"]["points_per_hit"],
                  rubric["ramp_up_penalty"]["weight_max"])
    score += penalty
    if unfamiliar:
        notes.append(f"ramp-up: {', '.join(unfamiliar)} ({penalty})")

    # --- bonuses ---
    bonus = rubric["bonus"]
    if facts.get("ai_in_daily_workflow"):
        score += bonus["ai_in_daily_workflow"]
        notes.append(f"AI-in-workflow (+{bonus['ai_in_daily_workflow']})")
    if facts.get("accessibility_or_wcag_mentioned"):
        score += bonus["accessibility_or_wcag_mentioned"]
        notes.append(f"WCAG/accessibility (+{bonus['accessibility_or_wcag_mentioned']})")
    if facts.get("small_team_full_ownership_signal"):
        score += bonus["small_team_full_ownership"]
        notes.append(f"small-team ownership (+{bonus['small_team_full_ownership']})")

    # --- location bonus ---
    loc = facts.get("location_type", "unclear")
    loc_bonus_map = {
        "remote_canada": rubric["location_bonus"]["remote_canada"],
        "halifax_ns_onsite": rubric["location_bonus"]["halifax_ns_onsite"],
        "other_canada_onsite": rubric["location_bonus"]["other_canada_onsite"],
    }
    loc_bonus = loc_bonus_map.get(loc, 0)
    score += loc_bonus
    if loc_bonus:
        notes.append(f"location {loc} (+{loc_bonus})")

    # apply hard cap last
    if cap_ceiling is not None:
        score = min(score, cap_ceiling)

    score = max(0, min(100, round(score)))

    tiers = rubric["tiers"]
    if score >= int(tiers["apply_first"].lstrip(">=")):
        tier = "apply_first"
    elif score >= int(tiers["worth_a_look"].split("-")[0]):
        tier = "worth_a_look"
    else:
        tier = "skip"

    return score, tier, caps_triggered, notes


CACHE_PATH = None  # set inside main() once REPO_ROOT is known
CACHE_MAX_AGE_DAYS = 14  # re-score after this long, in case the JD itself changed


def load_score_cache():
    if not CACHE_PATH.exists():
        return {}
    with open(CACHE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_score_cache(cache):
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def cache_entry_is_fresh(entry):
    from datetime import datetime, timezone
    try:
        scored_at = datetime.fromisoformat(entry["scored_at"])
    except (KeyError, ValueError):
        return False
    age_days = (datetime.now(timezone.utc) - scored_at).days
    return age_days < CACHE_MAX_AGE_DAYS


def main():
    global CACHE_PATH
    today = today_str()
    CACHE_PATH = REPO_ROOT / "data" / "score_cache.json"

    verified_path = REPO_ROOT / "data" / "verified" / f"{today}.json"
    if not verified_path.exists():
        print(f"No verified postings for {today} at {verified_path}. Run verify_posting.py first.")
        return []

    with open(verified_path, encoding="utf-8") as f:
        postings = [p for p in json.load(f) if p.get("verified")]

    if not postings:
        print("No verified/live postings to score today.")
        return []

    resume_text = (REPO_ROOT / "resume" / "resume.md").read_text(encoding="utf-8")
    rubric = yaml.safe_load((REPO_ROOT / "rubric.yaml").read_text(encoding="utf-8"))
    profile = rubric["profile"]

    cache = load_score_cache()
    from datetime import datetime, timezone

    results = []
    scored_fresh, reused_cached = 0, 0
    for posting in postings:
        cached = cache.get(posting["url"])
        if cached and cache_entry_is_fresh(cached):
            results.append({**posting, **cached["result"]})
            reused_cached += 1
            continue

        try:
            facts = extract_facts(posting, resume_text, rubric)
        except (requests.RequestException, RuntimeError, json.JSONDecodeError) as e:
            print(f"  [error] extracting facts for {posting['company']} - {posting['title']}: {e}")
            continue

        score, tier, caps, notes = compute_score(facts, rubric, profile)
        result = {"score": score, "tier": tier, "caps_triggered": caps,
                  "notes": notes, "facts": facts}
        results.append({**posting, **result})
        cache[posting["url"]] = {
            "result": result,
            "scored_at": datetime.now(timezone.utc).isoformat(),
        }
        scored_fresh += 1
        print(f"  {posting['company']} — {posting['title'][:45]}: {score} ({tier})")
        time.sleep(0.5)

    save_score_cache(cache)
    print(f"\n{scored_fresh} newly scored (API calls made), {reused_cached} reused from cache "
          f"(no API cost) — cache entries expire after {CACHE_MAX_AGE_DAYS} days.")

    out_dir = REPO_ROOT / "data" / "final"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"\nScored {len(results)} postings. Written to {out_path}")
    return results


if __name__ == "__main__":
    sys.exit(0 if main() is not None else 1)