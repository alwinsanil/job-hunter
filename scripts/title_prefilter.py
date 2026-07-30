"""
Zero-cost title/function pre-filter, run BEFORE score.py's API calls.

Two real problems this fixes:
1. Company job boards list EVERY role at the company, not just engineering —
   Sales, Marketing, Legal, VP/SVP roles were all reaching the paid scoring
   step. That's pure wasted API spend on postings that were never plausible.
2. Relying on the LLM extraction step to "notice" a VP title doesn't fit is
   fragile — a keyword match is deterministic and free, an LLM call is
   neither. Filter what you can filter for free; only pay to reason about
   the postings that survive.

This does NOT replace the rubric's hard_caps in score.py (years-required,
sponsorship, remaining seniority nuance) — it removes the obviously-wrong
postings before they cost anything, so the paid step only ever sees
plausible candidates.
"""
import re

# Titles containing any of these are excluded outright — leadership/executive
# track, not reachable for a new grad regardless of stack match.
SENIORITY_EXCLUDE = [
    r"\bVP\b", r"\bSVP\b", r"\bEVP\b", r"vice president",
    r"\bchief\b", r"\bpresident\b",
    r"\bdirector\b",
    r"head of\b",
    r"distinguished",
    r"\bprincipal\b",
    r"\bstaff\b",              # Staff Engineer = senior IC track, not new-grad reachable
    r"\blead\b",                # Tech Lead / Team Lead / "Lead Engineer" = senior IC or management
    r"\bmanager\b",              # Engineering Manager / People Manager = management track
]

# "Senior" is its own toggle — off by default per your instruction, but kept
# separate from the list above so you can flip SENIOR_ALLOWED to True later
# without touching the real leadership-exclusion list.
SENIOR_PATTERN = r"\bsenior\b|\bsr\.?\b"
SENIOR_ALLOWED = False

# Non-engineering functions — these show up on every company board regardless
# of company size, and none of them match your target function at all.
FUNCTION_EXCLUDE = [
    r"\bsales\b", r"account executive", r"account manager", r"business development",
    r"\bpartnership", r"\bmarketing\b", r"\bgrowth\b", r"\bbrand\b", r"\bcontent\b",
    r"social media", r"\bseo\b", r"\bpr\b", r"communications",
    r"customer success", r"customer support", r"client success", r"implementation specialist",
    r"\bonboarding\b", r"\bfinance\b", r"\baccounting\b", r"\bfp&a\b", r"\bpayroll\b", r"\btax\b",
    r"\blegal\b", r"\bcounsel\b", r"compliance",
    r"\bhr\b", r"\bpeople\b", r"\btalent\b", r"recruiter", r"recruiting",
    r"\bdesigner\b", r"\bux\b", r"\bui designer",
    r"product manager", r"product marketing",
    r"\boperations\b", r"revenue operations", r"deal desk",
    r"business intelligence analyst", r"\bdata analyst\b",
    r"solutions architect", r"sales engineer", r"forward deployed engineer",
    r"quality assurance engineer", r"\bqa\b(?!.*engineer.*automation)",  # generic QA excluded, automation QA still fuzzy
]

import yaml
from pathlib import Path

def _load_target_titles():
    """rubric.yaml's profile.target_titles is the real source of truth
    for what counts as a target function. This returns a list of regex patterns to match against job titles."""
    rubric_path = Path(__file__).resolve().parent.parent / "rubric.yaml"
    rubric = yaml.safe_load(rubric_path.read_text(encoding="utf-8"))
    titles = rubric.get("profile", {}).get("target_titles", [])
    # Turn each plain-English title into a loose regex: spaces/hyphens are
    # interchangeable, word-boundaried so "SRE" doesn't match inside another word.
    patterns = []
    for t in titles:
        escaped = re.escape(t).replace(r"\ ", r"[\s-]?").replace(r"\-", r"[\s-]?")
        patterns.append(rf"\b{escaped}\b" if len(t) <= 4 else escaped)
    return patterns


# Must match at least one of these to be considered a plausible target role.
# Sourced from rubric.yaml's profile.target_titles — edit the YAML, not this file.
FUNCTION_INCLUDE = _load_target_titles()
# Always-include safety net for common phrasing variants even if rubric.yaml's
# list is edited down to something narrower than expected.
FUNCTION_INCLUDE += [
    r"\bswe\b", r"full.?stack", r"front.?end", r"back.?end",
    r"application developer", r"applications engineer",
]

# Excluded regardless of function match — not applicable to a graduated candidate
LIFECYCLE_EXCLUDE = [r"\bintern\b", r"internship", r"\bco-?op\b"]


def _matches_any(patterns, text):
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def prefilter(title: str):
    """Returns (keep: bool, reason: str). reason is always populated for logging,
    even on keep, so you can audit what passed and why."""
    t = title.strip()

    if _matches_any(LIFECYCLE_EXCLUDE, t):
        return False, "excluded_intern_coop"

    if _matches_any(SENIORITY_EXCLUDE, t):
        return False, "excluded_leadership_or_senior_ic_title"

    if not SENIOR_ALLOWED and re.search(SENIOR_PATTERN, t, re.IGNORECASE):
        return False, "excluded_senior_title"

    if _matches_any(FUNCTION_EXCLUDE, t):
        return False, "excluded_non_target_function"

    if not _matches_any(FUNCTION_INCLUDE, t):
        return False, "excluded_no_target_function_match"

    return True, "passed_prefilter"


def filter_postings(postings: list):
    """Split a posting list into (kept, rejected_with_reasons)."""
    kept, rejected = [], []
    for p in postings:
        ok, reason = prefilter(p["title"])
        if ok:
            kept.append(p)
        else:
            p["prefilter_reject_reason"] = reason
            rejected.append(p)
    return kept, rejected


if __name__ == "__main__":
    # Quick smoke test against the exact titles from your last real run
    test_titles = [
        "SVP Partnerships", "VP of B2B Marketing, CLEAR1", "Senior Software Engineer",
        "Social Media Manager", "Lead, Global Revenue Onboarding", "DevOps Engineer",
        "Software Engineer, Backend", "Engineering Manager I, Applications",
        "Full Stack Engineer", "Director of Engineering, Members",
        "Data/Analytics Co-op", "Solutions Engineer - Commercial (Expansion Sales)",
    ]
    for title in test_titles:
        ok, reason = prefilter(title)
        print(f"  {'KEEP' if ok else 'DROP'}: {title!r} — {reason}")