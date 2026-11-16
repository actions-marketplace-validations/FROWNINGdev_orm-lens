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

// #267: AbstractBaseUser, PermissionsMixin and AbstractUser live in Django
// itself, so a workspace scan never reads them. Mirrors
// cli/tests/test_django_auth_bases.py.
const user = (src) => parseModelsFile('accounts/models.py', src).find((m) => m.name === 'User');

const CUSTOM_USER = `from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    USERNAME_FIELD = "email"
`;

test('a custom user inherits the auth columns from Django', () => {
  const u = user(CUSTOM_USER);
  assert.deepEqual(
    u.inheritedFields.map((f) => [f.name, f.inheritedFrom]),
    [
      ['password', 'AbstractBaseUser'],
      ['last_login', 'AbstractBaseUser'],
      ['is_superuser', 'PermissionsMixin'],
      ['groups', 'PermissionsMixin'],
      ['user_permissions', 'PermissionsMixin'],
    ],
    'is_active on AbstractBaseUser is a class attribute, not a field'
  );
  assert.deepEqual(
    u.inheritedFields.filter((f) => f.isRelation).map((f) => [f.name, f.relationKind, f.relatedModel]),
    [
      ['groups', 'ManyToManyField', 'auth.Group'],
      ['user_permissions', 'ManyToManyField', 'auth.Permission'],
    ]
  );
});

test('AbstractUser adds its own fields and an own email shadows the inherited one', () => {
  const u = user(`from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    email = models.EmailField(unique=True)
`);
  assert.deepEqual(
    u.inheritedFields.map((f) => f.name),
    [
      'password', 'last_login', 'is_superuser', 'groups', 'user_permissions',
      'username', 'first_name', 'last_name', 'is_staff', 'is_active', 'date_joined',
    ]
  );
  assert.deepEqual(u.fields.map((f) => f.name), ['email']);
});

test('a workspace class of the same name wins over the Django table', () => {
  const u = user(`from django.contrib.auth.base_user import AbstractBaseUser
from django.db import models


class PermissionsMixin(models.Model):
    role = models.CharField(max_length=20)

    class Meta:
        abstract = True


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
`);
  assert.deepEqual(u.inheritedFields.map((f) => f.name), ['password', 'last_login', 'role']);
});

test('a field set to None is not inherited', () => {
  const u = user(`from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    username = None
    first_name = None  # cookiecutter-django drops these two
    last_name = None
    email = models.EmailField(unique=True)
`);
  const names = u.inheritedFields.map((f) => f.name);
  assert.ok(!names.includes('username'));
  assert.ok(!names.includes('first_name'));
  assert.ok(names.includes('is_staff'));
});

test("an abstract parent's override beats the Django field", () => {
  const u = user(`from django.contrib.auth.models import AbstractUser
from django.db import models


class BaseUser(AbstractUser):
    email = models.EmailField(unique=True)

    class Meta:
        abstract = True


class User(BaseUser):
    pass
`);
  assert.deepEqual(
    u.inheritedFields.filter((f) => f.name === 'email').map((f) => [f.args, f.inheritedFrom]),
    [['unique=True', 'BaseUser']]
  );
});

test('a chained None removes every target', () => {
  const u = user(`from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    first_name = last_name = None
`);
  const names = u.inheritedFields.map((f) => f.name);
  assert.ok(!names.includes('first_name'));
  assert.ok(!names.includes('last_name'));
});

test('a function after the model is not read as its body', () => {
  const u = user(`from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    pass

def helper():
    groups = None
`);
  assert.ok(u.inheritedFields.some((f) => f.name === 'groups'));
});
