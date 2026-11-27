"""``class Meta: abstract = True`` written on one line.

Python allows a class body on the header line, and some projects write short
``Meta`` classes that way. Only the lines below ``class Meta:`` were read, so a
one-line abstract base was reported as a concrete model, its fields never
reached the subclasses, and ``drift`` failed with a table no migration creates.

Run: python -m unittest discover cli/tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orm_lens.drift import detect_drift
from orm_lens.parser import parse_models_file

SOURCE = """\
from django.db import models


class Stamped(models.Model):
    created = models.DateTimeField(auto_now_add=True)

    class Meta: abstract = True


class Item(Stamped):
    name = models.CharField(max_length=5)

    class Meta: db_table = "items"; ordering = ["name"]  # trailing comment


class ItemProxy(Item):
    class Meta(Item.Meta): proxy = True
"""

MIGRATION = """\
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Item",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("name", models.CharField(max_length=5)),
            ],
        ),
        migrations.CreateModel(
            name="ItemProxy",
            fields=[],
            options={"proxy": True},
            bases=("shop.item",),
        ),
    ]
"""


class InlineMetaTest(unittest.TestCase):
    def test_one_line_abstract_base_is_not_a_model(self) -> None:
        models = {m.name: m for m in parse_models_file("shop/models.py", SOURCE)}
        self.assertEqual(sorted(models), ["Item", "ItemProxy"])
        self.assertEqual(
            [f.name for f in models["Item"].all_fields()], ["created", "name"]
        )

    def test_one_line_meta_options_are_read(self) -> None:
        models = {m.name: m for m in parse_models_file("shop/models.py", SOURCE)}
        self.assertEqual(
            models["Item"].meta, {"db_table": '"items"', "ordering": '["name"]'}
        )
        self.assertEqual(models["ItemProxy"].meta, {"proxy": "True"})

    def test_drift_does_not_flag_a_one_line_abstract_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "shop"
            (app / "migrations").mkdir(parents=True)
            (app / "__init__.py").write_text("", encoding="utf-8")
            (app / "migrations" / "__init__.py").write_text("", encoding="utf-8")
            (app / "models.py").write_text(SOURCE, encoding="utf-8")
            (app / "migrations" / "0001_initial.py").write_text(
                MIGRATION, encoding="utf-8"
            )
            report = detect_drift(tmp)
        self.assertEqual([d.label for d in report.drifted], [])


if __name__ == "__main__":
    unittest.main()
