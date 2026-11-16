"""Regression tests for issue #249 — workspace Mixins are not models by name.

``_looks_like_model`` used to treat any base whose name ends in ``Mixin`` as
a Django model. That guess exists for third-party bases the scanner cannot
see, but it also fired for plain classes defined in the workspace — a
``QuerySet`` mixin or a URL helper — so ``drift`` reported them as declared
models with no migration.

A base defined in the analysed workspace must be resolved instead of guessed:
only a chain that reaches a real Django model base makes a class a model.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orm_lens.parser import parse_models_file, scan_workspace


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


LABEL_STUDIO_MIXINS = """
class StateQuerySetMixin:
    def with_state(self):
        return self
"""

LABEL_STUDIO_MODELS = """
from django.db import models

from .mixins import StateQuerySetMixin


class ProjectQuerySet(models.QuerySet):
    pass


class ProjectQuerySetWithState(StateQuerySetMixin, ProjectQuerySet):
    pass


class Project(models.Model):
    title = models.CharField(max_length=100)

    objects = ProjectQuerySetWithState.as_manager()
"""

WEBLATE_MIXINS = """
class BaseURLMixin:
    def base_url(self):
        return "/"


class URLMixin(BaseURLMixin):
    def get_absolute_url(self):
        return "/"


class LoggerMixin(BaseURLMixin):
    def log(self):
        pass


class PathMixin(LoggerMixin, URLMixin):
    def full_path(self):
        return self.get_absolute_url()
"""

WEBLATE_MODELS = """
from django.db import models

from .mixins import PathMixin


class Project(models.Model, PathMixin):
    title = models.CharField(max_length=100)
"""


class PlainMixinIsNotAModelTest(unittest.TestCase):
    def _models(self, app: str, mixins: str, models: str) -> set[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / app / "mixins.py", mixins)
            _write(root / app / "models.py", models)
            index = scan_workspace(str(root))
            return {m.name for a in index.apps for m in a.models}

    def test_queryset_mixin_is_not_a_model(self) -> None:
        # label-studio: ProjectQuerySetWithState(StateQuerySetMixin,
        # ProjectQuerySet), where StateQuerySetMixin is a plain workspace
        # class and ProjectQuerySet is a models.QuerySet.
        self.assertEqual(
            self._models("projects", LABEL_STUDIO_MIXINS, LABEL_STUDIO_MODELS),
            {"Project"},
        )

    def test_url_helper_mixins_are_not_models(self) -> None:
        # weblate: PathMixin is pulled in because Project inherits it, and
        # its ancestors (all plain helpers) must not become models with it.
        self.assertEqual(
            self._models("trans", WEBLATE_MIXINS, WEBLATE_MODELS),
            {"Project"},
        )


class AliasedWorkspaceMixinTest(unittest.TestCase):
    def test_aliased_import_is_resolved_before_the_mixin_guess(self) -> None:
        # ``StateMixin`` is the local binding for the workspace class
        # ``StateQuerySetMixin``. The definition set stores the original name,
        # so the lookup has to follow the import alias or the subclass is
        # still guessed to be a model (#249 follow-up).
        models = """
from django.db import models

from .mixins import StateQuerySetMixin as StateMixin


class ProjectQuerySet(models.QuerySet):
    pass


class ProjectQuerySetWithState(StateMixin, ProjectQuerySet):
    pass


class Project(models.Model):
    title = models.CharField(max_length=100)
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "projects" / "mixins.py", LABEL_STUDIO_MIXINS)
            _write(root / "projects" / "models.py", models)
            index = scan_workspace(str(root))
            names = {m.name for a in index.apps for m in a.models}
        self.assertEqual(names, {"Project"})


class WorkspaceWideNameDoesNotHideThirdPartyMixinTest(unittest.TestCase):
    def test_unresolvable_mixin_falls_back_to_the_suffix_guess(self) -> None:
        # app_a's plain AuditMixin must not make app_b's third-party
        # AuditMixin look like a workspace class: from app_b/models.py that
        # name does not resolve, so Record is still guessed to be a model.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "app_a" / "models.py",
                "class AuditMixin:\n"
                "    def audit(self):\n"
                "        return self\n",
            )
            _write(
                root / "app_b" / "models.py",
                "from django.db import models\n"
                "from thirdparty.mixins import AuditMixin\n"
                "\n"
                "class Record(AuditMixin):\n"
                "    title = models.CharField(max_length=100)\n",
            )
            index = scan_workspace(str(root))
            models = {m.name: m for a in index.apps for m in a.models}
        self.assertEqual(sorted(models), ["Record"])
        self.assertEqual([f.name for f in models["Record"].fields], ["title"])


