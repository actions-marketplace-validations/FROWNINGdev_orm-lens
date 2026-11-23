"""Regression tests for #341: a base bound by ``get_model("app", "Model")``.

django-oscar never imports its model classes. ``offer/benefits.py`` binds
``Benefit = get_model("offer", "Benefit")``, subclasses it, and
``offer/models.py`` star-imports the module so Django registers those
subclasses. orm-lens neither read the star-imported module nor resolved the
``get_model`` binding, so all twelve benefit and condition proxies were lost.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orm_lens.models import ParsedModel
from orm_lens.parser import scan_workspace

MODELS = (
    "from django.db import models\n"
    "\n"
    "from offer.benefits import *  # noqa\n"
    "\n"
    "\n"
    "class Benefit(models.Model):\n"
    "    value = models.DecimalField(max_digits=12, decimal_places=2)\n"
)


def _benefits(lookup: str) -> str:
    return (
        "from oscar.core.loading import get_model\n"
        "\n"
        f"Benefit = {lookup}\n"
        "\n"
        '__all__ = ["PercentageDiscountBenefit", "MultiBuyBenefit"]\n'
        "\n"
        "\n"
        "class PercentageDiscountBenefit(Benefit):\n"
        "    class Meta:\n"
        "        proxy = True\n"
        "\n"
        "\n"
        "class MultiBuyBenefit(Benefit):\n"
        "    pass\n"
    )


def _scan(root: Path, lookup: str) -> dict[str, ParsedModel]:
    app = root / "offer"
    app.mkdir(exist_ok=True)
    (app / "__init__.py").write_text("")
    (app / "models.py").write_text(MODELS)
    (app / "benefits.py").write_text(_benefits(lookup))
    index = scan_workspace(str(root))
    return {m.name: m for a in index.apps for m in a.models}


class GetModelBaseTests(unittest.TestCase):
    def test_subclasses_in_a_star_imported_module_are_models(self) -> None:
        with TemporaryDirectory() as tmp:
            models = _scan(Path(tmp), 'get_model("offer", "Benefit")')
        self.assertEqual(
            sorted(models), ["Benefit", "MultiBuyBenefit", "PercentageDiscountBenefit"]
        )
        proxy = models["PercentageDiscountBenefit"]
        self.assertEqual(proxy.app_name, "offer")
        self.assertEqual(proxy.meta.get("proxy"), "True")

    def test_get_model_base_resolves_to_the_concrete_parent(self) -> None:
        with TemporaryDirectory() as tmp:
            models = _scan(Path(tmp), 'loading.get_model("offer", "Benefit")')
        self.assertEqual(models["MultiBuyBenefit"].implicit_parent_links, {"benefit_ptr"})

    def test_appconfig_label_names_the_app(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "offer").mkdir()
            (root / "offer" / "apps.py").write_text(
                "from django.apps import AppConfig\n"
                "\n"
                "\n"
                "class OfferConfig(AppConfig):\n"
                '    name = "offer"\n'
                '    label = "shop_offer"\n'
            )
            models = _scan(root, "get_model('shop_offer', 'benefit')")
        self.assertEqual(models["MultiBuyBenefit"].implicit_parent_links, {"benefit_ptr"})

    def test_non_literal_arguments_are_ignored(self) -> None:
        with TemporaryDirectory() as tmp:
            models = _scan(Path(tmp), "get_model(app_label, model_name)")
        self.assertEqual(models["MultiBuyBenefit"].implicit_parent_links, set())

    def test_a_label_of_another_app_does_not_resolve(self) -> None:
        with TemporaryDirectory() as tmp:
            models = _scan(Path(tmp), 'get_model("voucher", "Benefit")')
        self.assertEqual(models["MultiBuyBenefit"].implicit_parent_links, set())


if __name__ == "__main__":
    unittest.main()


def _scan_files(root: Path, files: dict[str, str]) -> dict[str, ParsedModel]:
    app = root / "offer"
    app.mkdir(exist_ok=True)
    (app / "__init__.py").write_text("")
    for name, content in files.items():
        (app / name).write_text(content)
    index = scan_workspace(str(root))
    return {m.name: m for a in index.apps for m in a.models}


# The alias differs from the target name, so a base found by name alone
# cannot stand in for the get_model binding being resolved.
BOUND_BENEFIT_SUBCLASS = "\n\nclass MultiBuyBenefit(BoundBenefit):\n    pass\n"


class GetModelBaseReviewTests(unittest.TestCase):
    def test_a_get_model_call_split_over_lines_is_resolved(self) -> None:
        benefits = (
            "from oscar.core.loading import get_model\n"
            "\n"
            "BoundBenefit = get_model(\n"
            '    "offer",\n'
            '    "Benefit",\n'
            ")\n" + BOUND_BENEFIT_SUBCLASS
        )
        with TemporaryDirectory() as tmp:
            models = _scan_files(Path(tmp), {"models.py": MODELS, "benefits.py": benefits})
        self.assertIn("MultiBuyBenefit", models)
        self.assertEqual(models["MultiBuyBenefit"].implicit_parent_links, {"benefit_ptr"})

    def test_a_star_import_inside_a_string_is_not_read(self) -> None:
        models_py = (
            '"""Example:\n'
            "\n"
            "from offer.examples import *\n"
            '"""\n'
            "from django.db import models\n"
            "\n"
            "\n"
            "class Benefit(models.Model):\n"
            "    value = models.IntegerField()\n"
        )
        examples = "from django.db import models\n\n\nclass ExampleModel(models.Model):\n    pass\n"
        with TemporaryDirectory() as tmp:
            models = _scan_files(Path(tmp), {"models.py": models_py, "examples.py": examples})
        self.assertEqual(sorted(models), ["Benefit"])

    def test_a_get_model_target_found_through_an_ordinary_import_resolves(self) -> None:
        models_py = (
            "from django.db import models\n"
            "\n"
            "from offer.benefits import *  # noqa\n"
            "from .entities import Benefit\n"
            "\n"
            "\n"
            "class Special(Benefit):\n"
            "    pass\n"
        )
        entities = (
            "from django.db import models\n"
            "\n"
            "\n"
            "class Benefit(models.Model):\n"
            "    value = models.IntegerField()\n"
        )
        benefits = (
            "from oscar.core.loading import get_model\n"
            "\n"
            'BoundBenefit = get_model("offer", "Benefit")\n' + BOUND_BENEFIT_SUBCLASS
        )
        with TemporaryDirectory() as tmp:
            models = _scan_files(
                Path(tmp),
                {"models.py": models_py, "entities.py": entities, "benefits.py": benefits},
            )
        self.assertIn("MultiBuyBenefit", models)
        self.assertEqual(models["MultiBuyBenefit"].implicit_parent_links, {"benefit_ptr"})
