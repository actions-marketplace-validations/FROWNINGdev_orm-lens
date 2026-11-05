"""Path helpers shared by the Django and SQLAlchemy parsers.

Kept in a module of their own so that ``parser`` and ``sqlalchemy_parser``
need not import each other: ``parser`` calls into ``sqlalchemy_parser``, and
both walk the same directories and name apps the same way.
"""

from __future__ import annotations

import os

# Directory names skipped when walking a workspace for arbitrary ``.py`` files
# (n+1 scanner, signal graph). Kept broader than ``DEFAULT_EXCLUDES`` on purpose:
# those are glob patterns for the models.py walk, this is a dirname-based
# denylist for the whole-tree walk.
BROAD_SKIP_DIRS: frozenset = frozenset(
    {"migrations", "node_modules", "venv", ".venv", "env", ".git", "__pycache__"}
)


def _app_dir_for(fs_path: str) -> tuple[str, str]:
    parent = os.path.dirname(fs_path)
    parent_name = os.path.basename(parent)
    if parent_name == "models":
        grand = os.path.dirname(parent)
        return grand, os.path.basename(grand)
    return parent, parent_name
