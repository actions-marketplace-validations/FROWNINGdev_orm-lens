"""Regression tests for issue #335 — a call in the base list drops the model.

``ExportModelOperationsMixin("recipe")`` is a call that returns a class, used
directly in the base list. The class-header regex only reads bases up to the
first ``)``, so the header never matched and the class was skipped without
any message. ``_read_multiline_class`` now strips calls from the base list
before matching, so the other bases still decide whether this is a model.
"""

from __future__ import annotations

import unittest

from orm_lens.parser import parse_models_file

REPRO = """
from django.db import models
from django_prometheus.models import ExportModelOperationsMixin


class Comment(ExportModelOperationsMixin("comment"), models.Model):
    text = models.TextField()


class Plain(models.Model):
    text = models.TextField()
"""

MULTILINE = """
from django.db import models
from django_prometheus.models import ExportModelOperationsMixin


class Foo(
    ExportModelOperationsMixin("foo"),
    models.Model,
):
    text = models.TextField()
"""

NESTED_CALL = """
from django.db import models


class Bar(Mixin(get_label("x")), models.Model):
    text = models.TextField()
"""

NO_MODEL_BASE = """
from django.db import models


class Plain(SomeFactory("x")):
    text = models.TextField()
"""


class CallInBasesTest(unittest.TestCase):
    def test_call_in_base_list_keeps_the_model(self) -> None:
        models = parse_models_file("shop/models.py", REPRO)
        self.assertEqual([m.name for m in models], ["Comment", "Plain"])
        comment = models[0]
        self.assertEqual([f.name for f in comment.fields], ["text"])

    def test_multiline_header_with_a_call_in_the_bases(self) -> None:
        models = parse_models_file("shop/models.py", MULTILINE)
        self.assertEqual([m.name for m in models], ["Foo"])
        self.assertEqual([f.name for f in models[0].fields], ["text"])

    def test_nested_call_in_the_bases_is_stripped(self) -> None:
        models = parse_models_file("shop/models.py", NESTED_CALL)
        self.assertEqual([m.name for m in models], ["Bar"])

    def test_call_as_the_only_base_is_still_not_a_model(self) -> None:
        models = parse_models_file("shop/models.py", NO_MODEL_BASE)
        self.assertEqual(models, [])
