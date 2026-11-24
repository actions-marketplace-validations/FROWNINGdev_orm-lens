"""``migration-risk --only`` / positional files narrow the report (pre-commit).

A mature project carries old migrations that already ran in production.
Reporting them on every commit made the pre-commit hook fail forever; with
the staged files passed in, only those migrations are reported, while the
whole history is still analyzed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path

from orm_lens.cli import main

OLD = '''from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shop", "0001_initial")]
    operations = [
        migrations.AddField(
            model_name="item", name="code", field=models.CharField(max_length=10),
        ),
    ]
'''

NEW = '''from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shop", "0002_old")]
    operations = [
        migrations.AddField(
            model_name="item", name="sku", field=models.CharField(max_length=10),
        ),
    ]
'''

INITIAL = '''from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Item",
            fields=[("id", models.AutoField(primary_key=True))],
        ),
    ]
'''


def _project(tmp_path: Path) -> Path:
    mig = tmp_path / "shop" / "migrations"
    mig.mkdir(parents=True)
    (tmp_path / "shop" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "shop" / "models.py").write_text(
        "from django.db import models\n\n\nclass Item(models.Model):\n"
        "    code = models.CharField(max_length=10)\n"
        "    sku = models.CharField(max_length=10)\n",
        encoding="utf-8",
    )
    (mig / "__init__.py").write_text("", encoding="utf-8")
    (mig / "0001_initial.py").write_text(INITIAL, encoding="utf-8")
    (mig / "0002_old.py").write_text(OLD, encoding="utf-8")
    (mig / "0003_new.py").write_text(NEW, encoding="utf-8")
    return mig


def _run(argv: list[str]) -> tuple[int, list[str]]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["migration-risk", "--format", "json", *argv])
    return code, sorted({Path(f["filePath"]).name for f in json.loads(out.getvalue())["findings"]})


def test_default_reports_every_migration(tmp_path: Path) -> None:
    _project(tmp_path)
    code, files = _run(["--path", str(tmp_path)])
    assert code == 1
    assert files == ["0002_old.py", "0003_new.py"]


def test_only_flag_keeps_the_given_migration(tmp_path: Path) -> None:
    mig = _project(tmp_path)
    code, files = _run(["--path", str(tmp_path), "--only", str(mig / "0003_new.py")])
    assert code == 1
    assert files == ["0003_new.py"]


def test_positional_relative_paths_from_repo_root(tmp_path: Path) -> None:
    # pre-commit runs from the repo root and passes repo-relative paths.
    _project(tmp_path)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        code, files = _run(["shop/migrations/0003_new.py"])
    finally:
        os.chdir(cwd)
    assert code == 1
    assert files == ["0003_new.py"]


def test_a_clean_staged_migration_exits_zero(tmp_path: Path) -> None:
    mig = _project(tmp_path)
    code, files = _run(["--path", str(tmp_path), str(mig / "0001_initial.py")])
    assert code == 0
    assert files == []
