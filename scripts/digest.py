"""
Build today's digest from data/final/<today>.json.

Output: digests/<today>.md — sorted apply_first > worth_a_look > skip (collapsed).
Also merges today's URLs into data/seen_postings.json so tomorrow's fetch
scripts don't re-surface the same postings.
"""
import json
import sys

from common import REPO_ROOT, DIGEST_DIR, today_str


def format_row(p):
    caps = f" ⚠ {', '.join(p['caps_triggered'])}" if p["caps_triggered"] else ""
    notes = "; ".join(p["notes"]) if p["notes"] else ""
    return (f"- **[{p['company']}]({p['url']})** — {p['title']} — "
            f"**{p['score']}**{caps}\n  - {p['location']} · {notes}")


def main():
    today = today_str()
    final_path = REPO_ROOT / "data" / "final" / f"{today}.json"
    if not final_path.exists():
        print(f"No scored postings for {today} at {final_path}. Run score.py first.")
        return None

    with open(final_path, encoding="utf-8") as f:
        postings = json.load(f)

    apply_first = sorted([p for p in postings if p["tier"] == "apply_first"],
                          key=lambda p: -p["score"])
    worth_a_look = sorted([p for p in postings if p["tier"] == "worth_a_look"],
                           key=lambda p: -p["score"])
    skipped = sorted([p for p in postings if p["tier"] == "skip"],
                      key=lambda p: -p["score"])

    lines = [f"# Digest {today}", ""]

    lines.append(f"## Apply First ({len(apply_first)})")
    lines.extend(format_row(p) for p in apply_first) if apply_first else lines.append("*None today.*")
    lines.append("")

    lines.append(f"## Worth a Look ({len(worth_a_look)})")
    lines.extend(format_row(p) for p in worth_a_look) if worth_a_look else lines.append("*None today.*")
    lines.append("")

    lines.append(f"## Skipped ({len(skipped)})")
    lines.append("<details><summary>expand</summary>\n")
    lines.extend(format_row(p) for p in skipped) if skipped else lines.append("*None today.*")
    lines.append("\n</details>")
    lines.append("")

    lines.append("---")
    lines.append(f"Total scored today: {len(postings)}")

    digest_path = DIGEST_DIR / f"{today}.md"
    digest_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"Digest written to {digest_path}")
    print(f"  Apply first: {len(apply_first)}  Worth a look: {len(worth_a_look)}  Skipped: {len(skipped)}")
    return digest_path


if __name__ == "__main__":
    sys.exit(0 if main() is not None else 1)
