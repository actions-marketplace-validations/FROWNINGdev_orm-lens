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

// #336: django-extensions' and django-model-utils' TimeStampedModel live in
// site-packages, so a workspace scan never reads their `created` / `modified`.
// Mirrors cli/tests/test_timestamped_model.py.
const auditLog = (src) => parseModelsFile('audit/models.py', src).find((m) => m.name === 'AuditLog');

const AUDIT_LOG = `from django.db import models
from django_extensions.db.models import TimeStampedModel


class AuditLog(TimeStampedModel):
    action = models.CharField(max_length=32)
`;

test('a TimeStampedModel subclass gets created and modified', () => {
  assert.deepEqual(
    auditLog(AUDIT_LOG).inheritedFields.map((f) => [f.name, f.inheritedFrom]),
    [
      ['created', 'TimeStampedModel'],
      ['modified', 'TimeStampedModel'],
    ]
  );
});

test("django-model-utils' TimeStampedModel brings the same fields", () => {
  const m = auditLog(AUDIT_LOG.replace('django_extensions.db.models', 'model_utils.models'));
  assert.deepEqual(m.inheritedFields.map((f) => f.name), ['created', 'modified']);
});

test('a workspace TimeStampedModel wins over the table', () => {
  const m = auditLog(`from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class AuditLog(TimeStampedModel):
    action = models.CharField(max_length=32)
`);
  assert.deepEqual(m.inheritedFields.map((f) => f.name), ['created_at']);
});