class AbstractModelMixinStillRecognisedTest(unittest.TestCase):
    def test_abstract_model_mixin_still_makes_its_subclass_a_model(self) -> None:
        src = (
            "from django.db import models\n"
            "\n"
            "class OwnedMixin(models.Model):\n"
            "    owner = models.CharField(max_length=50)\n"
            "    class Meta:\n"
            "        abstract = True\n"
            "\n"
            "class Doc(OwnedMixin):\n"
            "    title = models.CharField(max_length=100)\n"
        )
        models = parse_models_file("docs/models.py", src)
        self.assertEqual([m.name for m in models], ["Doc"])
        self.assertEqual([f.name for f in models[0].inherited_fields], ["owner"])


class QuerySetManagerMixinChainTest(unittest.TestCase):
    def test_same_file_queryset_and_manager_subclasses_are_not_models(self) -> None:
        # The Mixin base itself is dropped by _collect_defs because all of its
        # bases are non-model tails. The subclass stays, and used to fall back
        # to the *Mixin suffix guess and become a model (#249).
        src = (
            "from django.db import models\n"
            "\n"
            "class SoftDeleteQuerySetMixin(models.QuerySet):\n"
            "    def alive(self):\n"
            "        return self.filter(deleted_at__isnull=True)\n"
            "\n"
            "class ArticleQuerySet(SoftDeleteQuerySetMixin):\n"
            "    pass\n"
            "\n"
            "class SoftDeleteManagerMixin(models.Manager):\n"
            "    pass\n"
            "\n"
            "class ArticleManager(SoftDeleteManagerMixin):\n"
            "    pass\n"
            "\n"
            "class Article(models.Model):\n"
            "    title = models.CharField(max_length=100)\n"
        )
        models = parse_models_file("blog/models.py", src)
        self.assertEqual([m.name for m in models], ["Article"])

    def test_imported_queryset_mixin_subclass_is_not_a_model(self) -> None:
        # The mixin lives outside the scanned models file; the resolver has to
        # follow the import and remember that the dropped class was a known
        # non-model rather than guess from the name.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "articles" / "querysets.py",
                "from django.db import models\n"
                "\n"
                "class SoftDeleteQuerySetMixin(models.QuerySet):\n"
                "    pass\n",
            )
            _write(
                root / "articles" / "models.py",
                "from django.db import models\n"
                "\n"
                "from .querysets import SoftDeleteQuerySetMixin\n"
                "\n"
                "class ArticleQuerySet(SoftDeleteQuerySetMixin):\n"
                "    pass\n"
                "\n"
                "class Article(models.Model):\n"
                "    title = models.CharField(max_length=100)\n",
            )
            index = scan_workspace(str(root))
            names = {m.name for a in index.apps for m in a.models}
        self.assertEqual(names, {"Article"})


class SameNamedAbstractBaseResolvedByFileTest(unittest.TestCase):
    def test_model_inherits_its_own_apps_base_not_the_same_named_one(self) -> None:
        # app_a's base lives in a module outside the scanned model files;
        # app_b has a scanned base with the same name. Matching bases by bare
        # name used to treat app_b's base as already known and never pull
        # app_a's, so app_a.Item inherited app_b's field. Identity is now
        # (file, name), resolved through app_a's import.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(
                root / "app_a" / "base.py",
                "from django.db import models\n"
                "\n"
                "class AbstractItem(models.Model):\n"
                "    local_field = models.CharField(max_length=50)\n"
                "\n"
                "    class Meta:\n"
                "        abstract = True\n",
            )
            _write(
                root / "app_a" / "models.py",
                "from django.db import models\n"
                "\n"
                "from .base import AbstractItem\n"
                "\n"
                "class Item(AbstractItem):\n"
                "    pass\n",
            )
            _write(
                root / "app_b" / "models.py",
                "from django.db import models\n"
                "\n"
                "class AbstractItem(models.Model):\n"
                "    other_field = models.CharField(max_length=50)\n"
                "\n"
                "    class Meta:\n"
                "        abstract = True\n"
                "\n"
                "class Item(AbstractItem):\n"
                "    pass\n",
            )
            index = scan_workspace(str(root))
            fields = {
                app.name: [
                    f.name
                    for f in next(
                        m for m in app.models if m.name == "Item"
                    ).all_fields()
                ]
                for app in index.apps
            }
        self.assertEqual(fields["app_a"], ["local_field"])
        self.assertEqual(fields["app_b"], ["other_field"])


ABSTRACT_URL_MIXINS = """
class AbstractURLMixin:
    def get_absolute_url(self):
        return "/"
"""

