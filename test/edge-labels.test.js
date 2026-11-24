const assert = require('node:assert/strict');
const Module = require('node:module');
const test = require('node:test');

const originalLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === 'vscode') {
    return {};
  }
  return originalLoad.call(this, request, parent, isMain);
};

const { buildMermaid } = require('../out/graphWebview');
const { parseModelsFile } = require('../out/parser');

test.after(() => {
  Module._load = originalLoad;
});

test('labels OneToOneField metadata and ManyToManyField through models', () => {
  const models = parseModelsFile('/project/app/models.py', `
class User(models.Model):
    profile = models.OneToOneField(
        'Profile', on_delete=models.CASCADE, related_name='user'
    )

class Profile(models.Model):
    pass

class Group(models.Model):
    members = models.ManyToManyField(
        'User', passthrough='Ignored', through='Membership', related_name='groups'
    )

class Membership(models.Model):
    pass
`);
  const mermaid = buildMermaid({
    apps: [{ name: 'app', path: '/project/app', models }],
    scannedAt: 0,
  });

  assert.match(mermaid, /User \|\|--\|\| Profile : "profile \[CASCADE, as user\]"/);
  assert.match(mermaid, /Group \}o--o\{ User : "members \[through Membership, as groups\]"/);
});
test('draws columns and relations inherited from an abstract base', () => {
  const models = parseModelsFile('/project/shop/models.py', `
class AbstractAddress(models.Model):
    line1 = models.CharField(max_length=255)
    country = models.ForeignKey('Country', on_delete=models.CASCADE, related_name='+')

    class Meta:
        abstract = True

class Country(models.Model):
    name = models.CharField(max_length=100)

class Address(AbstractAddress):
    pass
`);
  const mermaid = buildMermaid({
    apps: [{ name: 'shop', path: '/project/shop', models }],
    scannedAt: 0,
  });
  assert.match(mermaid, /Address \{\n    CharField line1\n  \}/);
  assert.ok(mermaid.includes('Address }o--|| Country : "country [CASCADE]"'), mermaid);
  assert.ok(!mermaid.includes('as _'), mermaid);
});
