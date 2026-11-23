"""Regression tests for issue #330 — django-solo, django-tree-queries and django-treebeard bases."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orm_lens.parser import scan_workspace


class ThirdPartyModelBasesTest(unittest.TestCase):
    def test_solo_tree_queries_and_treebeard_bases_are_recognised(self) -> None:
        src = (
            "from django.db import models\n"
            "from solo.models import SingletonModel\n"
            "from tree_queries.models import TreeNode\n"
            "from treebeard.mp_tree import MP_Node\n"
            "from treebeard.ns_tree import NS_Node\n"
            "from treebeard.al_tree import AL_Node\n\n"
            "class SiteConfiguration(SingletonModel):\n"
            "    site_name = models.CharField(max_length=255)\n\n"
            "class TestPlan(TreeNode):\n"
            "    name = models.CharField(max_length=255)\n\n"
            "class Category(MP_Node):\n"
            "    name = models.CharField(max_length=100)\n\n"
            "class NestedCategory(NS_Node):\n"
            "    title = models.CharField(max_length=100)\n\n"
            "class AdjacencyCategory(AL_Node):\n"
            "    label = models.CharField(max_length=100)\n"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models_py = root / "plans" / "models.py"
            models_py.parent.mkdir(parents=True)
            models_py.write_text(src, encoding="utf-8")

            index = scan_workspace(str(root))
            models_ = [model for app in index.apps for model in app.models]
            by_name = {m.name: [f.name for f in m.fields] for m in models_}
            self.assertEqual(
                by_name,
                {
                    "SiteConfiguration": ["site_name"],
                    "TestPlan": ["name"],
                    "Category": ["name"],
                    "NestedCategory": ["title"],
                    "AdjacencyCategory": ["label"],
                },
            )

    def test_local_treenode_without_django_base_is_not_a_model(self) -> None:
        src = (
            "class TreeNode:\n"
            "    pass\n\n"
            "class PlainHelper(TreeNode):\n"
            "    value = 42\n"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            models_py = root / "plans" / "models.py"
            models_py.parent.mkdir(parents=True)
            models_py.write_text(src, encoding="utf-8")

            index = scan_workspace(str(root))
            models_ = [model for app in index.apps for model in app.models]
            self.assertEqual(models_, [])


if __name__ == "__main__":
    unittest.main()