ABSTRACT_URL_MODELS = """
from django.db import models

from .mixins import AbstractURLMixin


class Project(AbstractURLMixin):
    title = models.CharField(max_length=100)
"""

PLAIN_ABSTRACT_FLAG_SOURCE = """
from django.db import models


class FieldMixin:
    label = models.CharField(max_length=30)

    class Meta:
        abstract = True


class Project(FieldMixin, models.Model):
    title = models.CharField(max_length=100)
"""

PLAIN_FIELD_WITH_REAL_ABSTRACT_SOURCE = """
from django.db import models


class FieldMixin:
    label = models.CharField(max_length=30)

    class Meta:
        abstract = True


class OwnedMixin(models.Model):
    owner = models.CharField(max_length=50)

    class Meta:
        abstract = True


class Project(FieldMixin, OwnedMixin):
    title = models.CharField(max_length=100)
"""


class AbstractNamedPlainMixinTest(unittest.TestCase):
    def test_abstract_named_workspace_mixin_is_not_a_model(self) -> None:
        # The ``Abstract*`` naming shortcut returned before the workspace
        # check, so a plain ``class AbstractURLMixin:`` helper made every
        # subclass a model even when nothing in its chain reached Django
        # (#249 follow-up).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "pages" / "mixins.py", ABSTRACT_URL_MIXINS)
            _write(root / "pages" / "models.py", ABSTRACT_URL_MODELS)
            index = scan_workspace(str(root))
            names = {m.name for a in index.apps for m in a.models}
        self.assertEqual(names, set())


class PlainAbstractFlagDoesNotContributeFieldsTest(unittest.TestCase):
    def test_plain_class_with_meta_abstract_contributes_no_fields(self) -> None:
        # ``Meta.abstract = True`` alone does not make a class a model, so
        # Django would never copy these attributes as columns (#249
        # follow-up).
        models = parse_models_file("pages/models.py", PLAIN_ABSTRACT_FLAG_SOURCE)
        self.assertEqual([m.name for m in models], ["Project"])
        self.assertEqual(models[0].inherited_fields, [])
        self.assertEqual([f.name for f in models[0].all_fields()], ["title"])

    def test_genuine_abstract_model_base_still_contributes_fields(self) -> None:
        models = parse_models_file(
            "pages/models.py", PLAIN_FIELD_WITH_REAL_ABSTRACT_SOURCE
        )
        self.assertEqual([m.name for m in models], ["Project"])
        self.assertEqual([f.name for f in models[0].inherited_fields], ["owner"])


KNOWN_NON_MODEL_BASE_SOURCE = """
from django.db import models


class QueryMixin(models.QuerySet):
    def alive(self):
        return self


class FieldMixin(QueryMixin):
    label = models.CharField(max_length=30)

    class Meta:
        abstract = True


class Project(FieldMixin, models.Model):
    title = models.CharField(max_length=100)
"""

EXTERNAL_MODEL_BASE_SOURCE = """
from django.contrib.auth.models import Group
from django.db import models


class AuditMixin(Group):
    owner = models.CharField(max_length=50)

    class Meta:
        abstract = True


class Project(AuditMixin, models.Model):
    title = models.CharField(max_length=100)
"""


class AncestryResolutionTest(unittest.TestCase):
    def test_known_non_model_base_does_not_contribute_fields(self) -> None:
        # QueryMixin is dropped as a known non-model QuerySet base. Its
        # abstract subclass must not launder that into model ancestry
        # (#249 follow-up).
        models = parse_models_file("pages/models.py", KNOWN_NON_MODEL_BASE_SOURCE)
        self.assertEqual([m.name for m in models], ["Project"])
        self.assertEqual(models[0].inherited_fields, [])
        self.assertEqual([f.name for f in models[0].all_fields()], ["title"])

    def test_external_model_base_still_contributes_fields(self) -> None:
        # Group comes from django.contrib.auth and is not in the workspace.
        # The scanner cannot prove it is a non-model, so the abstract parent
        # keeps contributing its local fields, as it did before #249.
        models = parse_models_file("pages/models.py", EXTERNAL_MODEL_BASE_SOURCE)
        self.assertEqual([m.name for m in models], ["Project"])
        self.assertEqual([f.name for f in models[0].inherited_fields], ["owner"])


ALIASED_MODEL_ROOT_MIXINS = """
from django.db import models
from django.db.models import Model as DjangoModel


class AuditMixin(DjangoModel):
    created = models.DateTimeField()

    class Meta:
        abstract = True
"""

ALIASED_MODEL_ROOT_MODELS = """
from django.db import models

from .mixins import AuditMixin


class Project(AuditMixin):
    name = models.CharField(max_length=50)
"""


