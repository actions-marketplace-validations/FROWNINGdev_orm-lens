"""django-orm-lens -> orm-lens: everything written against the old name still works."""

from __future__ import annotations

import os
import subprocess
import sys
import warnings
from pathlib import Path
from unittest import mock

import pytest

import orm_lens.workspace

# The old names ship in the compat distribution (compat/django-orm-lens), which
# CI does not pip-install; put its source on the path instead.
COMPAT = Path(__file__).resolve().parents[2] / "compat" / "django-orm-lens"
if COMPAT.is_dir() and str(COMPAT) not in sys.path:
    sys.path.insert(0, str(COMPAT))
with warnings.catch_warnings():
    # The import's own DeprecationWarning is checked below, in a fresh process.
    warnings.simplefilter("ignore", DeprecationWarning)
    pytest.importorskip("django_orm_lens")


def _run(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k != "ORM_LENS_NO_RENAME_NOTICE"}
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        check=True,
        env={**base, "PYTHONPATH": os.pathsep.join(sys.path), **env},
    )


def test_old_top_level_import_is_the_same_api() -> None:
    import django_orm_lens

    assert django_orm_lens.scan_workspace is orm_lens.scan_workspace
    assert django_orm_lens.__version__ == orm_lens.__version__


def test_old_submodule_imports_are_the_same_modules() -> None:
    import django_orm_lens.cli
    import django_orm_lens.query_analyzer
    from django_orm_lens.parser import scan_workspace

    assert django_orm_lens.cli is orm_lens.cli
    assert django_orm_lens.query_analyzer is sys.modules["orm_lens.query_analyzer"]
    assert scan_workspace is orm_lens.scan_workspace
    # The alias must not rewrite the real module's identity.
    assert orm_lens.cli.__spec__ is not None
    assert orm_lens.cli.__spec__.name == "orm_lens.cli"


def test_patching_through_the_old_path_reaches_the_real_module() -> None:
    with mock.patch("django_orm_lens.workspace._env", return_value="") as fake:
        assert orm_lens.workspace._env is fake


def test_python_dash_m_old_name_runs_the_cli() -> None:
    out = _run("-m", "django_orm_lens", "--version")
    assert out.stdout.strip().startswith("orm-lens ")


def test_old_commands_say_the_project_is_now_orm_lens() -> None:
    out = _run("-m", "django_orm_lens", "--version")
    # stderr only, and once: stdout carries JSON reports and the MCP stream.
    assert out.stderr.count("django-orm-lens is now orm-lens") == 1
    assert "pip install orm-lens" in out.stderr
    assert "DeprecationWarning" not in out.stderr
    assert "now orm-lens" not in out.stdout


def test_rename_notice_can_be_hidden() -> None:
    out = _run("-m", "django_orm_lens", "--version", ORM_LENS_NO_RENAME_NOTICE="1")
    assert out.stderr == ""
    assert out.stdout.strip().startswith("orm-lens ")


def test_old_commands_do_not_import_the_old_package() -> None:
    out = _run(
        "-c",
        "import sys, django_orm_lens_cli; print('django_orm_lens' in sys.modules)",
    )
    assert out.stdout.strip() == "False"


def test_importing_the_old_name_is_deprecated() -> None:
    code = (
        "import warnings\n"
        "with warnings.catch_warnings(record=True) as seen:\n"
        "    warnings.simplefilter('always')\n"
        "    import django_orm_lens\n"
        "print([str(w.message) for w in seen if w.category is DeprecationWarning])"
    )
    out = _run("-c", code)
    assert "import orm_lens instead" in out.stdout


def test_env_vars_fall_back_to_the_pre_rename_names(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if "ORM_LENS_" not in k}
    env["DJANGO_ORM_LENS_ROOT"] = str(tmp_path)
    with mock.patch.dict(os.environ, env, clear=True):
        assert orm_lens.workspace.resolve_workspace() == tmp_path.resolve()
        os.environ["ORM_LENS_ROOT"] = str(tmp_path / "missing")
        # The new name wins when both are set.
        assert not isinstance(orm_lens.workspace.resolve_workspace(), Path)
