"""Regression tests for explicit default managers in model bodies.

``objects = models.Manager()`` matches FIELD_RE as any other
``models.<Name>(...)`` assignment does, but it declares a manager, not a
column. No migration creates it, so drift used to report it as blocking and
fail CI on a healthy project. TaggableManager stays a field: it is a real
relation.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orm_lens.drift import detect_drift
from orm_lens.parser import parse_models_file

MODELS = """from django.db import models


class Note(models.Model):
    text = models.TextField()

    objects = models.Manager()
    active = models.Manager(
        include_deleted=False,
    )
    rows = models.QuerySet.as_manager()
    tags = TaggableManager()
    title = models.CharField(max_length=100)
"""

MIGRATION = """from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Note",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                ("text", models.TextField()),
                ("tags", models.ManyToManyField(to="taggit.Tag")),
                ("title", models.CharField(max_length=100)),
            ],
        ),
    ]
"""


class ManagerIsNotAFieldTest(unittest.TestCase):
    def test_manager_and_queryset_do_not_become_fields(self) -> None:
        models = parse_models_file("notes/models.py", MODELS)
        self.assertEqual(len(models), 1)
        self.assertEqual(
            [f.name for f in models[0].fields],
            ["text", "tags", "title"],
        )
        self.assertTrue(models[0].fields[1].is_relation)

    def test_multi_line_manager_call_does_not_leak_its_arguments(self) -> None:
        # The skip must advance past the whole call, or its body lines are
        # parsed as further assignments.
        src = (
            "from django.db import models\n\n"
            "class Note(models.Model):\n"
            "    objects = models.Manager(\n"
            "        include_deleted=False,\n"
            "    )\n"
            "    text = models.TextField()\n"
        )
        models = parse_models_file("notes/models.py", src)
        self.assertEqual([f.name for f in models[0].fields], ["text"])

    def test_drift_reports_no_blocking_entry_for_a_default_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "notes"
            migrations = app / "migrations"
            migrations.mkdir(parents=True)
            (app / "models.py").write_text(MODELS, encoding="utf-8")
            (migrations / "0001_initial.py").write_text(MIGRATION, encoding="utf-8")

            report = detect_drift(str(root))
            self.assertEqual(report.blocking_count, 0)
            self.assertEqual(report.drifted, [])