class AliasedModelRootTest(unittest.TestCase):
    def test_aliased_django_model_root_in_workspace_mixin(self) -> None:
        # Issue #281: _looks_like_model did not recognise DjangoModel, and
        # since #258 workspace mixins don't get the *Mixin suffix guess, so
        # Project dropped out of the model list.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "projects" / "mixins.py", ALIASED_MODEL_ROOT_MIXINS)
            _write(root / "projects" / "models.py", ALIASED_MODEL_ROOT_MODELS)
            index = scan_workspace(str(root))
            names = {m.name for a in index.apps for m in a.models}
            self.assertEqual(names, {"Project"})
            project = next(m for a in index.apps for m in a.models if m.name == "Project")
            field_names = [f.name for f in project.all_fields()]
            self.assertIn("name", field_names)
            self.assertIn("created", field_names)

    def test_aliased_django_model_root_in_single_file(self) -> None:
        src = (
            "from django.db import models\n"
            "from django.db.models import Model as DjangoModel\n"
            "\n"
            "class AuditMixin(DjangoModel):\n"
            "    created = models.DateTimeField()\n"
            "    class Meta:\n"
            "        abstract = True\n"
            "\n"
            "class Project(AuditMixin):\n"
            "    name = models.CharField(max_length=50)\n"
        )
        models = parse_models_file("projects/models.py", src)
        self.assertEqual([m.name for m in models], ["Project"])
        field_names = [f.name for f in models[0].all_fields()]
        self.assertIn("name", field_names)
        self.assertIn("created", field_names)

    def test_aliased_django_model_root_direct_model(self) -> None:
        src = (
            "from django.db import models\n"
            "from django.db.models import Model as DjangoModel\n"
            "\n"
            "class Project(DjangoModel):\n"
            "    name = models.CharField(max_length=50)\n"
        )
        models = parse_models_file("projects/models.py", src)
        self.assertEqual([m.name for m in models], ["Project"])

    def test_single_file_parsing_with_absolute_path_and_sibling_mixin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "app"
            root.mkdir()
            _write(
                root / "mixins.py",
                "from django.db import models\n\n"
                "class AuditMixin(models.Model):\n"
                "    class Meta:\n"
                "        abstract = True\n",
            )
            models_file = root / "models.py"
            content = "from .mixins import AuditMixin\n\nclass Project(AuditMixin):\n    pass\n"
            _write(models_file, content)
            models = parse_models_file(str(models_file), content)
            self.assertEqual([m.name for m in models], ["Project"])

    def test_local_non_model_shadowing_imported_model_alias(self) -> None:
        src = (
            "from django.db import models\n"
            "from django.db.models import Model as DjangoModel\n"
            "\n"
            "class DjangoModel(models.QuerySet):\n"
            "    pass\n"
            "\n"
            "class Project(DjangoModel):\n"
            "    pass\n"
        )
        models = parse_models_file("app/models.py", src)
        self.assertEqual(models, [])


class CycleDetectionByFileAndClassTest(unittest.TestCase):
    def test_ancestor_with_same_class_name_in_different_file_is_not_skipped(self) -> None:
        # Issue #281: app/mixins.py: AuditMixin extends shared/mixins.py: AuditMixin.
        # Keying cycle detection by bare name caused the second ancestor to be
        # skipped as a cycle, dropping its inherited fields from all_fields().
        shared_mixins = (
            "from django.db import models\n"
            "\n"
            "class AuditMixin(models.Model):\n"
            "    created = models.DateTimeField()\n"
            "    class Meta:\n"
            "        abstract = True\n"
        )
        app_mixins = (
            "from django.db import models\n"
            "from shared.mixins import AuditMixin as SharedAuditMixin\n"
            "\n"
            "class AuditMixin(SharedAuditMixin):\n"
            "    updated = models.DateTimeField()\n"
            "    class Meta:\n"
            "        abstract = True\n"
        )
        app_models = (
            "from django.db import models\n"
            "from .mixins import AuditMixin\n"
            "\n"
            "class Project(AuditMixin):\n"
            "    name = models.CharField(max_length=50)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "shared" / "mixins.py", shared_mixins)
            _write(root / "app" / "mixins.py", app_mixins)
            _write(root / "app" / "models.py", app_models)
            index = scan_workspace(str(root))
            project = next(m for a in index.apps for m in a.models if m.name == "Project")
            field_names = [f.name for f in project.all_fields()]
            self.assertIn("name", field_names)
            self.assertIn("updated", field_names)
            self.assertIn("created", field_names)


if __name__ == "__main__":
    unittest.main()
