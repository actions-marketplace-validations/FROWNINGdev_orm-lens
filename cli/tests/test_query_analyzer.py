"""Tests for the static QuerySet usage analyzer (suggest_indexes).

The analyzer walks every ``.py`` file in the workspace via pure ``ast``,
matches ``<Model>.objects.<qs_method>(...)`` calls, and proposes
``Meta.indexes`` entries from field-usage frequency. These tests build
synthetic Django-shaped workspaces inside ``tmp_path`` and assert the
returned shape.
"""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orm_lens.query_analyzer import suggest_indexes

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "miniapp"


def _copy_miniapp(dest: Path) -> None:
    """Copy the fixture miniapp into a fresh workspace root."""
    for app in ("orders", "users"):
        shutil.copytree(FIXTURE_ROOT / app, dest / app)


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class SuggestIndexesTest(unittest.TestCase):
    def test_single_field_filter_proposes_single_index(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(
                root / "views" / "dashboard.py",
                "from orders.models import Order\n"
                "def a():\n"
                "    return Order.objects.filter(status='paid')\n"
                "def b():\n"
                "    return Order.objects.filter(status='shipped')\n",
            )

            result = suggest_indexes("orders", "Order", str(root))

            self.assertEqual(result["target"], "orders.Order")
            fields = [u["field"] for u in result["filter_usages"] if not u.get("composite")]
            self.assertIn("status", fields)
            proposed_fieldsets = [p["fields"] for p in result["proposed_indexes"]]
            self.assertIn(["status"], proposed_fieldsets)
            # user is already in Meta.indexes → don't propose it again
            self.assertNotIn(["user"], proposed_fieldsets)
            self.assertEqual(result["existing_meta_indexes"], [["user"]])

    def test_chained_filter_counts_as_composite(self) -> None:
        """``.filter(user=x).filter(status=y)`` produces two filter sites, one
        per call. Because they're in *separate* calls they are NOT treated as
        composite — composites require field co-occurrence in one call.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(
                root / "views" / "dashboard.py",
                "from orders.models import Order\n"
                "def q():\n"
                "    return Order.objects.filter(user=1).filter(status='paid')\n"
                "def q2():\n"
                "    return Order.objects.filter(user=2).filter(status='shipped')\n",
            )

            result = suggest_indexes("orders", "Order", str(root))

            singles = {u["field"]: u["sites"] for u in result["filter_usages"] if not u.get("composite")}
            # 2 chained filter calls in each of 2 functions = 2 sites each field
            self.assertEqual(singles.get("user"), 2)
            self.assertEqual(singles.get("status"), 2)
            composites = [u for u in result["filter_usages"] if u.get("composite")]
            self.assertEqual(composites, [])

    def test_composite_filter_call_is_flagged(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(
                root / "views" / "dashboard.py",
                "from orders.models import Order\n"
                "def a():\n"
                "    return Order.objects.filter(user=1, status='paid')\n"
                "def b():\n"
                "    return Order.objects.filter(user=2, status='shipped')\n",
            )

            result = suggest_indexes("orders", "Order", str(root))

            composites = [u for u in result["filter_usages"] if u.get("composite")]
            self.assertEqual(len(composites), 1)
            self.assertEqual(composites[0]["field"], ["status", "user"])
            self.assertEqual(composites[0]["sites"], 2)
            proposed_fieldsets = [p["fields"] for p in result["proposed_indexes"]]
            self.assertIn(["status", "user"], proposed_fieldsets)

    def test_order_by_and_lookup_stripping(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(
                root / "views" / "list_view.py",
                "from orders.models import Order\n"
                "def a():\n"
                "    return Order.objects.filter(user__email='x').order_by('-created_at')\n"
                "def b():\n"
                "    return Order.objects.order_by('-created_at')\n",
            )

            result = suggest_indexes("orders", "Order", str(root))

            singles = {u["field"]: u["sites"] for u in result["filter_usages"] if not u.get("composite")}
            # user__email → root field 'user' (Meta already indexes it)
            self.assertEqual(singles.get("user"), 1)
            order_by = {u["field"]: u["sites"] for u in result["order_by_usages"]}
            self.assertEqual(order_by.get("-created_at"), 2)
            proposed_fieldsets = [p["fields"] for p in result["proposed_indexes"]]
            self.assertIn(["-created_at"], proposed_fieldsets)

    def test_advanced_flag_on_q_expression(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(
                root / "managers.py",
                "from django.db.models import Q\n"
                "from orders.models import Order\n"
                "def q():\n"
                "    return Order.objects.filter(Q(status='paid') | Q(status='shipped'))\n",
            )

            result = suggest_indexes("orders", "Order", str(root))

            # The Q() expression is opaque — analyzer records the site with
            # advanced=True and no field name. No index proposal from it.
            # Only the user index (from Meta) exists → no new proposals.
            self.assertEqual(result["existing_meta_indexes"], [["user"]])

    def test_missing_model_returns_error(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            result = suggest_indexes("orders", "NoSuchModel", str(root))
            self.assertEqual(result["target"], "orders.NoSuchModel")
            self.assertEqual(result["error"], "model not found in workspace")

    def test_get_call_is_captured(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(
                root / "api.py",
                "from orders.models import Order\n"
                "def get_one(pk):\n"
                "    return Order.objects.get(pk=pk)\n"
                "def get_two(pk):\n"
                "    return Order.objects.get(pk=pk)\n",
            )

            result = suggest_indexes("orders", "Order", str(root))
            # get sites feed into the filter/exclude/get pool for indexing.
            # ``pk`` is folded onto the real primary-key column (#60) — it is
            # an alias, not a field of its own, so counting it separately
            # would split one lookup across two rows.
            fields = {u["field"]: u["sites"] for u in result["filter_usages"] if not u.get("composite")}
            self.assertEqual(fields.get("id"), 2)
            self.assertNotIn("pk", fields)
            # …and the primary key is already indexed, so nothing is proposed.
            self.assertNotIn(["id"], [p["fields"] for p in result["proposed_indexes"]])

    def test_model_reached_through_the_models_module_is_counted(self) -> None:
        """``from orders import models`` + ``models.Order.objects.filter(...)``
        (and the fully dotted ``orders.models.Order.objects``) are filter
        sites for ``Order`` just like ``Order.objects.filter(...)``.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            _write(
                root / "views" / "dashboard.py",
                "import orders.models\n"
                "from orders import models\n"
                "def a():\n"
                "    return models.Order.objects.filter(status='paid')\n"
                "def b():\n"
                "    return orders.models.Order.objects.filter(status='shipped')\n",
            )

            result = suggest_indexes("orders", "Order", str(root))

            fields = {u["field"]: u["sites"] for u in result["filter_usages"] if not u.get("composite")}
            self.assertEqual(fields.get("status"), 2)
            self.assertIn(["status"], [p["fields"] for p in result["proposed_indexes"]])


_SHOP_MODELS = (
    "from django.db import models\n"
    "\n"
    "\n"
    "class Category(models.Model):\n"
    "    title = models.CharField(max_length=50)\n"
    "\n"
    "\n"
    "class Product(models.Model):\n"
    "    category = models.ForeignKey(Category, on_delete=models.CASCADE)\n"
    "    slug = models.SlugField()\n"
    "    external_id = models.CharField(max_length=50)\n"
    "\n"
    "    class Meta:\n"
    "        indexes = [\n"
    "            models.Index(fields=[\"category_id\", \"slug\"], name=\"product_cat_slug\"),\n"
    "        ]\n"
)


class ForeignKeyColumnAliasTest(unittest.TestCase):
    """``<fk>_id`` and ``<fk>`` are the same column (#225)."""

    def test_meta_index_on_fk_column_covers_field_name_lookups(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "shop" / "models.py", _SHOP_MODELS)
            _write(
                root / "shop" / "views.py",
                "from .models import Product\n"
                "def detail(category, slug):\n"
                "    return Product.objects.get(category=category, slug=slug)\n"
                "def exists(category, slug):\n"
                "    return Product.objects.filter(category=category, slug=slug).exists()\n",
            )

            result = suggest_indexes("shop", "Product", str(root))

            proposed = [sorted(p["fields"]) for p in result["proposed_indexes"]]
            self.assertNotIn(["category", "slug"], proposed)
            # Meta is reported as written.
            self.assertEqual(result["existing_meta_indexes"], [["category_id", "slug"]])

    def test_fk_column_and_field_name_lookups_count_as_one_field(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "shop" / "models.py", _SHOP_MODELS)
            _write(
                root / "shop" / "views.py",
                "from .models import Product\n"
                "def by_id(x):\n"
                "    return Product.objects.filter(category_id=x)\n"
                "def by_obj(y):\n"
                "    return Product.objects.filter(category=y)\n"
                "def ordered():\n"
                "    return Product.objects.order_by('-category_id')\n",
            )

            result = suggest_indexes("shop", "Product", str(root))

            fields = {u["field"]: u["sites"] for u in result["filter_usages"] if not u.get("composite")}
            self.assertEqual(fields.get("category"), 2)
            self.assertNotIn("category_id", fields)
            self.assertEqual([r["field"] for r in result["order_by_usages"]], ["-category"])

    def test_plain_field_ending_in_id_keeps_its_name(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "shop" / "models.py", _SHOP_MODELS)
            _write(
                root / "shop" / "views.py",
                "from .models import Product\n"
                "def a(x):\n"
                "    return Product.objects.filter(external_id=x)\n"
                "def b(x):\n"
                "    return Product.objects.filter(external_id=x)\n",
            )

            result = suggest_indexes("shop", "Product", str(root))

            fields = {u["field"]: u["sites"] for u in result["filter_usages"] if not u.get("composite")}
            self.assertEqual(fields.get("external_id"), 2)
            self.assertIn(["external_id"], [p["fields"] for p in result["proposed_indexes"]])


if __name__ == "__main__":
    unittest.main()
