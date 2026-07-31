"""
Free, regex-based approximation of score.py's extract_facts() — for testing
the RUBRIC's math (caps, weights, tiers) against real JD text before you've
set up API billing. No network call, no API key needed.

Real tradeoff, stated plainly: this is deliberately less accurate than the
LLM version. "5+ years" is easy to catch with regex; "you'll thrive here if
you've spent a few years shipping production systems" is not — a human or
an LLM reads that as an experience signal, regex mostly won't. Same for
sponsorship language, which companies phrase a hundred different ways.

Use this to sanity-check that compute_score()'s caps/weights/tiers behave
sensibly on real postings. Don't treat its output as the real digest — once
you're satisfied the rubric's logic is right, switch to score.py's actual
LLM extraction for the real thing, since it'll catch phrasing this can't.
"""
import json
import re
import sys

import yaml

from common import REPO_ROOT, today_str
from score import get_jd_text, compute_score

YEARS_PATTERNS = [
    r"(\d+)\+?\s*(?:to\s*\d+\s*)?years?\s+(?:of\s+)?(?:relevant\s+)?experience",
    r"minimum\s+(?:of\s+)?(\d+)\s+years?",
    r"(\d+)-(\d+)\s+years?\s+of\s+experience",
]

SENIOR_TITLE_WORDS = {"senior", "staff", "principal", "lead", "director", "vp", "svp", "manager", "head of"}

US_ONLY_PATTERNS = [
    r"must be (?:a )?(?:us|u\.s\.) citizen",
    r"authorized to work in the united states",
    r"us work authorization required",
    r"unable to sponsor",
    r"no sponsorship (?:is )?(?:available|provided)",
    r"not able to provide (?:visa )?sponsorship",
]

# A PGWP satisfies "authorized to work in Canada" — only flag if a posting
# explicitly requires PR/citizenship specifically, which PGWP does not cover.
PR_CITIZENSHIP_PATTERNS = [
    r"must be (?:a )?canadian (?:permanent resident|citizen)",
    r"canadian pr (?:or citizen)?(?: status)? required",
    r"permanent resident(?:ship)? (?:or citizen(?:ship)?)? required",
    r"must be a u\.?s\.? citizen or green card holder",
]

# Deliberately narrow — a government/military security clearance is a much
# higher, different bar than an ordinary background check, and the candidate
# can pass a routine background check. Don't let "background check" or
# "reference check" phrasing false-trigger this.
SECURITY_CLEARANCE_PATTERNS = [
    r"security clearance",
    r"\btop secret\b",
    r"\bsecret clearance\b",
    r"eligib\w* for (?:a )?(?:government )?(?:security )?clearance",
    r"clearance (?:required|eligibility|eligible)",
]

AI_WORKFLOW_PATTERNS = [r"\bai[\s-]?assisted\b", r"copilot", r"claude code", r"llm", r"machine learning tool"]
WCAG_PATTERNS = [r"\bwcag\b", r"accessibility", r"a11y"]
SMALL_TEAM_PATTERNS = [r"small team", r"wear many hats", r"full ownership", r"fast-?paced startup",
                        r"early[\s-]stage", r"high autonomy", r"end-to-end ownership"]

REMOTE_PATTERNS = [r"\bremote\b"]

# Canadian cities that show up in location fields WITHOUT the literal word
# "Canada" attached (e.g. Greenhouse often just says "Waterloo, Ontario" or
# "Toronto") — needed because trusting only the word "Canada" undercounts.
CANADIAN_CITIES = [
    "toronto", "vancouver", "waterloo", "calgary", "ottawa", "montreal",
    "edmonton", "winnipeg", "victoria", "hamilton", "kitchener",
    "mississauga", "brampton", "surrey", "burnaby", "regina", "saskatoon",
    "halifax", "dartmouth", "moncton", "fredericton", "st. john's",
]

US_MARKERS = ["united states", "usa", "u.s.", "(us)", " us)", "us-based"]
UK_MARKERS = ["united kingdom", "(uk)", " uk)", "u.k."]


def _search_any(patterns, text):
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def classify_location(location_field):
    """Classify using ONLY the structured location field the ATS API already
    provides — never the JD body text. The JD body frequently mentions other
    countries in unrelated boilerplate (other office locations, compliance
    text, benefits sections), which caused wildly inconsistent results when
    this used to search the full JD text: the same "Remote (United States)"
    role would sometimes get remote_canada and sometimes get correctly
    capped, purely depending on whether the word "Canada" happened to appear
    somewhere else in that specific JD's wording. The location field itself
    is reliable and doesn't have that noise."""
    loc = location_field.lower()
    is_remote = bool(re.search(r"\bremote\b", loc))

    # Multi-region postings (e.g. "London; Canada; Europe; United States")
    # list several eligible countries in ONE posting — meaning any of them
    # works, not that the role is exclusive to whichever appears first. This
    # differs from Tailscale-style separate postings per country, where each
    # listing genuinely IS exclusive to one region. So: check for Canada
    # FIRST, before any US/UK exclusivity check — checking US/UK first would
    # wrongly reject a posting that also lists Canada as an eligible option.
    if "canada" in loc or any(city in loc for city in CANADIAN_CITIES):
        if any(city in loc for city in ("halifax", "dartmouth", "bedford")) or "nova scotia" in loc:
            return "halifax_ns_onsite"
        return "remote_canada" if is_remote else "other_canada_onsite"

    if any(m in loc for m in US_MARKERS):
        return "outside_canada"
    if any(m in loc for m in UK_MARKERS):
        return "outside_canada"
    if is_remote:
        return "unclear"  # remote but no country signal at all — don't guess
    return "unclear"


