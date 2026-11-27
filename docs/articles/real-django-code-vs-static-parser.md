---
title: "24 open-source Django projects vs. my static parser: the Django code tutorials never show"
description: >-
  Running orm-lens over 24 real Django codebases (Tandoor, Read the Docs, Mayan
  EDMS, django-oscar, Ralph, pretix and more) turned up patterns a static
  parser never expects: calls in base lists, models looked up by name, Python
  3.14 syntax in a migration. What each one is, and why Django is fine with it.
---

# 24 open-source Django projects vs. my static parser: the Django code tutorials never show

*FROWNINGdev · 27 November 2026*

I maintain [orm-lens](https://github.com/FROWNINGdev/orm-lens), a static
analyzer for Django models and migrations. It reads source files only: no
database, no settings module, no `django.setup()`. That makes it fast and lets
it run on a half-broken checkout. It also means every model, field and
migration has to be understood from the text alone.

This month I ran it over 24 open-source Django codebases it had never seen:
NetBox, PostHog, authentik, Plane, paperless-ngx, Healthchecks, saleor,
wagtail, Label Studio, pretix, Weblate, Taiga, InvenTree, Kiwi TCMS, Mayan
EDMS, Misago, CVAT, Ralph, Zulip, Read the Docs, Nautobot, Tandoor Recipes,
wger and django-oscar. Then I read every place where its answer didn't match
what Django would do.

None of what follows is a bug in those projects. It is all valid Django and
most of it is idiomatic. It just isn't the Django you see in the tutorial,
where every model is `class Foo(models.Model)` and every field is
`models.Something(...)`. If you write a linter, a codemod or a migration check
for Django, this is the list I wish I'd had.

## Class headers

### A call in the base list (Tandoor Recipes)

```python
class Recipe(ExportModelOperationsMixin("recipe"), models.Model, PermissionModelMixin):
    ...
```

[django-prometheus](https://github.com/korfuri/django-prometheus) exports
per-model metrics through a mixin *factory*: `ExportModelOperationsMixin("recipe")`
returns a class, and Python accepts any expression in a base list. orm-lens
found class headers with a regex that read the bases as "everything up to the
first `)`", which here is the end of `("recipe"`. Tandoor lost 22 of its
models this way, including `Recipe`, `Step` and `Ingredient`.

The result of a call can't be resolved statically anyway, so the fix drops
calls from the base list and lets the remaining bases decide whether the class
is a model. ([#335](https://github.com/FROWNINGdev/orm-lens/issues/335))

### A base looked up by name (django-oscar)

```python
from oscar.core.loading import get_model

Benefit = get_model("offer", "Benefit")


class PercentageDiscountBenefit(Benefit):
    class Meta:
        proxy = True
```

Oscar lets every project override any model, so its own code never imports
model classes. It asks the app registry for them by label and name. To a
parser that follows imports, `Benefit` is just a module-level variable, and
all 12 benefit and condition proxies vanished. orm-lens now treats
`X = get_model("app", "Model")` as a binding to that model.
([#341](https://github.com/FROWNINGdev/orm-lens/issues/341))

### Fields that come from somewhere you don't parse

Read the Docs has 24 models on django-extensions' `TimeStampedModel`.
InvenTree, Mayan and Misago build trees on `MPTTModel`. Kiwi TCMS uses
django-tree-queries, Mayan uses django-solo. Each of these abstract bases
brings fields (`created`/`modified`, `lft`/`rght`/`tree_id`/`level`, ...) that
appear in every migration and in no `models.py` of the project.

A source-only tool doesn't read site-packages, so it has to know the popular
bases by name. Without that, `drift` reported 24 of Read the Docs' models as
missing two columns each.
([#336](https://github.com/FROWNINGdev/orm-lens/issues/336),
[#302](https://github.com/FROWNINGdev/orm-lens/issues/302))

### The field nobody declares (Ralph)

```python
class BaseObject(models.Model):
    remarks = models.TextField(blank=True)


class Asset(BaseObject):
    hostname = models.CharField(max_length=255)
```

With concrete inheritance Django adds a one-to-one link to the parent,
`baseobject_ptr`, and `makemigrations` writes it into the child's
`CreateModel`. The model never declares it. Ralph is built on a concrete
`BaseObject`, and the link showed up as drift 25 times.
([#301](https://github.com/FROWNINGdev/orm-lens/issues/301))

## Field declarations

### Alphabetical keyword arguments (Mayan EDMS)

```python
document = models.ForeignKey(
    on_delete=models.CASCADE, related_name="files", to=Document,
    verbose_name=_("Document")
)
```

Mayan writes every field with keyword arguments in alphabetical order, so
`to=` is never first. The parser stripped one closing parenthesis too many
when the last argument was itself a call (`_("Document")`), fell back to "take
the first identifier", and read the target as `on_delete`. That hit 81 of
Mayan's 92 relations. The test suite had kwarg-order tests; none of them ended
in a call. ([#300](https://github.com/FROWNINGdev/orm-lens/issues/300))

### Fields imported by name (Taiga, Label Studio)

```python
from django.contrib.postgres.fields import ArrayField

tags = ArrayField(models.TextField(), default=list)
```

`models.Anything(...)` is easy to spot. `ArrayField(...)`, `HStoreField(...)`
and `GeneratedField(...)` are almost always imported by name, and a bare call
only counts as a field if the parser knows the name. 15 of Taiga's 18 drift
entries were `ArrayField`s.
([#250](https://github.com/FROWNINGdev/orm-lens/issues/250))

## Migrations

### Migration names that don't start with four digits (pretix)

`0162b_auto_20201218_1810.py`, `008_remove_scheduled_mails.py`. Django doesn't
care what a migration file is called: its loader imports every module in
`migrations/` that doesn't start with `_` or `~`. orm-lens only read files
matching `^\d{4}_`. The fields added in `0162b` showed up as "declared but
never migrated", and with `008` gone, the migration before it looked like a
second leaf, which is a critical "conflicting migrations" finding for a
perfectly linear history.
([#246](https://github.com/FROWNINGdev/orm-lens/issues/246))

### Python 3.14 syntax in a data migration (Read the Docs)

```python
try:
    ...
except SyntaxError, ValueError:
    pass
```

[PEP 758](https://peps.python.org/pep-0758/) lets `except` take several types
without parentheses. Read the Docs already uses it in a data migration. On
Python 3.13 and older `ast.parse()` fails on that file, the migration was
dropped silently, and the gap it left in the graph turned into a false
"conflicting leaves". A tool that runs on the user's interpreter will meet
source written for a newer one. Now a migration that won't parse marks its
app's graph as unknown, and the leaf check stays silent instead of guessing.
([#337](https://github.com/FROWNINGdev/orm-lens/issues/337))

### The app label isn't the directory name (Misago)

```python
# misago/threads/migrations/0002_threads_settings.py
dependencies = [("misago_threads", "0001_initial"), ...]
```

Every Misago app sets `AppConfig.label`, and migration dependencies carry the
label, not the folder name. `migration-deps threads` matched on the folder,
saw no in-app dependencies at all, and printed all 39 migrations as both roots
and leaves. ([#327](https://github.com/FROWNINGdev/orm-lens/issues/327))

## Signals

### `@receiver` with a keyword (pretix)

```python
@receiver(signal=post_save, sender=Order)
def on_order_saved(sender, instance, **kwargs):
    ...
```

The Django docs pass the signal positionally. pretix passes it as
`signal=` in about 40 places, and each became an `<unknown>` signal.
([#248](https://github.com/FROWNINGdev/orm-lens/issues/248))

## What I take from it

A static tool for Django has to model Django's *rules*, not the shape of the
code in the docs. The rule is "a base list is any expression", not "a base
list is a few dotted names"; "a migration is any module in `migrations/` with a
`Migration` class", not "a file called `NNNN_*.py`". Each time I wrote the
shape instead of the rule, some real project broke it.

Synthetic tests didn't catch any of these. They check the cases I thought of.
The only thing that finds the cases I didn't think of is code I didn't write,
so every parser change is now run against a corpus of real projects before it
merges, and the output is compared with main.

Every item above became a GitHub issue with a minimal repro, the file to
start in and the test that should pass. Since 9 November, 25 people have sent
58 merged pull requests, most of them on these issues, and most within hours
of the issue going up. Two from this list are still open if you want one:
[#254](https://github.com/FROWNINGdev/orm-lens/issues/254) (field classes
defined in the project itself) and
[#308](https://github.com/FROWNINGdev/orm-lens/issues/308) (apps whose
directories share a name).

Try it on your own project:

```bash
uvx orm-lens list -v        # every model it found
uvx orm-lens drift          # models.py vs. the migration history
uvx orm-lens migration-risk # migrations that break on a populated table
```

If it gets your project wrong, [open an
issue](https://github.com/FROWNINGdev/orm-lens/issues). That's where this list
came from.
