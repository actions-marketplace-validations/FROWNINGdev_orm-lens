"""The ``django-orm-lens`` and ``django-orm-lens-mcp`` commands.

They run orm-lens and print one line on stderr saying the project is now
orm-lens. Stderr only: stdout carries ``--format json`` reports and the MCP
stdio stream. ``ORM_LENS_NO_RENAME_NOTICE=1`` hides the line.

A top-level module rather than part of the ``django_orm_lens`` package, so
the commands do not import that package and trip its DeprecationWarning on
top of this note.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

NOTICE = (
    "note: django-orm-lens is now orm-lens. Switch with `pip install orm-lens` "
    "and run `{new}` instead. Hide this note with ORM_LENS_NO_RENAME_NOTICE=1."
)


def notify(new: str = "orm-lens") -> None:
    if os.environ.get("ORM_LENS_NO_RENAME_NOTICE", "") not in ("", "0"):
        return
    print(NOTICE.format(new=new), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    notify()
    from orm_lens.cli import main as run

    return run(argv)


def mcp_main() -> int:
    notify("orm-lens-mcp")
    from orm_lens.mcp_server import main as run

    return run()
