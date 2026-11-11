"""Regression test for issue #227 — abstract bases matched by name workspace-wide.

Two apps each define an ``AbstractLine`` base. Before the fix, every subclass
was matched to whichever ``AbstractLine`` was scanned first, so ``order.Line``
inherited the ``basket`` app's fields. A subclass must now inherit from the
base in its own directory.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orm_lens.drift import detect_drift
from orm_lens.parser import scan_workspace

BASKET_BASE = (
    "from django.db import models\n\n\n"
    "class AbstractLine(models.Model):\n"
    "    basket = models.CharField(max_length=50)\n\n"
    "    class Meta:\n"
    "        abstract = True\n"
)
ORDER_BASE = (
    "from django.db import models\n\n\n"
    "class AbstractLine(models.Model):\n"
    "    title = models.CharField(max_length=50)\n\n"
    "    class Meta:\n"
    "        abstract = True\n"
)
LINE_MODEL = (
    "from {app}.abstract_models import AbstractLine\n\n\n"
    "class Line(AbstractLine):\n"
    "    pass\n"
)


MIGRATION = (
    "from django.db import migrations, models\n\n\n"
    "class Migration(migrations.Migration):\n"
    "    dependencies = []\n"
    "    operations = [\n"
    '        migrations.CreateModel(name="Line", fields=[("{field}", '
    'models.CharField(max_length=50))]),\n'
    "    ]\n"
)


class SameNameAbstractBaseTest(unittest.TestCase):
    def test_subclass_inherits_from_own_app_base(self) -> None:
        """A subclass in ``order/`` gets the ``order`` base's fields."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for app, base in (("basket", BASKET_BASE), ("order", ORDER_BASE)):
                (root / app).mkdir()
                (root / app / "abstract_models.py").write_text(base, encoding="utf-8")
                (root / app / "models.py").write_text(
                    LINE_MODEL.format(app=app), encoding="utf-8"
                )
            index = scan_workspace(str(root))
            lines = {
                a.name: next(m for m in a.models if m.name == "Line")
                for a in index.apps
            }
            self.assertEqual(
                sorted(f.name for f in lines["order"].all_fields()), ["title"]
            )
            self.assertEqual(
                sorted(f.name for f in lines["basket"].all_fields()), ["basket"]
            )

    def test_subclass_in_models_package_inherits_from_own_app_base(self) -> None:
        """A subclass in ``order/models/`` gets the ``order`` base's fields."""
        # ``order/models/line.py`` sits one level deeper than the base in
        # ``order/abstract_models.py``; the same app must still win.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for app, base in (("basket", BASKET_BASE), ("order", ORDER_BASE)):
                (root / app / "models").mkdir(parents=True)
                (root / app / "abstract_models.py").write_text(base, encoding="utf-8")
                (root / app / "models" / "line.py").write_text(
                    LINE_MODEL.format(app=app), encoding="utf-8"
                )
            index = scan_workspace(str(root))
            lines = {
                a.name: next(m for m in a.models if m.name == "Line")
                for a in index.apps
            }
            self.assertEqual(
                sorted(f.name for f in lines["order"].all_fields()), ["title"]
            )
            self.assertEqual(
                sorted(f.name for f in lines["basket"].all_fields()), ["basket"]
            )


class SameNameDriftTest(unittest.TestCase):
    def test_issue_227_repro_reports_no_drift(self) -> None:
        """``detect_drift`` on the issue #227 repro reports no drift."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for app, base, field in (
                ("basket", BASKET_BASE, "basket"),
                ("order", ORDER_BASE, "title"),
            ):
                (root / app / "migrations").mkdir(parents=True)
                (root / app / "abstract_models.py").write_text(base, encoding="utf-8")
                (root / app / "models.py").write_text(
                    LINE_MODEL.format(app=app), encoding="utf-8"
                )
                (root / app / "migrations" / "0001_initial.py").write_text(
                    MIGRATION.format(field=field), encoding="utf-8"
                )
            report = detect_drift(root)
            self.assertEqual(report.drifted, [], report.drifted)


if __name__ == "__main__":
    unittest.main()
