"""Keep the README's PyPI download badge counting both package names.

The project shipped as ``django-orm-lens`` until 2026-10-31 and as
``orm-lens`` since. A pepy badge counts one name, so after the rename it
either froze on the old name or restarted from zero on the new one.

Source: pypistats.org, daily totals including mirrors (the figure pepy shows).
pypistats keeps only the last 180 days, so every run merges what it returns
into a history file and the badge is summed from that history, not from the
window.

Double counting: from 2026-10-31 ``django-orm-lens`` is a shim that pins
``orm-lens==<same version>``, so one install of it is also one download of
``orm-lens``. The total therefore takes ``orm-lens`` in full and
``django-orm-lens`` only before the shim existed. Old pinned versions
installed after that date are left out - the badge undercounts slightly
rather than overstate.

Usage: python scripts/download_badge.py <dir>
<dir> holds downloads-history.json (read if present) and receives it back
together with pypi-downloads.json, a shields.io endpoint document.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

PACKAGES = ("orm-lens", "django-orm-lens")
LEGACY = "django-orm-lens"
SHIM_SINCE = "2026-10-31"  # first day django-orm-lens pulled orm-lens in
API = "https://pypistats.org/api/packages/{}/overall?mirrors=true"


def fetch_daily(package: str) -> dict[str, int]:
    req = urllib.request.Request(
        API.format(package), headers={"User-Agent": "orm-lens-download-badge"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = json.load(resp)["data"]
    return {
        r["date"]: int(r["downloads"])
        for r in rows
        if r.get("category") == "with_mirrors"
    }


def merge(history: dict[str, dict[str, int]], fresh: dict[str, dict[str, int]]):
    """Fresh days win: pypistats revises the most recent day after the fact."""
    out = {pkg: dict(days) for pkg, days in history.items()}
    for pkg, days in fresh.items():
        out.setdefault(pkg, {}).update(days)
    return out


def total(history: dict[str, dict[str, int]]) -> int:
    count = 0
    for pkg, days in history.items():
        for day, n in days.items():
            if pkg == LEGACY and day >= SHIM_SINCE:
                continue
            count += n
    return count


def humanize(n: int) -> str:
    """Round down, as a badge should: 999 -> '999', 9480 -> '9.4k', 31025 -> '31k'."""
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n // 100 / 10:g}k"
    if n < 1_000_000:
        return f"{n // 1000}k"
    return f"{n // 100_000 / 10:g}M"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/download_badge.py <dir>", file=sys.stderr)
        return 2
    target = Path(argv[1])
    target.mkdir(parents=True, exist_ok=True)
    history_path = target / "downloads-history.json"
    history = (
        json.loads(history_path.read_text(encoding="utf-8"))
        if history_path.exists()
        else {}
    )

    # Any failed fetch aborts before writing, so a pypistats outage (or its
    # 429s) leaves yesterday's badge up instead of a smaller number.
    fresh = {pkg: fetch_daily(pkg) for pkg in PACKAGES}
    history = merge(history, fresh)
    n = total(history)

    history_path.write_text(
        json.dumps(history, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    badge = {
        "schemaVersion": 1,
        "label": "downloads",
        "message": humanize(n),
        "color": "3775a9",
        "namedLogo": "pypi",
        "logoColor": "white",
    }
    (target / "pypi-downloads.json").write_text(
        json.dumps(badge) + "\n", encoding="utf-8"
    )
    print(f"{n} downloads -> {badge['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
