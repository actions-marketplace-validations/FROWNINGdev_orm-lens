"""Tests for third-party tree base columns (treebeard and tree-queries)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orm_lens.drift import detect_drift
from orm_lens.parser import scan_workspace

HEADER = "from django.db import migrations, models\n\n\n"


def _write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _migration(body: str) -> str:
    return (
        f"{HEADER}class Migration(migrations.Migration):\n"
        "    dependencies = []\n"
        f"    operations = [\n{body}    ]\n"
    )


class TreeBaseColumnsTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)

    def _run_drift(self, files: dict[str, str]) -> list:
        _write_files(self.root, files)
        return detect_drift(self.root).drifted

    def test_mp_node_contributes_treebeard_columns_without_drift(self) -> None:
        drifts = self._run_drift(
            {
                "cats/models.py": (
                    "from django.db import models\n"
                    "from treebeard.mp_tree import MP_Node\n\n"
                    "class Category(MP_Node):\n"
                    "    name = models.CharField(max_length=100)\n"
                ),
                "cats/migrations/0001_initial.py": _migration(
                    '        migrations.CreateModel(name="Category", fields=[\n'
                    '            ("id", models.AutoField(primary_key=True)),\n'
                    '            ("path", models.CharField(max_length=255, unique=True)),\n'
                    '            ("depth", models.PositiveIntegerField()),\n'
                    '            ("numchild", models.PositiveIntegerField(default=0)),\n'
                    '            ("name", models.CharField(max_length=100)),\n'
                    "        ]),\n"
                ),
            }
        )
        self.assertEqual(drifts, [])

    def test_ns_node_contributes_treebeard_columns_without_drift(self) -> None:
        drifts = self._run_drift(
            {
                "cats/models.py": (
                    "from django.db import models\n"
                    "from treebeard.ns_tree import NS_Node\n\n"
                    "class Category(NS_Node):\n"
                    "    name = models.CharField(max_length=100)\n"
                ),
                "cats/migrations/0001_initial.py": _migration(
                    '        migrations.CreateModel(name="Category", fields=[\n'
                    '            ("id", models.AutoField(primary_key=True)),\n'
                    '            ("lft", models.PositiveIntegerField(db_index=True)),\n'
                    '            ("rgt", models.PositiveIntegerField(db_index=True)),\n'
                    '            ("tree_id", models.PositiveIntegerField(db_index=True)),\n'
                    '            ("depth", models.PositiveIntegerField(db_index=True)),\n'
                    '            ("name", models.CharField(max_length=100)),\n'
                    "        ]),\n"
                ),
            }
        )
        self.assertEqual(drifts, [])

    def test_tree_node_contributes_parent_without_drift(self) -> None:
        drifts = self._run_drift(
            {
                "cats/models.py": (
                    "from django.db import models\n"
                    "from tree_queries.models import TreeNode\n\n"
                    "class Category(TreeNode):\n"
                    "    name = models.CharField(max_length=100)\n"
                ),
                "cats/migrations/0001_initial.py": _migration(
                    '        migrations.CreateModel(name="Category", fields=[\n'
                    '            ("id", models.AutoField(primary_key=True)),\n'
                    '            ("parent", models.ForeignKey(blank=True, null=True, on_delete=models.CASCADE, related_name="children", to="cats.category")),\n'
                    '            ("name", models.CharField(max_length=100)),\n'
                    "        ]),\n"
                ),
            }
        )
        self.assertEqual(drifts, [])

    def test_local_tree_node_does_not_get_parent_field(self) -> None:
        _write_files(
            self.root,
            {
                "cats/models.py": (
                    "from django.db import models\n\n"
                    "class TreeNode(models.Model):\n"
                    "    class Meta:\n"
                    "        abstract = True\n\n"
                    "class Category(TreeNode):\n"
                    "    name = models.CharField(max_length=100)\n"
                ),
            },
        )
        index = scan_workspace(str(self.root))
        category = next(
            m for app in index.apps for m in app.models if m.name == "Category"
        )
        self.assertEqual([f.name for f in category.fields], ["name"])
        self.assertEqual([f.name for f in category.all_fields()], ["name"])
