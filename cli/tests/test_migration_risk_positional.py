"""``migration-risk`` reads operation arguments by position too.

Hand-written migrations often pass ``AddField("item", "code", field)``
instead of the keywords ``makemigrations`` writes. Both forms must give the
same findings — before, the positional form lost both critical ones.
"""

from __future__ import annotations

import ast
from pathlib import Path

from orm_lens.migrations_parser import _get_kwarg, analyze_migration_risks

INITIAL = '''from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Item",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("old", models.CharField(max_length=10)),
            ],
        ),
    ]
'''

KEYWORDS = '''from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shop", "0001_initial")]
    operations = [
        migrations.AddField(
            model_name="item", name="code", field=models.CharField(max_length=10),
        ),
        migrations.AlterField(
            model_name="item", name="old", field=models.IntegerField(),
        ),
        migrations.AddIndex(
            model_name="item", index=models.Index(fields=["code"], name="item_code_idx"),
        ),
        migrations.RemoveField(model_name="item", name="old"),
        migrations.DeleteModel(name="Item"),
    ]
'''

POSITIONAL = '''from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shop", "0001_initial")]
    operations = [
        migrations.AddField("item", "code", models.CharField(max_length=10)),
        migrations.AlterField("item", "old", models.IntegerField()),
        migrations.AddIndex("item", models.Index(fields=["code"], name="item_code_idx")),
        migrations.RemoveField("item", "old"),
        migrations.DeleteModel("Item"),
    ]
'''

MODELS = '''from django.db import models


class Item(models.Model):
    code = models.CharField(max_length=10)
'''


def _findings(root: Path, second: str) -> set[tuple[str, str, str | None, str | None]]:
    mig = root / "shop" / "migrations"
    mig.mkdir(parents=True)
    (root / "shop" / "__init__.py").write_text("", encoding="utf-8")
    (root / "shop" / "models.py").write_text(MODELS, encoding="utf-8")
    (mig / "__init__.py").write_text("", encoding="utf-8")
    (mig / "0001_initial.py").write_text(INITIAL, encoding="utf-8")
    (mig / "0002_change.py").write_text(second, encoding="utf-8")
    return {
        (r.rule, r.severity, r.model, r.field)
        for r in analyze_migration_risks(str(root))
        if r.migration == "0002_change"
    }


def _call(src: str) -> ast.Call:
    node = ast.parse(src).body[0]
    assert isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    return node.value


def test_positional_and_keyword_forms_give_the_same_findings(tmp_path: Path) -> None:
    keywords = _findings(tmp_path / "kw", KEYWORDS)
    positional = _findings(tmp_path / "pos", POSITIONAL)
    assert positional == keywords


def test_positional_form_keeps_the_critical_findings(tmp_path: Path) -> None:
    rules = {(rule, severity) for rule, severity, _, _ in _findings(tmp_path, POSITIONAL)}
    assert ("add_field_not_null_no_default", "critical") in rules
    assert ("delete_model_still_referenced", "critical") in rules
    assert not any(rule.endswith("_unverified") for rule, _ in rules)


def test_keyword_wins_over_a_positional_value() -> None:
    call = _call('migrations.AddField("item", name="code")')
    node = _get_kwarg(call, "name")
    assert isinstance(node, ast.Constant) and node.value == "code"
    node = _get_kwarg(call, "model_name")
    assert isinstance(node, ast.Constant) and node.value == "item"
    assert _get_kwarg(call, "field") is None


def test_field_constructors_are_not_read_positionally() -> None:
    # ``CharField("Label")`` passes verbose_name, never ``default`` or ``null``.
    call = _call('models.CharField("Label", max_length=5)')
    assert _get_kwarg(call, "default") is None
    assert _get_kwarg(call, "null") is None
