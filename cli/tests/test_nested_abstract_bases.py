"""Regression test for issue #309: an abstract model nested in another class.

Ralph declares ``NamedMixin.NonUnique``, an abstract model inside the
``NamedMixin`` class body, and its models inherit from it by that dotted name.
Only module-level classes were collected and the dotted base was read as
``module.Class``, so ``name`` never reached ``AssetHolder`` and ``drift``
reported it as "migrated but no longer declared".
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orm_lens.parser import _collect_defs, scan_workspace

NAMED_MIXIN = """\
from django.db import models


class NamedMixin(models.Model):
    name = models.CharField(max_length=255, unique=True)

    class Meta:
        abstract = True

    class NonUnique(models.Model):
        name = models.CharField(max_length=75, db_index=True)

        class Meta:
            abstract = True
"""

ASSETS = """\
from django.db import models

from lib.mixins import NamedMixin


class AssetHolder(NamedMixin.NonUnique, models.Model):
    pass


class Manufacturer(NamedMixin, models.Model):
    pass
"""

# The same shape inside one module, plus nested classes that are not abstract
# models and must stay out of the scan. The nested body holds a column-0 comment
# and a field through a renamed import, which must not end or empty it.
PLACES = """\
from django.db import models
from django.db.models import CharField as Text


class Labelled(models.Model):
    class Meta:
        abstract = True

    class Short(models.Model):
        label = models.CharField(max_length=20)
# kept short for the map pins
        code = Text(max_length=4)

        class Meta:
            abstract = True

    class Kind(models.TextChoices):
        CITY = "city"


class Spot(Labelled.Short, models.Model):
    pass
"""


def write_project(root: Path) -> None:
    for rel, text in {
        "lib/__init__.py": "",
        "lib/mixins.py": NAMED_MIXIN,
        "assets/__init__.py": "",
        "assets/models.py": ASSETS,
        "places/__init__.py": "",
        "places/models.py": PLACES,
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class NestedAbstractBaseTest(unittest.TestCase):
    def setUp(self) -> None:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        write_project(Path(td.name))
        index = scan_workspace(td.name)
        self.models = {m.name: m for a in index.apps for m in a.models}

    def inherited(self, name: str) -> list[tuple[str, str, str | None]]:
        return [
            (f.name, f.args, f.inherited_from)
            for f in self.models[name].inherited_fields
        ]

    def test_nested_base_from_another_module(self) -> None:
        self.assertEqual(
            self.inherited("AssetHolder"),
            [("name", "max_length=75, db_index=True", "NamedMixin.NonUnique")],
        )

    def test_outer_base_keeps_its_own_field(self) -> None:
        self.assertEqual(
            self.inherited("Manufacturer"),
            [("name", "max_length=255, unique=True", "NamedMixin")],
        )

    def test_nested_base_in_the_same_module(self) -> None:
        self.assertEqual(
            self.inherited("Spot"),
            [
                ("label", "max_length=20", "Labelled.Short"),
                ("code", "max_length=4", "Labelled.Short"),
            ],
        )

    def test_nested_classes_are_not_listed(self) -> None:
        self.assertEqual(sorted(self.models), ["AssetHolder", "Manufacturer", "Spot"])

    def test_outer_class_fields_do_not_include_the_nested_ones(self) -> None:
        defs = {d.name: d for d in _collect_defs("lib/mixins.py", NAMED_MIXIN)}
        self.assertEqual(sorted(defs), ["NamedMixin", "NamedMixin.NonUnique"])
        self.assertEqual(
            [(f.name, f.args) for f in defs["NamedMixin"].fields],
            [("name", "max_length=255, unique=True")],
        )
        nested = defs["NamedMixin.NonUnique"]
        self.assertEqual(nested.line_number, 9)
        self.assertEqual(nested.fields[0].line_number, 10)


def nested_variant(header: str, body: str) -> str:
    """``lib/mixins.py`` with ``NonUnique`` written another way."""
    return (
        "from django.db import models\n\n\n"
        "class NamedMixin(models.Model):\n"
        "    class Meta:\n"
        "        abstract = True\n\n"
        f"{header}\n{body}"
        "        class Meta:\n"
        "            abstract = True\n"
    )


class NestedBaseShapesTest(unittest.TestCase):
    """Other ways of writing the nested base, each with AssetHolder's result."""

    def inherited(self, mixins: str) -> list[tuple[str, str | None]]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, text in {
                "lib/__init__.py": "",
                "lib/mixins.py": mixins,
                "assets/__init__.py": "",
                "assets/models.py": ASSETS,
            }.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text, encoding="utf-8")
            models = {m.name: m for a in scan_workspace(tmp).apps for m in a.models}
            return [(f.name, f.inherited_from) for f in models["AssetHolder"].inherited_fields]

    def test_header_wrapped_over_lines(self) -> None:
        mixins = nested_variant(
            "    class NonUnique(\n        models.Model,\n    ):",
            "        name = models.CharField(max_length=75)\n\n",
        )
        self.assertEqual(self.inherited(mixins), [("name", "NamedMixin.NonUnique")])

    def test_base_naming_a_sibling_nested_class(self) -> None:
        mixins = nested_variant(
            "    class Common(models.Model):\n"
            "        name = models.CharField(max_length=75)\n\n"
            "        class Meta:\n"
            "            abstract = True\n\n"
            "    class NonUnique(Common):",
            "\n",
        )
        self.assertEqual(self.inherited(mixins), [("name", "NamedMixin.Common")])

    def test_base_named_like_the_class_is_the_outer_scope_one(self) -> None:
        # ``class NonUnique(NonUnique)`` reads the module's NonUnique: the
        # nested name is bound only after its bases are read.
        mixins = (
            "from django.db import models\n\n\n"
            "class NonUnique(models.Model):\n"
            "    name = models.CharField(max_length=75)\n\n"
            "    class Meta:\n"
            "        abstract = True\n\n\n"
            "class NamedMixin(models.Model):\n"
            "    class Meta:\n"
            "        abstract = True\n\n"
            "    class NonUnique(NonUnique):\n"
            "        class Meta:\n"
            "            abstract = True\n"
        )
        self.assertEqual(self.inherited(mixins), [("name", "NonUnique")])

    def test_base_naming_a_sibling_that_is_not_a_model(self) -> None:
        # The nested ``Common`` shadows the module's abstract ``Common``, and
        # is no model, so NonUnique gives no fields.
        mixins = (
            "from django.db import models\n\n\n"
            "class Common(models.Model):\n"
            "    name = models.CharField(max_length=75)\n\n"
            "    class Meta:\n"
            "        abstract = True\n\n\n"
            "class NamedMixin(models.Model):\n"
            "    class Meta:\n"
            "        abstract = True\n\n"
            "    class Common(object):\n"
            "        pass\n\n"
            "    class NonUnique(Common, models.Model):\n"
            "        class Meta:\n"
            "            abstract = True\n"
        )
        self.assertEqual(self.inherited(mixins), [])

    def test_plain_object_base_is_not_a_model(self) -> None:
        mixins = nested_variant(
            "    class NonUnique(object):",
            "        name = models.CharField(max_length=75)\n\n",
        )
        self.assertEqual(self.inherited(mixins), [])


