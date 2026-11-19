"""Graph-level migration conflict detection.

Django refuses to migrate an app whose migration graph has more than one leaf
("Conflicting migrations detected; multiple leaf nodes in the migration
graph"). The negative cases matter as much as the positive one: a merge
migration is exactly what a user writes to *fix* the conflict, so flagging it
would send them in circles.
"""

from __future__ import annotations

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orm_lens.migrations_parser import analyze_migration_risks

RULE = "conflicting_migration_leaves"


def _project(files: list[tuple[str, str]]) -> str:
    """Build a throwaway app whose migrations have the given dependencies."""
    tmp = TemporaryDirectory()
    _project._keep.append(tmp)  # type: ignore[attr-defined]
    _write_migrations(Path(tmp.name) / "shop", files)
    return tmp.name


def _write_migrations(app: Path, files: list[tuple[str, str]]) -> None:
    mig = app / "migrations"
    mig.mkdir(parents=True)
    (mig / "__init__.py").write_text("", encoding="utf-8")
    for name, deps in files:
        (mig / name).write_text(
            textwrap.dedent(
                f"""
                from django.db import migrations


                class Migration(migrations.Migration):
                    dependencies = {deps}
                    operations = []
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )


def _write_app_config(app: Path, label: str) -> None:
    app.joinpath("apps.py").write_text(
        "from django.apps import AppConfig\n\n"
        "class TestsConfig(AppConfig):\n"
        f"    name = {app.name!r}\n"
        f"    label = {label!r}\n",
        encoding="utf-8",
    )


_project._keep = []  # type: ignore[attr-defined]


def _conflicts(root: str):
    return [r for r in analyze_migration_risks(root, current_schema={}) if r.rule == RULE]


class MigrationConflictTests(unittest.TestCase):
    def test_two_branches_on_the_same_parent_conflict(self) -> None:
        root = _project(
            [
                ("0001_initial.py", "[]"),
                ("0002_alice.py", "[('shop', '0001_initial')]"),
                ("0002_bob.py", "[('shop', '0001_initial')]"),
            ]
        )
        found = _conflicts(root)
        # One finding per leaf, so every conflicting file is annotated in a PR
        # rather than only whichever one sorted first.
        self.assertEqual(len(found), 2)
        self.assertEqual({r.migration for r in found}, {"0002_alice", "0002_bob"})
        for r in found:
            self.assertEqual(r.severity, "critical")
            self.assertIn("leaf", r.description)
            self.assertIn("--merge", r.mitigation)

    def test_each_finding_names_the_migration_it_conflicts_with(self) -> None:
        root = _project(
            [
                ("0001_initial.py", "[]"),
                ("0002_alice.py", "[('shop', '0001_initial')]"),
                ("0002_bob.py", "[('shop', '0001_initial')]"),
            ]
        )
        by_name = {r.migration: r for r in _conflicts(root)}
        self.assertIn("0002_bob", by_name["0002_alice"].description)
        self.assertIn("0002_alice", by_name["0002_bob"].description)

    def test_linear_history_is_clean(self) -> None:
        root = _project(
            [
                ("0001_initial.py", "[]"),
                ("0002_a.py", "[('shop', '0001_initial')]"),
                ("0003_b.py", "[('shop', '0002_a')]"),
            ]
        )
        self.assertEqual(_conflicts(root), [])

    def test_same_directory_names_with_distinct_app_labels_do_not_conflict(self) -> None:
        with TemporaryDirectory() as root:
            for package, label in (
                ("tests", "tests"),
                ("lib/mixins/tests", "mixins_tests"),
                ("lib/permissions/tests", "permissions_tests"),
            ):
                app = Path(root) / package
                _write_migrations(
                    app,
                    [
                        ("0001_initial.py", "[]"),
                        (f"0002_{label}.py", f"[({label!r}, '0001_initial')]"),
                    ],
                )
                _write_app_config(app, label)

            self.assertEqual(_conflicts(root), [])

    def test_copies_of_same_configured_app_still_share_a_graph(self) -> None:
        with TemporaryDirectory() as root:
            for package, branch in (("shop", "alice"), ("vendored_shop", "bob")):
                app = Path(root) / package
                _write_migrations(
                    app,
                    [
                        ("0001_initial.py", "[]"),
                        (f"0002_{branch}.py", "[('store', '0001_initial')]"),
                    ],
                )
                _write_app_config(app, "store")

            conflicts = _conflicts(root)
            self.assertEqual({finding.app for finding in conflicts}, {"store"})
            self.assertEqual({finding.migration for finding in conflicts}, {"0002_alice", "0002_bob"})

    def test_uses_the_selected_subclass_of_a_local_app_config(self) -> None:
        root = _project(
            [
                ("0001_initial.py", "[]"),
                ("0002_update.py", "[('custom_shop', '0001_initial')]"),
            ]
        )
        (Path(root) / "shop/apps.py").write_text(
            "from django.apps import AppConfig\n\n"
            "class BaseConfig(AppConfig):\n"
            "    name = 'shop'\n"
            "    label = 'base_shop'\n\n"
            "class ShopConfig(BaseConfig):\n"
            "    default = True\n"
            "    label = 'custom_shop'\n",
            encoding="utf-8",
        )

        self.assertEqual(_conflicts(root), [])

    def test_relabelled_app_checks_only_its_own_declared_models(self) -> None:
        with TemporaryDirectory() as root:
            for package, label in (("a/tests", "a_tests"), ("b/tests", "b_tests")):
                app = Path(root) / package
                _write_migrations(app, [("0001_initial.py", "[]")])
                _write_app_config(app, label)
                migration = app / "migrations" / "0001_initial.py"
                migration.write_text(
                    migration.read_text(encoding="utf-8").replace(
                        "operations = []", 'operations = [migrations.DeleteModel(name="Item")]'
                    ),
                    encoding="utf-8",
                )
            (Path(root) / "a/tests/models.py").write_text(
                "from django.db import models\n\n"
                "class Item(models.Model):\n"
                "    name = models.CharField(max_length=100)\n",
                encoding="utf-8",
            )

            findings = analyze_migration_risks(root)
            referenced = [f.app for f in findings if f.rule == "delete_model_still_referenced"]
            self.assertEqual(referenced, ["a_tests"])

    def test_single_migration_is_clean(self) -> None:
        self.assertEqual(_conflicts(_project([("0001_initial.py", "[]")])), [])

    def test_short_numeric_prefix_is_a_migration_not_a_leaf_split(self) -> None:
        """`008_...` is loaded by Django; skipping it left `0009` unlinked."""
        root = _project(
            [
                ("0001_initial.py", "[]"),
                ("0007_bigint.py", "[('shop', '0001_initial')]"),
                ("008_remove_scheduled_mails.py", "[('shop', '0007_bigint')]"),
                ("0009_after.py", "[('shop', '008_remove_scheduled_mails')]"),
            ]
        )
        self.assertEqual(_conflicts(root), [])

    def test_helper_module_is_not_a_leaf(self) -> None:
        """Django imports every module in the package; only `Migration` counts.

        A constants/mixins module beside a linear history used to parse as one
        more root with no dependents — an extra leaf, i.e. a critical
        `conflicting_migration_leaves` finding for a healthy app.
        """
        root = _project(
            [
                ("0001_initial.py", "[]"),
                ("0002_add_slug.py", "[('shop', '0001_initial')]"),
            ]
        )
        (Path(root) / "shop" / "migrations" / "helpers.py").write_text(
            "from django.db import migrations\n\n\nNEW_FIELDS = []\n",
            encoding="utf-8",
        )
        self.assertEqual(_conflicts(root), [])

    def test_merge_migration_resolves_the_conflict(self) -> None:
        """The fix must not keep reporting the problem."""
        root = _project(
            [
                ("0001_initial.py", "[]"),
                ("0002_a.py", "[('shop', '0001_initial')]"),
                ("0002_b.py", "[('shop', '0001_initial')]"),
                ("0003_merge.py", "[('shop', '0002_a'), ('shop', '0002_b')]"),
            ]
        )
        self.assertEqual(_conflicts(root), [])

    def test_cross_app_dependency_is_not_a_leaf_of_this_app(self) -> None:
        """Only in-app edges decide leafness; a dependency on another app's
        migration must not make this app look like it has extra roots."""
        root = _project(
            [
                ("0001_initial.py", "[]"),
                ("0002_a.py", "[('other', '0001_initial'), ('shop', '0001_initial')]"),
            ]
        )
        self.assertEqual(_conflicts(root), [])

    def test_dependencies_built_by_a_function_are_not_a_conflict(self) -> None:
        """`dependencies = get_dependencies()` cannot be read statically.

        Reading it as `[]` left its parent without a child, so a linear history
        got a critical conflict between the parent and the real leaf.
        """
        root = _project(
            [
                ("0001_initial.py", "[]"),
                ("0003_noop.py", "[('shop', '0002_backfill')]"),
            ]
        )
        (Path(root) / "shop" / "migrations" / "0002_backfill.py").write_text(
            textwrap.dedent(
                """
                from django.conf import settings
                from django.db import migrations


                def get_dependencies():
                    dependencies = [("shop", "0001_initial")]
                    if "billing" in settings.INSTALLED_APPS:
                        dependencies.append(("billing", "0001_initial"))
                    return dependencies


                class Migration(migrations.Migration):
                    dependencies = get_dependencies()
                    operations = []
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(_conflicts(root), [])

    def test_annotated_computed_dependencies_are_not_a_conflict(self) -> None:
        """`dependencies: list = get_dependencies()` after `dependencies = []`.

        The annotated assignment rebinds dependencies with a computed value;
        ignoring AnnAssign allowed the initial `[]` to win and misidentified
        0001_initial as a conflicting leaf.
        """
        root = _project(
            [
                ("0001_initial.py", "[]"),
                ("0003_noop.py", "[('shop', '0002_backfill')]"),
            ]
        )
        (Path(root) / "shop" / "migrations" / "0002_backfill.py").write_text(
            textwrap.dedent(
                """
                from django.db import migrations


                def get_dependencies():
                    return [("shop", "0001_initial")]


                class Migration(migrations.Migration):
                    dependencies = []
                    dependencies: list = get_dependencies()
                    operations = []
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(_conflicts(root), [])

    def test_computed_pair_inside_dependencies_list_is_not_a_conflict(self) -> None:
        """A computed element inside `dependencies = [("shop", get_parent())]`.

        Dropping the computed pair made 0002 look like it had no dependencies,
        making 0001_initial a second leaf alongside 0003_noop.
        """
        root = _project(
            [
                ("0001_initial.py", "[]"),
                ("0003_noop.py", "[('shop', '0002_backfill')]"),
            ]
        )
        (Path(root) / "shop" / "migrations" / "0002_backfill.py").write_text(
            textwrap.dedent(
                """
                from django.db import migrations


                def get_parent():
                    return "0001_initial"


                class Migration(migrations.Migration):
                    dependencies = [("shop", get_parent())]
                    operations = []
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(_conflicts(root), [])

    def test_literal_dependencies_still_conflict_next_to_a_tuple(self) -> None:
        """A tuple literal is still read, so a real conflict is still caught."""
        root = _project(
            [
                ("0001_initial.py", "()"),
                ("0002_alice.py", "(('shop', '0001_initial'),)"),
                ("0002_bob.py", "[('shop', '0001_initial')]"),
            ]
        )
        self.assertEqual(
            {r.migration for r in _conflicts(root)}, {"0002_alice", "0002_bob"}
        )

    def test_app_label_differing_from_directory_name_is_not_a_conflict(self) -> None:
        """Django lets `AppConfig.label` differ from the package directory, and
        dependency tuples carry the *label*. Computing leafness against the
        directory name then finds no in-app edges and calls every migration a
        leaf — which reported a critical conflict for a linear history. Caught
        by two pre-existing CI-format tests whose fixture does exactly this."""
        tmp = TemporaryDirectory()
        _project._keep.append(tmp)  # type: ignore[attr-defined]
        mig = Path(tmp.name) / "checkout_app" / "migrations"
        mig.mkdir(parents=True)
        (mig / "__init__.py").write_text("", encoding="utf-8")
        for name, deps in [
            ("0001_initial.py", "[]"),
            ("0002_next.py", "[('orders', '0001_initial')]"),
        ]:
            (mig / name).write_text(
                textwrap.dedent(
                    f"""
                    from django.db import migrations


                    class Migration(migrations.Migration):
                        dependencies = {deps}
                        operations = []
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
        self.assertEqual(_conflicts(tmp.name), [])

    def test_swappable_dependency_still_reports_conflict_on_branches(self) -> None:
        """`migrations.swappable_dependency(...)` is a known cross-app dependency.

        It must not mark dependencies unknown (which would silence real leaf
        conflicts), and because it is not a same-app edge it leaves the in-app
        leaf graph intact.
        """
        root = _project(
            [
                ("0001_initial.py", "[]"),
                (
                    "0002_alice.py",
                    "[('shop', '0001_initial'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]",
                ),
                (
                    "0002_bob.py",
                    "[swappable_dependency(settings.AUTH_USER_MODEL), ('shop', '0001_initial')]",
                ),
            ]
        )
        self.assertEqual(
            {r.migration for r in _conflicts(root)}, {"0002_alice", "0002_bob"}
        )

    def test_swappable_dependency_in_linear_history_is_not_a_conflict(self) -> None:
        """A linear migration path containing a swappable dependency has a single leaf."""
        root = _project(
            [
                ("0001_initial.py", "[]"),
                (
                    "0002_user_profile.py",
                    "[('shop', '0001_initial'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]",
                ),
            ]
        )
        self.assertEqual(_conflicts(root), [])


if __name__ == "__main__":
    unittest.main()
