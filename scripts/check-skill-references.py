#!/usr/bin/env python3
"""Check that every skill-bundle path SKILL.md references actually exists.

The bug class: the doc tells the conductor to run `<this-skill>/scripts/oprun.py` or to open
`templates/run-proposal.md`, and the file is not in the bundle — so the instruction cannot succeed.
It has happened: `templates/run-proposal.md` was referenced while absent from the install, and a
missing module stays invisible until someone follows the doc.

Scope: paths under the bundle dirs (`scripts/`, `references/`, `templates/`, `assets/`) mentioned in
`SKILL.md`. Repo paths (`docs/`, `tests/`) and runtime paths (`<worktree>/.oprun/…`) are out of
scope — they are not shipped with the skill.

A path may be referenced *as something the skill bans*, and is then deliberately absent. Name those
in INTENTIONALLY_ABSENT instead of teaching this check a cleverer heuristic: a silencing pattern in
a checker is how a checker stops telling the truth.

Stdlib only. Exit 0 when nothing is missing, 1 when something is.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BUNDLE_DIRS = ("scripts", "references", "templates", "assets")
INTENTIONALLY_ABSENT = {
    "references/herdr.md": "banned by the SKILL.md Bans section (a PTY/Herdr lane is not a witness)",
}
REFERENCE = re.compile(r"(?<![\w/.-])((?:%s)/[\w.-]+)" % "|".join(BUNDLE_DIRS))
TRAILING = ".,;:)`\"'"


def referenced_paths(skill_md: str) -> list[str]:
    """Bundle-relative paths SKILL.md mentions, deduped and sorted.

    `<this-skill>/scripts/x.py` is rewritten to `scripts/x.py` first: `<this-skill>` *is* the bundle
    root, so the leading segment is a label, not a directory.
    """
    text = skill_md.replace("<this-skill>/", "").replace("<skill>/", "")
    return sorted({m.group(1).rstrip(TRAILING) for m in REFERENCE.finditer(text)})


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    skill = repo / "SKILL.md"
    if not skill.is_file():
        print(f"check-skill-references: no SKILL.md in {repo}", file=sys.stderr)
        return 1

    present: list[str] = []
    absent_by_design: list[str] = []
    missing: list[str] = []
    for rel in referenced_paths(skill.read_text(encoding="utf-8")):
        if (repo / rel).is_file():
            present.append(rel)
        elif rel in INTENTIONALLY_ABSENT:
            absent_by_design.append(rel)
        else:
            missing.append(rel)

    for rel in absent_by_design:
        print(f"absent by design: {rel} — {INTENTIONALLY_ABSENT[rel]}")
    checked = len(present) + len(absent_by_design) + len(missing)
    print(
        f"check-skill-references: {checked} bundle path(s) referenced by SKILL.md — "
        f"{len(present)} present, {len(absent_by_design)} absent by design, {len(missing)} missing"
    )

    if missing:
        print("\nMISSING — SKILL.md points at a file the bundle does not ship:", file=sys.stderr)
        for rel in missing:
            print(f"  {rel}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
