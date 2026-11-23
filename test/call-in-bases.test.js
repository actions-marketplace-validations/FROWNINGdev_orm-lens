const assert = require('node:assert/strict');
const Module = require('node:module');
const test = require('node:test');

const originalLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === 'vscode') return {};
  return originalLoad.call(this, request, parent, isMain);
};

const { parseModelsFile } = require('../out/parser');

test.after(() => {
  Module._load = originalLoad;
});

// Regression for issue #335 — a call in the base list drops the model.
//
// `ExportModelOperationsMixin("recipe")` is a call that returns a class, used
// directly in the base list. The class-header regex only reads bases up to the
// first `)`, so the header never matched and the class was skipped without
// any message. `readMultilineClass` now strips calls from the base list
// before matching, so the other bases still decide whether this is a model.
const REPRO = `
from django.db import models
from django_prometheus.models import ExportModelOperationsMixin


class Comment(ExportModelOperationsMixin("comment"), models.Model):
    text = models.TextField()


class Plain(models.Model):
    text = models.TextField()
`;

const MULTILINE = `
from django.db import models
from django_prometheus.models import ExportModelOperationsMixin


class Foo(
    ExportModelOperationsMixin("foo"),
    models.Model,
):
    text = models.TextField()
`;

const NESTED_CALL = `
from django.db import models


class Bar(Mixin(get_label("x")), models.Model):
    text = models.TextField()
`;

const NO_MODEL_BASE = `
from django.db import models


class Plain(SomeFactory("x")):
    text = models.TextField()
`;

test('a call in the base list keeps the model (issue #335)', () => {
  const models = parseModelsFile('shop/models.py', REPRO);
  assert.deepEqual(
    models.map((m) => m.name),
    ['Comment', 'Plain'],
  );
  assert.deepEqual(
    models[0].fields.map((f) => f.name),
    ['text'],
  );
});

test('a multi-line header with a call in the bases (issue #335)', () => {
  const models = parseModelsFile('shop/models.py', MULTILINE);
  assert.deepEqual(
    models.map((m) => m.name),
    ['Foo'],
  );
  assert.deepEqual(
    models[0].fields.map((f) => f.name),
    ['text'],
  );
});

test('a nested call in the bases is stripped (issue #335)', () => {
  const models = parseModelsFile('shop/models.py', NESTED_CALL);
  assert.deepEqual(
    models.map((m) => m.name),
    ['Bar'],
  );
});

test('a call as the only base is still not a model (issue #335)', () => {
  const models = parseModelsFile('shop/models.py', NO_MODEL_BASE);
  assert.deepEqual(models, []);
});
