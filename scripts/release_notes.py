"""Release page notes and title for a tag, both read from CHANGELOG.md.

Used by publish.yml (``vX.Y.Z``, the extension) and publish-pypi.yml
(``py-vX.Y.Z``, the Python package), so both kinds of release page are built
the same way.

Title. The Releases tab is the first thing a visitor from the repository
sidebar reads, and a page called "v0.20.0" or "py-v1.13.8 - cascade names the
delete handlers Django 6.1's DB_CASCADE skips (#179)" tells them nothing about
why they would want it. A CHANGELOG section may therefore open with a one-line
headline written for users, as a blockquote:

    ## [py-1.16.0] - 2026-10-31

    > SQLAlchemy models and Alembic revisions, read without importing your app.

    ### Added
    ...

When the headline is there it becomes the title; without one the title falls
back to the section's first bold bullet, as before.

Usage: python scripts/release_notes.py <tag> [notes-out]
Writes the notes to notes-out (default release-notes.md), prints the title,
and appends ``title=...`` to $GITHUB_OUTPUT when running in Actions.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

MAX_SUBTITLE = 70


def heading_for(tag: str) -> str:
    """py-v1.8.1 -> py-1.8.1, v0.20.0 -> 0.20.0"""
    if tag.startswith("py-v"):
        return "py-" + tag[len("py-v"):]
    if tag.startswith("v"):
        return tag[1:]
    return tag


def section(changelog: str, heading: str) -> str:
    lines = changelog.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")]
    for n, i in enumerate(starts):
        if re.match(rf"## \[{re.escape(heading)}\]", lines[i]):
            end = starts[n + 1] if n + 1 < len(starts) else len(lines)
            return "\n".join(lines[i + 1:end]).strip()
    return ""


def _plain(text: str) -> str:
    # A release title is plain text on GitHub: markdown would show literally.
    text = text.replace("`", "").replace("**", "")
    text = " ".join(text.split()).rstrip(".")
    if len(text) > MAX_SUBTITLE:
        text = text[: MAX_SUBTITLE - 3].rstrip() + "..."
    return text


def subtitle(body: str) -> str:
    for line in body.splitlines():
        if not line.strip():
            continue
        if line.startswith("> "):
            return _plain(line[2:])
        break  # the headline, if any, is the first thing in the section
    lead = re.search(r"^- \*\*(.+?)\*\*", body, re.MULTILINE)
    return _plain(lead.group(1)) if lead else ""


def title_for(tag: str, body: str) -> str:
    sub = subtitle(body)
    return f"{tag} - {sub}" if sub else tag


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print("usage: python scripts/release_notes.py <tag> [notes-out]", file=sys.stderr)
        return 2
    tag = argv[1]
    out = Path(argv[2] if len(argv) == 3 else "release-notes.md")
    heading = heading_for(tag)
    body = section(Path("CHANGELOG.md").read_text(encoding="utf-8"), heading)
    if not body:
        # No section is not a reason to fail a publish that has already
        # reached the registry. Ship a stub and say so.
        body = f"See CHANGELOG.md for {heading}."
        print(f"::warning::no CHANGELOG section for {heading}")
    out.write_text(body + "\n", encoding="utf-8")

    title = title_for(tag, body)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"title={title}\n")
    print("release title:", title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
