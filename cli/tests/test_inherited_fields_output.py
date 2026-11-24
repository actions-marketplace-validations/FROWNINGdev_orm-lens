"""Abstract bases' fields show up in the CLI table, markdown and ER output.

An oscar-style project declares every field on an abstract model in
``abstract_models.py`` and only ``class UserAddress(AbstractUserAddress):
pass`` in ``models.py``. Showing what the class body declares printed 0
fields and drew no relations for most of the project.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from orm_lens.cli import _build_mermaid
from orm_lens.er_formats import build_dbml
from orm_lens.formatters import format_index, format_model
from orm_lens.parser import scan_workspace

ABSTRACT = '''from django.db import models


class AbstractCountry(models.Model):
    name = models.CharField(max_length=100)

    class Meta:
        abstract = True


class AbstractAddress(models.Model):
    line1 = models.CharField(max_length=255)
    country = models.ForeignKey("shop.Country", on_delete=models.CASCADE, related_name="+")

    class Meta:
        abstract = True
'''

MODELS = '''from django.db import models

from shop.abstract_models import AbstractAddress, AbstractCountry


class Country(AbstractCountry):
    pass


class Address(AbstractAddress):
    note = models.TextField()
'''


class InheritedFieldsOutputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="orm-lens-inh-out-"))
        app = self.tmp / "shop"
        app.mkdir()
        (app / "__init__.py").write_text("", encoding="utf-8")
        (app / "abstract_models.py").write_text(ABSTRACT, encoding="utf-8")
        (app / "models.py").write_text(MODELS, encoding="utf-8")
        self.index = scan_workspace(str(self.tmp))
        self.models = {m.name: m for a in self.index.apps for m in a.models}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_table_counts_inherited_fields_and_relations(self) -> None:
        rows = {
            line.split()[1]: line.split()[2:4]
            for line in format_index(self.index, "table").splitlines()[2:]
        }
        self.assertEqual(rows["Country"], ["1", "0"])
        self.assertEqual(rows["Address"], ["2", "1"])

    def test_table_paths_are_relative_to_the_working_directory(self) -> None:
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            table = format_index(self.index, "table")
        finally:
            os.chdir(cwd)
        self.assertIn(os.path.join("shop", "models.py") + ":", table)
        self.assertNotIn(str(self.tmp), table)

    def test_model_markdown_lists_inherited_fields(self) -> None:
        md = format_model(self.models["Address"], "markdown")
        for name in ("line1", "country", "note"):
            self.assertIn(f"`{name}`", md)

    def test_mermaid_draws_inherited_columns_and_relation(self) -> None:
        mermaid = _build_mermaid(self.index)
        self.assertIn("CharField line1", mermaid)
        self.assertIn("CharField name", mermaid)
        self.assertIn('Address }o--|| Country : "country [CASCADE]"', mermaid)

    def test_mermaid_hides_disabled_reverse_accessor(self) -> None:
        self.assertNotIn("as _", _build_mermaid(self.index))

    def test_dbml_has_inherited_ref(self) -> None:
        dbml = build_dbml(self.index)
        self.assertIn("line1", dbml)
        self.assertEqual(sum(1 for line in dbml.splitlines() if line.startswith("Ref")), 1)

    def test_json_still_lists_only_declared_fields(self) -> None:
        self.assertEqual([f["name"] for f in self.models["Address"].to_dict()["fields"]], ["note"])


if __name__ == "__main__":
    unittest.main()
