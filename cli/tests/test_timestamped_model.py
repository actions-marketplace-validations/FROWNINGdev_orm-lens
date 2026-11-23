"""Regression tests for #336: ``created`` / ``modified`` from ``TimeStampedModel``.

django-extensions and django-model-utils both ship an abstract
``TimeStampedModel`` with those two fields. It lives in site-packages, so a
workspace scan never reads it, and every subclass lost both columns: ``drift``
reported them as "migrated but no longer declared".
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orm_lens.drift import detect_drift
from orm_lens.models import ParsedModel
from orm_lens.parser import parse_models_file

AUDIT_LOG = '''
from django.db import models
from django_extensions.db.models import TimeStampedModel


class AuditLog(TimeStampedModel):
    action = models.CharField(max_length=32)
'''

# What ``makemigrations`` writes for AUDIT_LOG.
INITIAL_MIGRATION = '''
import django_extensions.db.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", django_extensions.db.fields.CreationDateTimeField(auto_now_add=True, verbose_name="created")),
                ("modified", django_extensions.db.fields.ModificationDateTimeField(auto_now=True, verbose_name="modified")),
                ("action", models.CharField(max_length=32)),
            ],
        ),
    ]
'''


def _model(source: str, name: str = "AuditLog") -> ParsedModel:
    return next(m for m in parse_models_file("audit/models.py", source) if m.name == name)


class TimeStampedModelTest(unittest.TestCase):
    def test_subclass_gets_created_and_modified(self) -> None:
        self.assertEqual(
            [(f.name, f.inherited_from) for f in _model(AUDIT_LOG).all_fields()],
            [
                ("created", "TimeStampedModel"),
                ("modified", "TimeStampedModel"),
                ("action", None),
            ],
        )

    def test_model_utils_base_gets_the_same_fields(self) -> None:
        model = _model(AUDIT_LOG.replace("django_extensions.db.models", "model_utils.models"))
        self.assertEqual([f.name for f in model.inherited_fields], ["created", "modified"])

    def test_a_workspace_class_of_the_same_name_wins(self) -> None:
        model = _model(
            "from django.db import models\n"
            "\n"
            "class TimeStampedModel(models.Model):\n"
            "    created_at = models.DateTimeField(auto_now_add=True)\n"
            "\n"
            "    class Meta:\n"
            "        abstract = True\n"
            "\n"
            "class AuditLog(TimeStampedModel):\n"
            "    action = models.CharField(max_length=32)\n"
        )
        self.assertEqual([f.name for f in model.inherited_fields], ["created_at"])

    def test_the_initial_migration_is_not_drift(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "audit" / "migrations").mkdir(parents=True)
            (root / "audit" / "models.py").write_text(AUDIT_LOG, encoding="utf-8")
            (root / "audit" / "migrations" / "__init__.py").write_text("", encoding="utf-8")
            (root / "audit" / "migrations" / "0001_initial.py").write_text(
                INITIAL_MIGRATION, encoding="utf-8"
            )
            self.assertEqual(detect_drift(str(root)).to_dict()["drifted"], [])


if __name__ == "__main__":
    unittest.main()
