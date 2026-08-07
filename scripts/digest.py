"""
Build today's digest from data/final/<today>.json.

Output: digests/<today>.md — sorted apply_first > worth_a_look > skip (collapsed).
Renders each tier as a markdown table instead of a flat bullet/comma list —
much easier to scan. Also merges today's URLs into data/seen_postings.json so
tomorrow's fetch scripts don't re-surface the same postings.
"""
import json
import sys

from common import REPO_ROOT, DIGEST_DIR, today_str


def esc(text):
    """Escape pipe characters so they don't break table cells."""
    return str(text).replace("|", "\\|") if text else ""


def split_notes(notes_list):
    """Pull out stack-match / ramp-up / bonus / location notes into separate
    short fields so the table doesn't need one giant run-on notes column."""
    stack, ramp, bonuses = "", "", []
    for n in notes_list:
        if n.startswith("stack match:"):
            stack = n.replace("stack match:", "").strip()
        elif n.startswith("ramp-up:"):
            ramp = n.replace("ramp-up:", "").strip()
        else:
            bonuses.append(n)
    return stack, ramp, "; ".join(bonuses)


def format_table(postings):
    if not postings:
        return "*None today.*\n"

    header = (
        "| Score | Company | Title | Location | Stack Match | Ramp-Up | Other | Flags |\n"
        "|---|---|---|---|---|---|---|---|\n"
    )
    rows = []
    for p in postings:
        stack, ramp, bonuses = split_notes(p.get("notes", []))
        flags = ", ".join(p.get("caps_triggered", [])) or "—"
        title_link = f"[{esc(p['title'])}]({p['url']})"
        if p.get("newly_scored"):
            title_link = "🆕 " + title_link
        rows.append(
            f"| **{p['score']}** | {esc(p['company'])} | {title_link} | "
            f"{esc(p['location'])} | {esc(stack) or '—'} | {esc(ramp) or '—'} | "
            f"{esc(bonuses) or '—'} | {flags} |"
        )
    return header + "\n".join(rows) + "\n"


def main():
    today = today_str()
    final_path = REPO_ROOT / "data" / "final" / f"{today}.json"
    if not final_path.exists():
        print(f"No scored postings for {today} — writing empty digest.")
        postings = []
    else:
        with open(final_path, encoding="utf-8") as f:
            postings = json.load(f)

    apply_first = sorted([p for p in postings if p["tier"] == "apply_first"],
                          key=lambda p: -p["score"])
    worth_a_look = sorted([p for p in postings if p["tier"] == "worth_a_look"],
                           key=lambda p: -p["score"])
    skipped = sorted([p for p in postings if p["tier"] == "skip"],
                      key=lambda p: -p["score"])

    lines = [f"# Digest {today}", ""]

    lines.append(f"## 🟢 Apply First ({len(apply_first)})")
    lines.append(format_table(apply_first))

    lines.append(f"## 🟡 Worth a Look ({len(worth_a_look)})")
    lines.append(format_table(worth_a_look))

    lines.append(f"## ⚪ Skipped ({len(skipped)})")
    lines.append("<details><summary>expand</summary>\n")
    lines.append(format_table(skipped))
    lines.append("</details>")
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
