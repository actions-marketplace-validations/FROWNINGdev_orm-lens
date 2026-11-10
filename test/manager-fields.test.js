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

test('a default Manager and QuerySet are not model fields', () => {
  const [note] = parseModelsFile('/project/notes/models.py', `
class Note(models.Model):
    text = models.TextField()
    objects = models.Manager()
    rows = models.QuerySet.as_manager()
    tags = TaggableManager()
    title = models.CharField(max_length=100)
`);

  assert.deepEqual(
    note.fields.map((field) => field.name),
    ['text', 'tags', 'title']
  );
  assert.equal(note.fields[1].isRelation, true);
});

test('a multi-line Manager call is skipped as a whole', () => {
  const [note] = parseModelsFile('/project/notes/models.py', `
class Note(models.Model):
    objects = models.Manager(
        include_deleted=False,
    )
    text = models.TextField()
`);

  assert.deepEqual(
    note.fields.map((field) => field.name),
    ['text']
  );
});
