"""Regression tests for #267: fields from ``django.contrib.auth``'s abstract bases.

``AbstractBaseUser``, ``PermissionsMixin`` and ``AbstractUser`` live in Django
itself, so a workspace scan never reads them, and a custom user model lost
their columns: ``drift`` reported them as "migrated but no longer declared".
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orm_lens.drift import detect_drift
from orm_lens.models import ParsedModel
from orm_lens.parser import parse_models_file

CUSTOM_USER = '''
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    USERNAME_FIELD = "email"
'''

# What ``makemigrations`` writes for CUSTOM_USER.
INITIAL_MIGRATION = '''
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [("auth", "0012_alter_user_first_name_max_length")]
    operations = [
        migrations.CreateModel(
            name="User",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("password", models.CharField(max_length=128, verbose_name="password")),
                ("last_login", models.DateTimeField(blank=True, null=True, verbose_name="last login")),
                ("is_superuser", models.BooleanField(default=False)),
                ("email", models.EmailField(max_length=254, unique=True)),
                ("groups", models.ManyToManyField(blank=True, related_name="user_set", related_query_name="user", to="auth.group")),
                ("user_permissions", models.ManyToManyField(blank=True, related_name="user_set", related_query_name="user", to="auth.permission")),
            ],
        ),
    ]
'''


def _user(source: str) -> ParsedModel:
    return next(m for m in parse_models_file("accounts/models.py", source) if m.name == "User")


class DjangoAuthBasesTest(unittest.TestCase):
    def test_custom_user_gets_the_auth_columns(self) -> None:
        # ``is_active`` on AbstractBaseUser is a class attribute, not a field.
        self.assertEqual(
            [f.name for f in _user(CUSTOM_USER).all_fields()],
            ["password", "last_login", "is_superuser", "groups", "user_permissions", "email"],
        )

    def test_records_the_django_base_each_field_came_from(self) -> None:
        origins = {f.name: f.inherited_from for f in _user(CUSTOM_USER).inherited_fields}
        self.assertEqual(origins["password"], "AbstractBaseUser")
        self.assertEqual(origins["groups"], "PermissionsMixin")

    def test_groups_and_permissions_are_many_to_many_relations(self) -> None:
        relations = {
            f.name: (f.relation_kind, f.related_model)
            for f in _user(CUSTOM_USER).inherited_fields
            if f.is_relation
        }
        self.assertEqual(
            relations,
            {
                "groups": ("ManyToManyField", "auth.Group"),
                "user_permissions": ("ManyToManyField", "auth.Permission"),
            },
        )

    def test_abstract_user_fields_and_an_own_email_shadows_the_inherited_one(self) -> None:
        user = _user(
            "from django.contrib.auth.models import AbstractUser\n"
            "from django.db import models\n"
            "\n"
            "class User(AbstractUser):\n"
            "    email = models.EmailField(unique=True)\n"
        )
        self.assertEqual(
            [f.name for f in user.inherited_fields],
            [
                "password", "last_login", "is_superuser", "groups", "user_permissions",
                "username", "first_name", "last_name", "is_staff", "is_active", "date_joined",
            ],
        )
        emails = [f for f in user.all_fields() if f.name == "email"]
        self.assertEqual([f.args for f in emails], ["unique=True"])

    def test_a_workspace_class_of_the_same_name_wins(self) -> None:
        user = _user(
            "from django.contrib.auth.base_user import AbstractBaseUser\n"
            "from django.db import models\n"
            "\n"
            "class PermissionsMixin(models.Model):\n"
            "    role = models.CharField(max_length=20)\n"
            "\n"
            "    class Meta:\n"
            "        abstract = True\n"
            "\n"
            "class User(AbstractBaseUser, PermissionsMixin):\n"
            "    email = models.EmailField(unique=True)\n"
        )
        self.assertEqual(
            [f.name for f in user.inherited_fields], ["password", "last_login", "role"]
        )

    def test_a_field_set_to_none_is_not_inherited(self) -> None:
        # The email-only user: Django removes an abstract base's field that
        # the subclass sets to None.
        user = _user(
            "from django.contrib.auth.models import AbstractUser\n"
            "from django.db import models\n"
            "\n"
            "class User(AbstractUser):\n"
            "    username = None\n"
            "    first_name = None  # cookiecutter-django drops these two\n"
            "    last_name = None\n"
            "    email = models.EmailField(unique=True)\n"
        )
        names = [f.name for f in user.inherited_fields]
        self.assertNotIn("username", names)
        self.assertNotIn("first_name", names)
        self.assertIn("is_staff", names)

    def test_a_chained_none_removes_every_target(self) -> None:
        user = _user(
            "from django.contrib.auth.models import AbstractUser\n"
            "\n"
            "class User(AbstractUser):\n"
            "    first_name = last_name = None\n"
        )
        names = [f.name for f in user.inherited_fields]
        self.assertNotIn("first_name", names)
        self.assertNotIn("last_name", names)

    def test_an_abstract_parent_override_beats_the_django_field(self) -> None:
        models_ = parse_models_file(
            "accounts/models.py",
            "from django.contrib.auth.models import AbstractUser\n"
            "from django.db import models\n"
            "\n"
            "class BaseUser(AbstractUser):\n"
            "    email = models.EmailField(unique=True)\n"
            "\n"
            "    class Meta:\n"
            "        abstract = True\n"
            "\n"
            "class User(BaseUser):\n"
            "    pass\n",
        )
        user = next(m for m in models_ if m.name == "User")
        emails = [f for f in user.all_fields() if f.name == "email"]
        self.assertEqual([(f.args, f.inherited_from) for f in emails], [("unique=True", "BaseUser")])

    def test_the_initial_migration_is_not_drift(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "accounts" / "migrations").mkdir(parents=True)
            (root / "accounts" / "models.py").write_text(CUSTOM_USER, encoding="utf-8")
            (root / "accounts" / "migrations" / "__init__.py").write_text("", encoding="utf-8")
            (root / "accounts" / "migrations" / "0001_initial.py").write_text(
                INITIAL_MIGRATION, encoding="utf-8"
            )
            self.assertEqual(detect_drift(str(root)).to_dict()["drifted"], [])


if __name__ == "__main__":
    unittest.main()
