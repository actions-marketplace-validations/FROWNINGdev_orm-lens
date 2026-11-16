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


def _is_app_dir(path: str) -> bool:
    """A directory Django would treat as an app: it has ``migrations/`` or ``apps.py``."""
    return os.path.isdir(os.path.join(path, "migrations")) or os.path.isfile(
        os.path.join(path, "apps.py")
    )


def _package_dir(fs_path: str) -> str:
    """The directory that owns a models file: its parent, or the grandparent for ``models/``."""
    parent = os.path.dirname(fs_path)
    return os.path.dirname(parent) if os.path.basename(parent) == "models" else parent


def _app_dir_for(fs_path: str) -> tuple[str, str]:
    """The app directory and label for a models file.

    Start at the file's directory (or the parent of a ``models/`` package).
    Django labels a model by the app whose package contains the module, so an
    app that splits its models into sub-packages (``io_storages/s3/models.py``)
    still belongs to ``io_storages``: when the start directory is not an app
    itself, walk up while the parent is a package and stop at the first
    directory with ``migrations/`` or ``apps.py``. Nothing found: the start
    directory, as before.
    """
    start = _package_dir(fs_path)
    current = start
    while not _is_app_dir(current):
        up = os.path.dirname(current)
        if up == current or not os.path.isfile(os.path.join(up, "__init__.py")):
            return start, os.path.basename(start)
        current = up
    return current, os.path.basename(current)
