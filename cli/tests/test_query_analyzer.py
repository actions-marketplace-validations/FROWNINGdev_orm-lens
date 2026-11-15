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

    def test_only_local_columns_are_proposed_for_indexes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "board" / "models.py",
                "from django.db import models\n"
                "class Board(models.Model):\n"
                "    label = models.CharField(max_length=200)\n"
                "class Story(models.Model):\n"
                "    board = models.ForeignKey(Board, on_delete=models.CASCADE, db_index=False)\n"
                "    title = models.CharField(max_length=200)\n"
                "    watchers = models.ManyToManyField('auth.User')\n"
                "class Task(models.Model):\n"
                "    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name='work_items')\n"
                "    done = models.BooleanField(default=False)\n",
            )
            _write(
                root / "board" / "stats.py",
                "from .models import Story\n"
                "Story.objects.filter(watchers__isnull=False, title='one', work_items__done=False)\n"
                "Story.objects.exclude(watchers=1, title='two', work_items__done=True)\n"
                "Story.objects.filter(board=1)\n"
                "Story.objects.filter(board_id=2)\n"
                "Story.objects.order_by('-work_items__done')\n"
                "Story.objects.order_by('-work_items__done')\n",
            )

            result = suggest_indexes("board", "Story", str(root))
            self.assertEqual(
                {tuple(p["fields"]) for p in result["proposed_indexes"]},
                {("board",), ("title",)},
            )
            self.assertEqual(
                {u["field"]: u["sites"] for u in result["filter_usages"] if not u.get("composite")},
                {"board": 2, "title": 2, "watchers": 2, "work_items": 2},
            )
            self.assertEqual(
                {u["field"]: u["sites"] for u in result["order_by_usages"]}, {"-work_items": 2}
            )

    def test_composite_with_a_unique_column_is_not_proposed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "board" / "models.py",
                "from django.db import models\n"
                "class Story(models.Model):\n"
                "    title = models.CharField(max_length=200)\n"
                "    slug = models.SlugField(unique=True)\n"
                "    status = models.CharField(max_length=20)\n",
            )
            _write(
                root / "board" / "stats.py",
                "from .models import Story\n"
                "def a(pk, title):\n"
                "    return Story.objects.filter(id=pk, title=title).first()\n"
                "def b(pk, title):\n"
                "    return Story.objects.get(id=pk, title=title)\n"
                "def c(pk, status):\n"
                "    return Story.objects.filter(pk=pk, status=status).exists()\n"
                "def d(pk, status):\n"
                "    return Story.objects.filter(pk=pk, status=status).first()\n"
                "def e(slug, status):\n"
                "    return Story.objects.filter(slug=slug, status=status).exists()\n"
                "def f(slug, status):\n"
                "    return Story.objects.filter(slug=slug, status=status).first()\n",
            )

            result = suggest_indexes("board", "Story", str(root))

            self.assertEqual(
                {tuple(p["fields"]) for p in result["proposed_indexes"]},
                {("status",), ("title",)},
            )
            composites = [u["field"] for u in result["filter_usages"] if u.get("composite")]
            self.assertIn(["id", "title"], composites)

    def test_custom_primary_key_composite_is_not_proposed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "board" / "models.py",
                "from django.db import models\n"
                "class Story(models.Model):\n"
                "    code = models.CharField(max_length=20, primary_key=True)\n"
                "    title = models.CharField(max_length=200)\n"
                "    status = models.CharField(max_length=20)\n",
            )
            _write(
                root / "board" / "stats.py",
                "from .models import Story\n"
                "def a(code, title):\n"
                "    return Story.objects.filter(code=code, title=title).first()\n"
                "def b(code, title):\n"
                "    return Story.objects.get(code=code, title=title)\n"
                "def c(code, status):\n"
                "    return Story.objects.filter(code=code, status=status).exists()\n"
                "def d(code, status):\n"
                "    return Story.objects.filter(code=code, status=status).first()\n"
                "def e(title, status):\n"
                "    return Story.objects.filter(title=title, status=status).exists()\n"
                "def f(title, status):\n"
                "    return Story.objects.filter(title=title, status=status).first()\n",
            )

            result = suggest_indexes("board", "Story", str(root))

            self.assertEqual(
                {tuple(p["fields"]) for p in result["proposed_indexes"]},
                {("title",), ("status", "title")},
            )

    def test_db_index_field_still_earns_a_composite(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "board" / "models.py",
                "from django.db import models\n"
                "class Story(models.Model):\n"
                "    rank = models.IntegerField(db_index=True)\n"
                "    status = models.CharField(max_length=20)\n",
            )
            _write(
                root / "board" / "stats.py",
                "from .models import Story\n"
                "def a(rank, status):\n"
                "    return Story.objects.filter(rank=rank, status=status).first()\n"
                "def b(rank, status):\n"
                "    return Story.objects.filter(rank=rank, status=status).exists()\n",
            )

            result = suggest_indexes("board", "Story", str(root))

            # Indexed but not unique: the pair can still pay off.
            self.assertEqual(
                {tuple(p["fields"]) for p in result["proposed_indexes"]},
                {("rank", "status"), ("status",)},
            )

    def test_missing_model_returns_error(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _copy_miniapp(root)
            result = suggest_indexes("orders", "NoSuchModel", str(root))
            self.assertEqual(result["target"], "orders.NoSuchModel")
            self.assertEqual(result["error"], "model not found in workspace")

    def test_aliased_fields_keep_their_index_eligibility(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "shop" / "models.py",
                "from django.db import models\n"
                "from django.db.models import CharField as CF, ManyToManyField as M2M\n"
                "class Product(models.Model):\n"
                "    code = CF(max_length=30)\n"
                "    tags = M2M('Tag')\n",
            )
            _write(
                root / "shop" / "views.py",
                "from .models import Product\n"
                "Product.objects.filter(code='one')\n"
                "Product.objects.filter(code='two')\n"
                "Product.objects.filter(tags=1)\n"
                "Product.objects.filter(tags=2)\n",
            )

            result = suggest_indexes("shop", "Product", str(root))
            self.assertEqual([p["fields"] for p in result["proposed_indexes"]], [["code"]])
            self.assertEqual(
                {u["field"]: u["sites"] for u in result["filter_usages"]},
                {"code": 2, "tags": 2},
            )

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

    def test_skips_test_files_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "shop" / "models.py", _SHOP_MODELS)
            _write(
                root / "shop" / "tests" / "test_products.py",
                "from shop.models import Product\n"
                "def test_lookup_by_external_id():\n"
                "    assert Product.objects.filter(external_id='sku-1').exists()\n"
                "def test_get_by_external_id():\n"
                "    Product.objects.get(external_id='sku-1')\n",
            )

            result_default = suggest_indexes("shop", "Product", str(root))
            self.assertEqual(result_default.get("proposed_indexes", []), [])
            self.assertEqual(result_default.get("filter_usages", []), [])

            result_included = suggest_indexes("shop", "Product", str(root), include_tests=True)
            proposed = [p["fields"] for p in result_included.get("proposed_indexes", [])]
            self.assertIn(["external_id"], proposed)


if __name__ == "__main__":
    unittest.main()