def extract_years(text):
    for pattern in YEARS_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            nums = [int(g) for g in m.groups() if g and g.isdigit()]
            if nums:
                return max(nums)  # take the upper bound if a range like "3-5 years"
    return None


def classify_seniority(title):
    t = title.lower()
    for word in ("staff", "principal", "director", "vp", "svp", "head of"):
        if word in t:
            return "staff_or_above"
    if "senior" in t or re.search(r"\bsr\.?\b", t) or "lead" in t or "manager" in t:
        return "senior"
    return "new_grad"  # title_prefilter.py already filtered out the obvious mismatches upstream


def _keyword_present(term, text):
    """Word-boundary match, not bare substring. Bare re.escape(term) was
    matching 'Go' (the ramp-up penalty item) inside 'going', 'goals',
    'good', 'ongoing' — ordinary English, present in nearly every JD —
    which was silently applying a -5 penalty to almost every posting
    regardless of whether the role actually touches Go. Lookaround
    (rather than \\b) handles terms with punctuation cleanly too
    (e.g. '.NET / C#', 'CI/CD')."""
    variants = KEYWORD_SYNONYMS.get(term, [term])
    for variant in variants:
        pattern = r"(?<![A-Za-z0-9])" + re.escape(variant) + r"(?![A-Za-z0-9])"
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False

KEYWORD_SYNONYMS = {
    "REST": ["REST", "RESTful"],
    "Kubernetes": ["Kubernetes", "K8s"],
    "PostgreSQL": ["PostgreSQL", "Postgres"],
    "JavaScript": ["JavaScript", "JS"],
    "Node.js": ["Node.js", "Node", "NodeJS"],
}


def extract_facts_offline(posting, rubric):
    jd_text = get_jd_text(posting)
    stack_list = sum(rubric["stack_match"]["strong_signals"].values(), [])
    ramp_list = rubric["ramp_up_penalty"]["unfamiliar_platforms"]

    matched_stack = [s for s in stack_list if _keyword_present(s, jd_text)]
    unfamiliar = [s for s in ramp_list if _keyword_present(s, jd_text)]

    return {
        "years_required": extract_years(jd_text),
        "title_seniority": classify_seniority(posting["title"]),
        "requires_us_only_auth": _search_any(US_ONLY_PATTERNS, jd_text),
        "denies_sponsorship": _search_any(US_ONLY_PATTERNS, jd_text),  # same signal set, regex can't reliably split these
        "requires_pr_or_citizenship": _search_any(PR_CITIZENSHIP_PATTERNS, jd_text),
        "requires_security_clearance": _search_any(SECURITY_CLEARANCE_PATTERNS, jd_text),
        "location_type": classify_location(posting.get("location", "")),
        "matched_stack": matched_stack,
        "unfamiliar_platforms_mentioned": unfamiliar,
        "ai_in_daily_workflow": _search_any(AI_WORKFLOW_PATTERNS, jd_text),
        "accessibility_or_wcag_mentioned": _search_any(WCAG_PATTERNS, jd_text),
        "small_team_full_ownership_signal": _search_any(SMALL_TEAM_PATTERNS, jd_text),
        "_extraction_method": "offline_regex_approximation",  # flag so you know this wasn't the LLM
    }


def main():
    today = today_str()
    verified_path = REPO_ROOT / "data" / "verified" / f"{today}.json"
    if not verified_path.exists():
        print(f"No verified postings for {today} at {verified_path}. Run verify_posting.py first.")
        return []

    with open(verified_path, encoding="utf-8") as f:
        postings = [p for p in json.load(f) if p.get("verified")]

    if not postings:
        print("No verified postings to score.")
        return []

    rubric = yaml.safe_load((REPO_ROOT / "rubric.yaml").read_text(encoding="utf-8"))
    profile = rubric["profile"]

    results = []
    for posting in postings:
        facts = extract_facts_offline(posting, rubric)
        score, tier, caps, notes = compute_score(facts, rubric, profile)
        results.append({**posting, "score": score, "tier": tier,
                         "caps_triggered": caps, "notes": notes, "facts": facts})
        print(f"  {posting['company']} — {posting['title'][:45]}: {score} ({tier})")

    out_dir = REPO_ROOT / "data" / "final"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print(f"\n[OFFLINE MODE — regex approximation, not the real LLM extraction]")
    print(f"Scored {len(results)} postings. Written to {out_path}")
    print("Run digest.py next to see the sorted result. Once you're happy with how "
          "the rubric behaves, switch to score.py (needs ANTHROPIC_API_KEY) for real "
          "extraction accuracy on nuanced phrasing this regex version will miss.")
    return results


if __name__ == "__main__":
    sys.exit(0 if main() is not None else 1)