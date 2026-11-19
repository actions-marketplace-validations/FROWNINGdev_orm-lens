"""N+1 precision on patterns taken from large real-world codebases.

Each case was a false or misleading finding when the scanner was run over
NetBox, Healthchecks, Plane, authentik and paperless-ngx.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from textwrap import dedent

import pytest

from orm_lens.cli import main
from orm_lens.parser import scan_workspace
from orm_lens.query_analyzer import scan_for_nplusone
from tests.test_nplusone_detector import BLOG_SCHEMA, _write

SCHEMA = {
    **BLOG_SCHEMA,
    "Profile": {"user": "o2o", "email": "scalar"},
    "User": {"email": "scalar"},
}


def test_dict_values_do_not_look_like_querysets(tmp_path: Path) -> None:
    _write(
        tmp_path / "formats.py",
        "def build(tracks_by_id, methods):\n"
        "    for track in tracks_by_id.values():\n"
        "        track.shapes.sort(key=lambda shape: shape.frame)\n"
        "        first = track.shapes[0]\n"
        "    for meta in methods.values():\n"
        "        for transition in meta.transitions.values():\n"
        "            print(transition.target)\n",
    )
    assert scan_for_nplusone(str(tmp_path), {"Track": {"shapes": "reverse"}}) == []


@pytest.mark.parametrize(
    "setup, source, access",
    [
        ("", 'Post.objects.values("id", "author")', 'row["author"]'),
        ("", 'Post.objects.values(author_pk=F("author_id"))', 'row["author_pk"]'),
        ("", 'Post.objects.values_list("author", named=True)', "row.author.real"),
        ("", 'Post.objects.filter(active=True).values_list("author", named=True).iterator()', "row.author.real"),
        ('rows = Post.objects.values_list("author", named=True).order_by("author")\n', "rows", "row.author.real"),
        ('rows = Post.objects.values_list("author", named=True)\n', 'rows[:10].order_by("author")', "row.author.real"),
        (
            'rows = Post.objects.values_list("author", named=True)\n'
            'alias = rows\n'
            'ordered: QuerySet = alias.order_by("author")\n',
            "ordered",
            "row.author.real",
        ),
        (
            'def rows(qs):\n    return qs.values_list("author", named=True)\n',
            "rows(Post.objects.all()).iterator()",
            "row.author.real",
        ),
    ],
)
def test_projected_queryset_rows_do_not_load_relations(
    tmp_path: Path, setup: str, source: str, access: str,
) -> None:
    _write(tmp_path / "views.py", f"{setup}for row in {source}:\n    print({access})\n")
    assert scan_for_nplusone(str(tmp_path), SCHEMA) == []


def test_projection_does_not_hide_queries_on_outer_loop_models(tmp_path: Path) -> None:
    _write(
        tmp_path / "views.py",
        "rows = Post.objects.filter(author_id__in=Author.objects.values_list('id', flat=True))\n"
        "for post in rows.order_by('id'):\n"
        "    print(post.author.name)\n"
        "rows = rows.values_list('author', named=True)\n"
        "for row in rows:\n"
        "    print(row.author.real)\n"
        "for post in Post.objects.prefetch_related('tags'):\n"
        "    print(list(post.tags.values('id')))\n"
        "for post in Post.objects.prefetch_related('tags'):\n"
        "    for row in post.tags.values_list('id', named=True):\n"
        "        print(row.id.real)\n",
    )
    findings = scan_for_nplusone(str(tmp_path), SCHEMA)
    assert [(f.loop_var, f.suggested_fix) for f in findings] == [
        ("post", '.select_related("author")'),
        ("post", '.prefetch_related("tags")'),
        ("post", '.prefetch_related("tags")'),
    ]


def test_projected_binding_does_not_shadow_function_parameter(tmp_path: Path) -> None:
    _write(
        tmp_path / "views.py",
        "rows = Post.objects.values_list('author', named=True)\n"
        "def render(rows):\n"
        "    for post in rows.order_by('id'):\n"
        "        print(post.author.name)\n",
    )
    (finding,) = scan_for_nplusone(str(tmp_path), SCHEMA)
    assert finding.suggested_fix == '.select_related("author")'


@pytest.mark.parametrize(
    "body",
    [
        """
        rows = Post.objects.all()
        rows = rows[:10]
        """,
        """
        source = Post.objects.all()
        rows = source[:10]
        """,
        """
        rows = Post.objects.values_list("author", named=True)
        rows, marker = (Post.objects.all(), None)
        """,
        """
        rows = Post.objects.all()
        if projected:
            rows = rows.values_list("author", named=True)
            return list(rows)
        """,
        """
        rows = Post.objects.all()
        if projected:
            rows = rows.values_list("author", named=True)
        """,
    ],
    ids=["in-place-slice", "slice-alias", "unpack", "returning-branch", "either-row-type"],
)
def test_model_binding_survives_projection_tracking(tmp_path: Path, body: str) -> None:
    statements = dedent(body).strip()
    _write(
        tmp_path / "views.py",
        "def render(projected):\n"
        + "".join(f"    {line}\n" for line in statements.splitlines())
        + "    for post in rows:\n"
        "        print(post.author.name)\n",
    )
    (finding,) = scan_for_nplusone(str(tmp_path), SCHEMA)
    assert finding.suggested_fix == '.select_related("author")'
    assert finding.confidence == "high"


def test_unknown_unpack_clears_stale_projection(tmp_path: Path) -> None:
    _write(
        tmp_path / "views.py",
        "def render(source):\n"
        "    rows = Post.objects.values_list('author', named=True)\n"
        "    [rows, *other] = source\n"
        "    for post in rows.order_by('id'):\n"
        "        print(post.author.name)\n",
    )
    (finding,) = scan_for_nplusone(str(tmp_path), SCHEMA)
    assert finding.suggested_fix == '.select_related("author")'


def test_projection_on_both_continuing_branches_is_suppressed(tmp_path: Path) -> None:
    _write(
        tmp_path / "views.py",
        "def render(active):\n"
        "    if active:\n"
        "        rows = Post.objects.filter(active=True).values_list('author', named=True)\n"
        "    else:\n"
        "        rows = Post.objects.filter(active=False).values_list('author', named=True)\n"
        "    for row in rows.order_by('author'):\n"
        "        print(row.author.real)\n",
    )
    assert scan_for_nplusone(str(tmp_path), SCHEMA) == []


def test_projection_on_only_continuing_branch_is_suppressed(tmp_path: Path) -> None:
    _write(
        tmp_path / "views.py",
        "def render(active):\n"
        "    rows = Post.objects.values_list('author', named=True)\n"
        "    if active:\n"
        "        rows = Post.objects.all()\n"
        "        return list(rows)\n"
        "    for row in rows.order_by('author'):\n"
        "        print(row.author.real)\n",
    )
    assert scan_for_nplusone(str(tmp_path), SCHEMA) == []


def test_helper_returns_use_the_binding_at_each_return(tmp_path: Path) -> None:
    _write(
        tmp_path / "views.py",
        dedent("""
            def model_rows(active):
                rows = Post.objects.values_list("author", named=True)
                if active:
                    rows = Post.objects.all()
                    return rows
                return []

            def projected_rows(active):
                rows = Post.objects.all()
                if active:
                    rows = Post.objects.values_list("author", named=True)
                    return rows
                return []

            for post in model_rows(True):
                print(post.author.name)
            for row in projected_rows(True):
                print(row.author.real)
        """),
    )
    (finding,) = scan_for_nplusone(str(tmp_path), SCHEMA)
    assert (finding.loop_var, finding.suggested_fix) == ("post", '.select_related("author")')


def test_nested_loop_checks_later_model_binding_in_enclosing_scope(tmp_path: Path) -> None:
    _write(
        tmp_path / "views.py",
        dedent("""
            def render():
                rows = Post.objects.values_list("author", named=True)
                def consume():
                    for post in rows:
                        print(post.author.name)
                rows = Post.objects.all()
                consume()
                rows = rows.values_list("author", named=True)

            def stable():
                rows = Post.objects.values_list("author", named=True)
                def consume():
                    for row in rows:
                        print(row.author.real)
                consume()
        """),
    )
    (finding,) = scan_for_nplusone(str(tmp_path), SCHEMA)
    assert (finding.loop_var, finding.suggested_fix) == ("post", '.select_related("author")')


def test_related_manager_writes_are_not_reads(tmp_path: Path) -> None:
    # NetBox: `interface.vdcs.set(vdcs)` used to be suggested a prefetch.
    _write(
        tmp_path / "views.py",
        "def tag_all(tags):\n"
        "    for post in Post.objects.all():\n"
        "        post.tags.set(tags)\n"
        "        post.tags.add(tags[0])\n"
        "        post.comment_set.create(body='x')\n",
    )
    assert scan_for_nplusone(str(tmp_path), SCHEMA) == []


def test_write_after_loading_a_single_valued_relation_is_still_flagged(tmp_path: Path) -> None:
    # Healthchecks: `profile.user.delete()` loads profile.user first. The fix
    # is select_related, and only select_related.
    _write(
        tmp_path / "prune.py",
        "def prune():\n"
        "    for profile in Profile.objects.filter(email=''):\n"
        "        print(profile.user.email)\n"
        "        profile.user.delete()\n",
    )
    (finding,) = scan_for_nplusone(str(tmp_path), SCHEMA)
    assert finding.suggested_fix == '.select_related("user")'


def test_schema_less_scan_never_suggests_both_clauses_for_one_field(tmp_path: Path) -> None:
    _write(
        tmp_path / "prune.py",
        "def prune(pq):\n"
        "    pq = pq.filter(active=False)\n"
        "    for profile in pq:\n"
        "        print(profile.user.email)\n"
        "        profile.user.delete()\n",
    )
    for finding in scan_for_nplusone(str(tmp_path), None):
        assert 'prefetch_related("user")' not in finding.suggested_fix


def test_assigning_a_relation_is_not_reading_it(tmp_path: Path) -> None:
    # NetBox virtualization/views.py: `device.cluster = cluster` in a loop.
    _write(
        tmp_path / "views.py",
        "def assign(cluster, pks):\n"
        "    for post in Post.objects.filter(pk__in=pks):\n"
        "        post.author = cluster\n"
        "        post.save()\n",
    )
    assert scan_for_nplusone(str(tmp_path), SCHEMA) == []


def test_writing_through_a_relation_still_loads_it(tmp_path: Path) -> None:
    _write(
        tmp_path / "views.py",
        "def rename(pks):\n"
        "    for post in Post.objects.filter(pk__in=pks):\n"
        "        post.author.name = 'x'\n",
    )
    (finding,) = scan_for_nplusone(str(tmp_path), SCHEMA)
    assert finding.suggested_fix == '.select_related("author")'


def test_method_call_on_the_instance_is_not_a_relation(tmp_path: Path) -> None:
    # Healthchecks api/views.py: `check.matches_tag_set(tags)` matched the
    # `<model>_set` reverse-manager heuristic.
    _write(
        tmp_path / "views.py",
        "def listing(tags):\n"
        "    for post in Post.objects.all():\n"
        "        if post.matches_tag_set(tags):\n"
        "            print(post.to_dict())\n",
    )
    assert scan_for_nplusone(str(tmp_path), SCHEMA) == []
    assert scan_for_nplusone(str(tmp_path), None) == []


def test_test_modules_are_skipped_unless_asked(tmp_path: Path) -> None:
    loop = (
        "def check():\n"
        "    for post in Post.objects.all():\n"
        "        print(post.author.name)\n"
    )
    for rel in ("tests/test_views.py", "app/tests.py", "app/test_api.py", "conftest.py", "app/views_test.py"):
        _write(tmp_path / rel, loop)
    _write(tmp_path / "app" / "views.py", loop)

    files = {f.file for f in scan_for_nplusone(str(tmp_path), SCHEMA)}
    assert files == {"app/views.py"}
    everything = {f.file for f in scan_for_nplusone(str(tmp_path), SCHEMA, include_tests=True)}
    assert len(everything) == 6


def test_cli_include_tests_flag(tmp_path: Path) -> None:
    _write(
        tmp_path / "tests" / "test_views.py",
        "def check():\n"
        "    for post in Post.objects.all():\n"
        "        print(post.author.name)\n",
    )

    def run(*extra: str) -> list:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["nplusone", "--path", str(tmp_path), "--format", "json", "--exit-zero", *extra])
        return json.loads(out.getvalue())

    assert run() == []
    assert len(run("--include-tests")) == 1


def test_model_reached_through_the_models_module_is_resolved(tmp_path: Path) -> None:
    # babybuddy: `from core import models` + `models.Nap.objects.all()` was not
    # resolved, so a scalar `nap.duration.seconds` got select_related advice
    # (a FieldError at runtime) and the real FK was only `medium`.
    _write(
        tmp_path / "core" / "models.py",
        "from django.db import models\n"
        "\n"
        "\n"
        "class Child(models.Model):\n"
        "    name = models.CharField(max_length=50)\n"
        "\n"
        "\n"
        "class Nap(models.Model):\n"
        "    child = models.ForeignKey(Child, on_delete=models.CASCADE)\n"
        "    duration = models.DurationField()\n",
    )
    _write(
        tmp_path / "core" / "views.py",
        "from core import models\n"
        "\n"
        "\n"
        "def total_seconds():\n"
        "    total = 0\n"
        "    for nap in models.Nap.objects.all():\n"
        "        total += nap.duration.seconds\n"
        "    return total\n"
        "\n"
        "\n"
        "def names_loop():\n"
        "    out = []\n"
        "    for nap in core.models.Nap.objects.all():\n"
        "        out.append(nap.child.name)\n"
        "    return out\n",
    )
    schema = {"Child": {"name": "scalar"}, "Nap": {"child": "fk", "duration": "scalar"}}
    (finding,) = scan_for_nplusone(str(tmp_path), schema)
    assert finding.accessed == ["child"]
    assert finding.confidence == "high"
    assert finding.suggested_fix == '.select_related("child")'


def test_unknown_loop_model_skips_names_that_are_scalar_on_every_model(tmp_path: Path) -> None:
    # pretix exporters/events.py: `for e in self.events.all()` does not root at
    # a model, so `e.date_from.astimezone(tz)` got select_related("date_from").
    _write(
        tmp_path / "events" / "models.py",
        "from django.db import models\n"
        "\n"
        "\n"
        "class Organizer(models.Model):\n"
        "    name = models.CharField(max_length=100)\n"
        "\n"
        "\n"
        "class Event(models.Model):\n"
        "    organizer = models.ForeignKey(Organizer, on_delete=models.CASCADE, related_name=\"events\")\n"
        "    date_from = models.DateTimeField()\n"
        "    date_to = models.DateTimeField(null=True)\n",
    )
    _write(
        tmp_path / "events" / "exporters.py",
        "class EventExporter:\n"
        "    def __init__(self, organizer, tz):\n"
        "        self.organizer = organizer\n"
        "        self.tz = tz\n"
        "\n"
        "    def rows(self):\n"
        "        for e in self.organizer.events.all():\n"
        "            yield [\n"
        "                e.date_from.astimezone(self.tz).isoformat(),\n"
        "                e.date_to.astimezone(self.tz).isoformat() if e.date_to else \"\",\n"
        "            ]\n"
        "\n"
        "    def organizers(self):\n"
        "        for e in self.organizer.events.all():\n"
        "            yield e.organizer.name\n",
    )
    (finding,) = scan_for_nplusone(str(tmp_path), scan_workspace(str(tmp_path)))
    assert finding.line == 14
    assert finding.accessed == ["organizer"]
    assert finding.confidence == "medium"


def test_unknown_loop_model_still_guesses_names_that_are_a_relation_somewhere(tmp_path: Path) -> None:
    _write(
        tmp_path / "views.py",
        "def rows(things):\n"
        "    for x in things.all():\n"
        "        print(x.owner.name, x.title.upper(), x.missing.value)\n",
    )
    schema = {
        "Note": {"owner": "scalar", "title": "scalar"},
        "Ticket": {"owner": "fk"},
    }
    (finding,) = scan_for_nplusone(str(tmp_path), schema)
    assert finding.accessed == ["owner", "missing"]
    assert finding.confidence == "medium"