class DeeperNestingTest(unittest.TestCase):
    """Nested bases resolved inside one scanned ``lib/models.py``."""

    def inherited(self, models_py: str) -> list[tuple[str, str | None]]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, text in {"lib/__init__.py": "", "lib/models.py": models_py}.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text, encoding="utf-8")
            models = {m.name: m for a in scan_workspace(tmp).apps for m in a.models}
            return [(f.name, f.inherited_from) for f in models["Consumer"].inherited_fields]

    def test_base_naming_a_sibling_in_its_own_body(self) -> None:
        models_py = (
            "from django.db import models\n\n\n"
            "class Outer:\n"
            "    class Wrapper:\n"
            "        class Named(models.Model):\n"
            "            name = models.CharField(max_length=10)\n\n"
            "            class Meta:\n"
            "                abstract = True\n\n"
            "        class Child(Named):\n"
            "            class Meta:\n"
            "                abstract = True\n\n\n"
            "class Consumer(Outer.Wrapper.Child, models.Model):\n"
            "    pass\n"
        )
        self.assertEqual(self.inherited(models_py), [("name", "Outer.Wrapper.Named")])

    def test_enclosing_class_name_is_not_in_scope(self) -> None:
        # Inside Outer.Wrapper's body ``Wrapper`` is not bound yet and Outer's
        # body is not visible, so ``Wrapper.Base`` is the module's.
        models_py = (
            "from django.db import models\n\n\n"
            "class Wrapper:\n"
            "    class Base(models.Model):\n"
            "        name = models.CharField(max_length=10)\n\n"
            "        class Meta:\n"
            "            abstract = True\n\n\n"
            "class Outer:\n"
            "    class Wrapper:\n"
            "        class Child(Wrapper.Base):\n"
            "            class Meta:\n"
            "                abstract = True\n\n\n"
            "class Consumer(Outer.Wrapper.Child, models.Model):\n"
            "    pass\n"
        )
        self.assertEqual(self.inherited(models_py), [("name", "Wrapper.Base")])

    def test_non_model_sibling_shadows_an_abstract_model_of_the_module(self) -> None:
        # The module's abstract Common is scanned too, so it is a candidate by
        # name; NonUnique's base is still the plain nested Common.
        models_py = (
            "from django.db import models\n\n\n"
            "class Common(models.Model):\n"
            "    name = models.CharField(max_length=75)\n\n"
            "    class Meta:\n"
            "        abstract = True\n\n\n"
            "class NamedMixin(models.Model):\n"
            "    class Meta:\n"
            "        abstract = True\n\n"
            "    class Common(object):\n"
            "        pass\n\n"
            "    class NonUnique(Common, models.Model):\n"
            "        class Meta:\n"
            "            abstract = True\n\n\n"
            "class Consumer(NamedMixin.NonUnique, models.Model):\n"
            "    pass\n"
        )
        self.assertEqual(self.inherited(models_py), [])

    def test_non_model_sibling_two_bodies_down_shadows_it_too(self) -> None:
        models_py = (
            "from django.db import models\n\n\n"
            "class Common(models.Model):\n"
            "    name = models.CharField(max_length=75)\n\n"
            "    class Meta:\n"
            "        abstract = True\n\n\n"
            "class Outer:\n"
            "    class Wrapper:\n"
            "        class Common(object):\n"
            "            pass\n\n"
            "        class Child(Common, models.Model):\n"
            "            class Meta:\n"
            "                abstract = True\n\n\n"
            "class Consumer(Outer.Wrapper.Child, models.Model):\n"
            "    pass\n"
        )
        self.assertEqual(self.inherited(models_py), [])


if __name__ == "__main__":
    unittest.main()
