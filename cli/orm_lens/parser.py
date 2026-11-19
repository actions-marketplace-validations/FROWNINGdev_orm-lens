"""Django models.py parser — Python port of the VS Code extension parser.

Static regex-based parser. Zero third-party deps. Produces a structurally
compatible schema with the TypeScript ``src/parser.ts`` (fields, relations,
Meta) — the cross-language golden fixture under ``test/`` pins the shared
shape. Note: indent detection on the Python side expands tabs to width 4
and clamps to 32 (ReDoS mitigation); the TypeScript side reports raw
character width. Both agree on the common 4-space case.

Key semantics preserved from the TS parser:
* multi-line ``class X(A, B):`` inheritance;
* non-model class filter (``ModelAdmin`` / ``ModelForm`` / ``Serializer`` etc.);
* per-class indent width detection (2/4/tab all work);
* both ``models.CharField(...)`` and bare ``CharField(...)`` imports;
* balanced-paren reader for multi-line field arg blocks;
* first-arg extraction for FK/O2O/M2M ``related_model``;
* ``class Meta:`` attribute capture.
"""

from __future__ import annotations

import ast
import functools
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from fnmatch import fnmatch
from pathlib import Path

from ._paths import BROAD_SKIP_DIRS, _app_dir_for, _package_dir
from .models import (
    ParsedApp,
    ParsedField,
    ParsedModel,
    WorkspaceIndex,
)
from .sqlalchemy_parser import (
    declares_sqlalchemy,
    is_sqlalchemy_only_source,
    scan_sqlalchemy_models,
)

RELATION_TYPES: tuple[str, ...] = ("ForeignKey", "ManyToManyField", "OneToOneField")

# django-mptt's Tree* fields are thin subclasses of Django's own relation
# fields — same ``to``, same ``on_delete``, same ``related_name``. Mapping the
# name to its Django counterpart is therefore the whole fix: every extractor
# below keeps working unchanged, and a ``TreeForeignKey('self', ...)`` produces
# the same self-edge a plain ``ForeignKey('self', ...)`` does.
TREE_FIELD_ALIASES: dict[str, str] = {
    "TreeForeignKey": "ForeignKey",
    "TreeOneToOneField": "OneToOneField",
    "TreeManyToManyField": "ManyToManyField",
}

# ``(?:\s*\[[^\]]*\])?`` — optional PEP-695 generic type parameter list
# introduced in Python 3.12, e.g. ``class Container[T](models.Model):``.
# Only handles a single-line, non-nested bracket group — nested brackets in
# class-header generics are vanishingly rare and don't warrant a full
# bracket matcher.
CLASS_RE = re.compile(
    r"^class\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s*\[[^\]]*\])?\s*"
    r"(?:\(([^)]*)\))?\s*:"
)
CLASS_START_RE = re.compile(
    r"^class\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s*\[[^\]]*\])?\s*\("
)

NON_MODEL_TAIL = re.compile(
    r"^(ModelAdmin|ModelForm|ModelSerializer|ModelChoiceField|"
    r"ModelMultipleChoiceField|Serializer|Form|Admin|View|ViewSet|"
    r"Manager|QuerySet|Config|AppConfig|Response|Handler|Middleware|"
    r"Backend|Command)$"
)

# ``name = models.Manager()`` declares a manager, not a column. FIELD_RE
# matches any ``models.<Name>(``, so these need an explicit skip in
# _collect_defs. TaggableManager is a real relation and must stay a field.
_NOT_A_FIELD = frozenset({"Manager", "QuerySet"})

BARE_FIELD_TYPES = "|".join(
    [
        "CharField", "TextField", "SlugField", "EmailField", "URLField", "UUIDField",
        "IntegerField", "BigIntegerField", "SmallIntegerField",
        "PositiveIntegerField", "PositiveSmallIntegerField", "PositiveBigIntegerField",
        "FloatField", "DecimalField",
        "BooleanField", "NullBooleanField",
        "DateTimeField", "DateField", "TimeField", "DurationField",
        "JSONField", "BinaryField",
        "GeneratedField",
        "ArrayField", "HStoreField", "SearchVectorField",
        "IntegerRangeField", "BigIntegerRangeField", "DecimalRangeField",
        "DateRangeField", "DateTimeRangeField",
        "CICharField", "CIEmailField", "CITextField",
        "FileField", "ImageField", "FilePathField",
        "GenericIPAddressField",
        "AutoField", "BigAutoField", "SmallAutoField",
        "ForeignKey", "OneToOneField", "ManyToManyField",
        "TaggableManager",
        "TreeForeignKey", "TreeOneToOneField", "TreeManyToManyField",
    ]
)
_BARE_FIELD_NAMES = frozenset(BARE_FIELD_TYPES.split("|"))
_FIELD_IMPORT_RE = re.compile(
    r"^from\s+(?:django\.db\.models(?:\.[\w.]+)?|django\.contrib\.postgres\.(?:fields(?:\.[\w.]+)?|search))"
    r"\s+import\s+"
)
_IMPORT_ALIAS_RE = re.compile(r"([A-Za-z_]\w*)\s+as\s+([A-Za-z_]\w*)")


def _read_multiline_class(lines: Sequence[str], start: int):
    """Read a possibly-multi-line ``class Foo(Bar,\n    Baz):`` header.

    Returns ``(match, end_line_idx)`` on success, ``(None, end_line_idx)``
    when the parens closed but the joined buffer did not match a class
    signature, and ``(None, start)`` when the wrap never closes (truncated
    or malformed file). Always a tuple.

    Advance semantics for the caller:
    * closed-but-no-match: use ``end_line_idx`` to skip past the whole
      wrapped section without re-scanning each continuation line;
    * never-closes: advance one line past ``start`` so the outer loop
      keeps making progress on the rest of the file instead of jumping
      to EOF (which used to drop any valid models below the malformed
      header).
    """
    buffer = lines[start]
    depth = 0
    saw_open = False
    for i in range(start, len(lines)):
        src = lines[i] if i == start else lines[i].lstrip()
        for ch in src:
            if ch == "(":
                depth += 1
                saw_open = True
            elif ch == ")":
                depth -= 1
        if i > start:
            buffer += " " + src
        if saw_open and depth == 0:
            m = CLASS_RE.match(buffer)
            if m:
                return m, i
            return None, i
    return None, start


_COND_OPENER_RE = re.compile(r"^\s*(if|elif|else|try|except|finally)\b")
_METHOD_OR_CLASS_RE = re.compile(r"^\s*(?:(?:async\s+)?def|class)\s+")


def _line_indent_width(line: str) -> int:
    return len(line) - len(line.lstrip())


def _find_block_header_end(lines: Sequence[str], start: int) -> int | None:
    """Return the line index holding the terminating ``:`` of a compound header."""
    depth = 0
    quote: str | None = None
    i = start
    while i < len(lines):
        line = lines[i]
        idx = 0
        n = len(line)
        while idx < n:
            ch = line[idx]
            if quote is not None:
                if ch == "\\" and idx + 1 < n:
                    idx += 2
                    continue
                if len(quote) == 3:
                    if line[idx : idx + 3] == quote:
                        quote = None
                        idx += 3
                        continue
                elif ch == quote:
                    quote = None
                idx += 1
                continue
            if ch == "#":
                break
            if line[idx : idx + 3] in ("'''", '"""'):
                quote = line[idx : idx + 3]
                idx += 3
                continue
            if ch in ("'", '"'):
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}" and depth > 0:
                depth -= 1
            elif ch == ":" and depth == 0:
                if idx + 1 < n and line[idx + 1] == "=":
                    idx += 2
                    continue
                rest = line[idx + 1 :].lstrip()
                if not rest or rest.startswith("#"):
                    return i
                return None
            idx += 1
        if depth == 0 and quote is None and not line.rstrip().endswith("\\"):
            return None
        i += 1
    return None


def _cond_block_header_end(lines: Sequence[str], start: int) -> tuple[str, int] | None:
    """If ``lines[start]`` opens a conditional/try clause header, return ``(keyword, end_line)``."""
    match = _COND_OPENER_RE.match(lines[start])
    if not match:
        return None
    end_line = _find_block_header_end(lines, start)
    if end_line is None:
        return None
    return match.group(1), end_line


@dataclass
class _FieldScope:
    items: list[ParsedField] = dataclass_field(default_factory=list)
    seen: set[str] = dataclass_field(default_factory=set)
    cond_names: set[str] = dataclass_field(default_factory=set)


@dataclass
class _CondChain:
    hdr_indent: int
    kind: str  # "if" or "try"
    body_indent: int | None = None
    branches: list[_FieldScope] = dataclass_field(default_factory=list)
    has_else: bool = False
    section: str = "branch"  # "branch", "try_else", "finally"
    try_else: _FieldScope = dataclass_field(default_factory=_FieldScope)
    finally_scope: _FieldScope = dataclass_field(default_factory=_FieldScope)

    def current_scope(self) -> _FieldScope:
        if self.section == "try_else":
            return self.try_else
        if self.section == "finally":
            return self.finally_scope
        return self.branches[-1]

    def can_continue(self, kw: str) -> bool:
        if self.kind == "if":
            return not self.has_else and kw in ("elif", "else")
        return kw in ("except", "else", "finally")

    def advance(self, kw: str) -> None:
        self.body_indent = None
        if self.kind == "if":
            if kw == "else":
                self.has_else = True
            self.branches.append(_FieldScope())
            return
        if kw == "except":
            self.section = "branch"
            self.branches.append(_FieldScope())
        elif kw == "else":
            self.section = "try_else"
        elif kw == "finally":
            self.section = "finally"


def _record_field(scope: _FieldScope, parsed_field: ParsedField, is_conditional: bool) -> None:
    if is_conditional and parsed_field.name in scope.seen:
        return
    replaced_conditional = (not is_conditional) and (parsed_field.name in scope.cond_names)
    scope.seen.add(parsed_field.name)
    if is_conditional:
        scope.cond_names.add(parsed_field.name)
    if replaced_conditional:
        scope.cond_names.discard(parsed_field.name)
        idx = next(k for k, existing in enumerate(scope.items) if existing.name == parsed_field.name)
        scope.items[idx] = parsed_field
    else:
        scope.items.append(parsed_field)


def _scope_first_by_name(scope: _FieldScope) -> dict[str, ParsedField]:
    by_name: dict[str, ParsedField] = {}
    for parsed_field in scope.items:
        by_name.setdefault(parsed_field.name, parsed_field)
    return by_name


def _intersect_branch_fields(scopes: Sequence[_FieldScope]) -> list[ParsedField]:
    if not scopes:
        return []
    first = _scope_first_by_name(scopes[0])
    common = set(first)
    for scope in scopes[1:]:
        common &= {parsed_field.name for parsed_field in scope.items}
    return [parsed_field for name, parsed_field in first.items() if name in common]


def _finalize_cond_chain(chain: _CondChain) -> list[ParsedField]:
    """Return fields guaranteed across every path of *chain*."""
    if chain.kind == "if":
        if not chain.has_else:
            return []
        return _intersect_branch_fields(chain.branches)

    guaranteed: list[ParsedField] = []
    seen: set[str] = set()
    success_scope = _FieldScope(items=[*chain.branches[0].items, *chain.try_else.items])
    if len(chain.branches) > 1:
        try_fields = _intersect_branch_fields([success_scope, *chain.branches[1:]])
    else:
        # With no `except`, an exception in the try body aborts the class body,
        # so every class that gets created ran the whole try body.
        try_fields = success_scope.items
    for parsed_field in try_fields:
        if parsed_field.name not in seen:
            seen.add(parsed_field.name)
            guaranteed.append(parsed_field)
    for parsed_field in chain.finally_scope.items:
        if parsed_field.name not in seen:
            seen.add(parsed_field.name)
            guaranteed.append(parsed_field)
    return guaranteed


def _detect_class_indent(lines: Sequence[str], class_line_idx: int) -> int:
    end = min(class_line_idx + 30, len(lines))
    for i in range(class_line_idx + 1, end):
        m = re.match(r"^([\t ]+)\S", lines[i])
        if m:
            raw = m.group(1)
            width = sum(4 if ch == "\t" else 1 for ch in raw)
            return min(width, 32)
    return 4


_BODY_REGEX_CACHE: dict = {}


def _build_body_regexes(indent: int):
    cached = _BODY_REGEX_CACHE.get(indent)
    if cached is not None:
        return cached
    w = r"\s{" + str(indent) + r"}"
    w2 = r"\s{" + str(indent * 2) + r"}"
    # ``(?:\s*:[^=]+)?`` — optional PEP-526 type annotation between the field
    # name and ``=``, e.g. ``jti: CharField[str] = models.CharField(...)``.
    # ``[^=]+`` is safe because ``=`` never appears inside a type expression
    # (subscripts, unions, dotted refs, and generics all use other punctuation).
    ann = r"(?:\s*:[^=]+)?"
    # ``(?:[A-Za-z_][A-Za-z0-9_]*\.)?`` — optional single-identifier prefix on
    # the RHS. Covers:
    #   * aliased models module — ``from django.db import models as m; x = m.CharField(...)``
    #   * third-party field packages — ``x = jsonfield.JSONField(default=dict)``
    #   * standard bare imports — ``from django.db.models import CharField; x = CharField(...)``
    # Safe against false positives because the type name is still restricted
    # to Django's known field whitelist (BARE_FIELD_TYPES).
    prefix_opt = r"(?:[A-Za-z_][A-Za-z0-9_]*\.)?"
    result = {
        "FIELD_RE": re.compile(
            rf"^{w}([a-zA-Z_][a-zA-Z0-9_]*){ann}\s*=\s*models\.([A-Za-z_][A-Za-z0-9_]*)\s*\("
        ),
        "BARE_FIELD_RE": re.compile(
            rf"^{w}([a-zA-Z_][a-zA-Z0-9_]*){ann}\s*=\s*{prefix_opt}({BARE_FIELD_TYPES})\s*\("
        ),
        "ALIASED_FIELD_RE": re.compile(
            rf"^{w}([a-zA-Z_][a-zA-Z0-9_]*){ann}\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*\("
        ),
        # ``a = None`` or chained ``a = b = None``; group 1 holds the targets.
        "REMOVED_RE": re.compile(
            rf"^{w}([a-zA-Z_][a-zA-Z0-9_]*(?:\s*=\s*[a-zA-Z_][a-zA-Z0-9_]*)*){ann}"
            r"\s*=\s*None\s*(?:#.*)?$"
        ),
        "META_START_RE": re.compile(rf"^{w}class\s+Meta\s*(?:\([^)]*\))?\s*:"),
        "META_ITEM_RE": re.compile(
            rf"^{w2}([a-zA-Z_][a-zA-Z0-9_]*){ann}\s*=\s*(.+?)\s*(#.*)?$"
        ),
        "META_BODY_RE": re.compile(r"^\s{" + str(indent * 2) + r",}"),
    }
    _BODY_REGEX_CACHE[indent] = result
    return result


def _dotted_name(node: ast.AST) -> str | None:
    """Best-effort textual dotted name of a Name/Attribute AST node.

    Returns e.g. ``"models.CASCADE"`` for ``ast.Attribute(value=Name("models"),
    attr="CASCADE")``. Returns ``None`` for anything else (Call, Constant,
    Subscript, ...).
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None


def _parse_call_args(args_block: str) -> tuple[list[ast.expr] | None, dict[str, ast.expr] | None]:
    """Parse a Django field's raw arg text via ``ast.parse``.

    ``args_block`` may include the field call's closing ``)``, as returned by
    :func:`_read_balanced_args`, or have it removed by the caller. Wraps the
    arguments as ``_(<content>)`` and parses them as an expression.

    Returns ``(posargs, kwargs)`` on success or ``(None, None)`` on
    :class:`SyntaxError`, so callers can fall back to the legacy regex path.
    """
    s = args_block.strip()
    try:
        tree = ast.parse(f"_({s})", mode="eval")
    except SyntaxError:
        # A final parenthesis can belong to a nested argument such as _('Name').
        # Drop it only if the original argument list could not be parsed.
        if not s.endswith(")"):
            return None, None
        try:
            tree = ast.parse(f"_({s[:-1]})", mode="eval")
        except SyntaxError:
            return None, None
    call = tree.body
    if not isinstance(call, ast.Call):
        return None, None
    posargs = [a for a in call.args if not isinstance(a, ast.Starred)]
    kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
    return posargs, kwargs


def _node_to_str(node: ast.AST) -> str | None:
    """String-constant or dotted-name value of ``node``, else ``None``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return _dotted_name(node)


def _extract_related(args_block: str) -> str | None:
    """Model reference from a FK/O2O/M2M field call.

    Recognises both positional (``ForeignKey('User', ...)``) and keyword
    (``ForeignKey(on_delete=..., to='User')``) forms — kwarg order does not
    matter. ``settings.AUTH_USER_MODEL``-style dotted refs are preserved
    as-is; downstream :func:`resolve_related_tail` normalises them.
    """
    posargs, kwargs = _parse_call_args(args_block)
    if posargs is None or kwargs is None:
        return _extract_related_regex(args_block)
    val = kwargs.get("to")
    if val is None and posargs:
        val = posargs[0]
    if val is None:
        return None
    raw = _node_to_str(val)
    if not raw:
        return None
    if raw == "self":
        return "self"
    return raw


def _extract_on_delete(args_block: str, positional: bool = True) -> str | None:
    """``on_delete`` policy tail (``CASCADE`` / ``SET_NULL`` / ...).

    Handles bare identifiers, ``models.`` prefixed forms, and the callable
    ``SET(default)`` form — all return the last dotted segment.

    With ``positional`` (ForeignKey, OneToOneField and their subclasses) the
    second positional argument counts as well: the signature is
    ``(to, on_delete, ...)``, and Django's own contrib apps write
    ``ForeignKey(ContentType, models.CASCADE)``. ManyToManyField has no
    ``on_delete``; its second positional is ``related_name``.
    """
    posargs, kwargs = _parse_call_args(args_block)
    if posargs is None or kwargs is None:
        return _extract_on_delete_regex(args_block, positional)
    od = kwargs.get("on_delete")
    if od is None and positional and len(posargs) >= 2:
        od = posargs[1]
    if od is None:
        return None
    if isinstance(od, ast.Call):
        callee = _dotted_name(od.func)
        if callee:
            return callee.rsplit(".", 1)[-1]
        return None
    name = _dotted_name(od)
    if name:
        return name.rsplit(".", 1)[-1]
    return None


def _extract_related_name(args_block: str) -> str | None:
    posargs, kwargs = _parse_call_args(args_block)
    if posargs is None or kwargs is None:
        return _extract_related_name_regex(args_block)
    val = kwargs.get("related_name")
    if isinstance(val, ast.Constant) and isinstance(val.value, str):
        return val.value
    return None


def _extract_through_model(args_block: str) -> str | None:
    posargs, kwargs = _parse_call_args(args_block)
    if posargs is None or kwargs is None:
        return _extract_through_model_regex(args_block)
    val = kwargs.get("through")
    if val is None:
        return None
    return _node_to_str(val)


# ---------------------------------------------------------------------------
# Regex fallbacks — used when ast.parse rejects the args block (rare: comments
# mid-expression, unbalanced quotes in string literals, etc.). Kept behaviour-
# compatible with pre-AST versions.
# ---------------------------------------------------------------------------


def _extract_related_regex(args_block: str) -> str | None:
    stripped = args_block.strip()
    m = re.match(
        r"^(?:to\s*=\s*)?(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_.]*))",
        stripped,
    )
    if not m:
        return None
    raw = m.group(1) or m.group(2) or m.group(3)
    if not raw:
        return None
    if raw == "self":
        return "self"
    return raw.strip("'\"")


def _extract_on_delete_regex(args_block: str, positional: bool = True) -> str | None:
    m = re.search(r"on_delete\s*=\s*(?:models\.)?([A-Z][A-Z_]+)", args_block)
    if m:
        return m.group(1)
    if re.search(r"on_delete\s*=\s*(?:models\.)?SET\s*\(", args_block):
        return "SET"
    if not positional:
        return None
    # ForeignKey(to, on_delete, ...): same shape as the TS parser accepts.
    m = _POSITIONAL_ON_DELETE_RE.match(args_block)
    if m:
        return m.group(1)
    if _POSITIONAL_SET_RE.match(args_block):
        return "SET"
    return None


_POSITIONAL_ON_DELETE_RE = re.compile(
    r"\s*[^,=()\[\]]+,\s*(?:[\w.]+\.)?([A-Z][A-Z_]*)\s*(?:[,)]|$)"
)
_POSITIONAL_SET_RE = re.compile(r"\s*[^,=()\[\]]+,\s*(?:[\w.]+\.)?SET\s*\(")


def _extract_related_name_regex(args_block: str) -> str | None:
    m = re.search(r"related_name\s*=\s*(?:'([^']+)'|\"([^\"]+)\")", args_block)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _extract_through_model_regex(args_block: str) -> str | None:
    m = re.search(
        r"\bthrough\s*=\s*(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_.]*))",
        args_block,
    )
    if not m:
        return None
    return m.group(1) or m.group(2) or m.group(3)


def _read_balanced_args(lines: Sequence[str], start: int):
    open_idx = lines[start].find("(")
    if open_idx == -1:
        return "", start

    depth = 1
    quote = ""
    parts: list[str] = []
    for i in range(start, len(lines)):
        src = lines[i][open_idx + 1 :] if i == start else lines[i]
        k = 0
        while k < len(src):
            ch = src[k]
            if quote:
                if ch == "\\":
                    k += 2
                    continue
                if src.startswith(quote, k):
                    k += len(quote)
                    quote = ""
                    continue
            elif ch == "#":
                break
            elif ch in "'\"":
                triple = ch * 3
                quote = triple if src.startswith(triple, k) else ch
                k += len(quote)
                continue
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    parts.append(src[: k + 1])
                    return "".join(parts), i
            k += 1
        # A single-quoted string can continue only via an escaped newline.
        if len(quote) == 1 and k <= len(src):
            quote = ""
        parts.extend((src, "\n"))
    return "".join(parts), len(lines) - 1


def _encloses_only_blocks(lines: Sequence[str], idx: int, indent: int) -> bool:
    """Is the indented statement at ``lines[idx]`` still at module level?

    Django's swappable-model convention wraps concrete models in a plain
    ``if``::

        if not is_model_registered("catalogue", "ProductClass"):

            class ProductClass(AbstractProductClass):
                pass

    That class is a module-level model in every sense that matters, but it is
    indented, and matching ``^class`` alone missed it — 21 of django-oscar's
    22 app ``models.py`` files parsed to nothing, losing catalogue, order,
    partner, payment, voucher and the rest of the framework.

    Walking outwards, every enclosing header must be a block statement
    (``if`` / ``try`` / ``with`` / ``for`` …). A ``def`` or ``class`` means
    the class is a local or a nested helper — ``class Meta`` is the common
    one — and must stay excluded.
    """
    want = indent
    for k in range(idx - 1, -1, -1):
        line = lines[k]
        if not line.strip():
            continue
        cur = len(line) - len(line.lstrip())
        if cur >= want:
            continue
        if line.lstrip().startswith(("def ", "async def ", "class ")):
            return False
        want = cur
        if want == 0:
            return True
    return True


def _looks_like_model(
    base_classes: list[str],
    is_workspace_defined: Callable[[str], bool] | None = None,
    is_model_root: Callable[[str], bool] | None = None,
) -> bool:
    """Does a base list identify a Django model from its names alone?

    A ``*Mixin`` suffix is a guess for third-party bases the scanner cannot
    see. When ``is_workspace_defined`` resolves the base to a class defined in
    the analysed workspace, that guess is skipped and the base's own chain
    decides. A plain ``class BaseURLMixin:`` helper must not turn its
    subclasses into models (issue #249), while a real
    ``class TimeStampMixin(models.Model)`` is still seeded through its
    ``models.Model`` base.
    """
    for b in base_classes:
        tail = b.split(".")[-1]
        if NON_MODEL_TAIL.match(tail):
            return False
        if re.search(r"models\.Model$", b):
            return True
        if re.match(
            r"^(Model|AbstractModel|AbstractBaseModel|TimeStampedModel"
            r"|PolymorphicModel|MPTTModel)$",
            tail,
        ):
            return True
        if is_model_root is not None and is_model_root(b):
            return True
        if re.match(r"^Abstract[A-Z]", tail):
            # The same rule as the ``Mixin`` branch below: a base defined in
            # the analysed workspace is resolved through its own chain, so a
            # plain ``class AbstractURLMixin:`` helper must not turn its
            # subclasses into models (#249 follow-up).
            if is_workspace_defined is not None and is_workspace_defined(b):
                continue
            return True
        if re.search(r"Mixin$", tail):
            if is_workspace_defined is not None and is_workspace_defined(b):
                continue
            return True
    return False


def parse_models_file(file_path: str, content: str) -> list[ParsedModel]:
    """Parse a single models.py-style file. Returns 0..N Django model classes.

    Two-pass: (1) collect every class def with base classes + Meta,
    (2) resolve transitive inheritance via fixed-point so ``Profile(TimeStamped)``
    where ``TimeStamped(models.Model)`` sits in the same file is correctly
    identified as a model. Classes with ``Meta.abstract = True`` are dropped.

    Resolution here is scoped to this single file's definitions. Use
    ``scan_workspace`` when bases may live in another parsed module (a
    shared abstract base imported from elsewhere in the project) — it
    collects definitions across every file first and resolves the union
    once, so a base defined outside this file is still found.
    """
    non_models: set[str] = set()
    defs = _collect_defs(file_path, content, non_models)
    resolver = _WorkspaceResolver(Path(file_path).parent, ())
    entry = _ModuleEntry(
        {d.name: d for d in defs},
        _parse_imports(content),
        frozenset(non_models),
    )
    p = Path(file_path)
    resolver.loaded[p] = entry
    resolver.loaded[resolver._resolved(p)] = entry
    return _resolve_and_filter(
        defs, resolver=resolver, known_non_models={file_path: non_models}
    )


# Column-0 statements that can't be part of a column-0 class body: the block
# headers ``_encloses_only_blocks`` walks through, plus ``def``, ``async def``
# and decorators.
_COLUMN0_STATEMENT_RE = re.compile(
    r"^(?:(?:if|elif|while|for|try|with|async\s+(?:def|for|with)|def)\b"
    r"|(?:else|except|finally)\b|@)"
)


def _lines_inside_strings(lines: Sequence[str]) -> frozenset[int]:
    """Return the indexes of lines that start inside a multi-line string.

    A light scanner, not ``tokenize``: model files that don't fully parse
    still need a best-effort answer. It follows ``#`` comments, single and
    triple quotes and backslash escapes, so a column-0 line of docstring
    text such as ``if needed, ...`` isn't mistaken for a statement.
    """
    inside: set[int] = set()
    quote = ""  # the open string's delimiter: ', ", ''' or """
    for idx, line in enumerate(lines):
        if quote:
            inside.add(idx)
        k = 0
        n = len(line)
        while k < n:
            ch = line[k]
            if quote:
                if ch == "\\":
                    k += 2
                    continue
                if line.startswith(quote, k):
                    k += len(quote)
                    quote = ""
                    continue
                k += 1
                continue
            if ch == "#":
                break
            if ch in "'\"":
                triple = ch * 3
                quote = triple if line.startswith(triple, k) else ch
                k += len(quote)
                continue
            k += 1
        # A single-quoted string only continues past a trailing backslash.
        if len(quote) == 1 and not line.endswith("\\"):
            quote = ""
    return frozenset(inside)


def _field_import_aliases(lines: Sequence[str], in_string: frozenset[int]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for i, line in enumerate(lines):
        match = _FIELD_IMPORT_RE.match(line)
        if not match or i in in_string:
            continue
        names = line[match.end():].partition("#")[0].strip()
        if names.startswith("("):
            j = i
            while ")" not in names and j + 1 < len(lines):
                j += 1
                names += " " + lines[j].partition("#")[0]
            names = names[1:].partition(")")[0]
        for name in names.split(","):
            alias = _IMPORT_ALIAS_RE.fullmatch(name.strip())
            if alias and alias[1] in _BARE_FIELD_NAMES:
                aliases[alias[2]] = alias[1]
    return aliases


def _pop_cond_chain(cond_stack: list[_CondChain], root_scope: _FieldScope) -> None:
    chain = cond_stack.pop()
    target_scope = cond_stack[-1].current_scope() if cond_stack else root_scope
    for pf in _finalize_cond_chain(chain):
        _record_field(target_scope, pf, is_conditional=True)


def _collect_defs(
    file_path: str,
    content: str,
    non_model_names: set[str] | None = None,
    field_aliases: dict[str, str] | None = None,
    scope: str = "",
) -> list[ParsedModel]:
    """Pass 1 only: collect every class def (name, bases, fields, Meta).

    *scope* (``"Outer."``) is the dotted path of the class body *content* was
    cut from, so the abstract models nested in its classes come back named,
    and their bases resolved, from the module down.

    No transitive resolution or abstract filtering — callers combine the
    result with definitions from other files before calling
    ``_resolve_and_filter``.

    Classes dropped because every base is a known non-model tail
    (``models.QuerySet``, ``models.Manager``, ...) are recorded in
    ``non_model_names`` when the caller passes a set. Their subclasses stay
    in the result, so the resolver needs those names to see that the chain
    ends in a non-model instead of falling back to the ``*Mixin`` guess
    (issue #249).
    """
    # Strip leading BOM (U+FEFF) — Windows editors (Notepad, VS Code with
    # certain settings) save UTF-8 files with a byte-order mark. Without this
    # the first line becomes "﻿class Foo..." and CLASS_RE fails to match.
    if content.startswith("﻿"):
        content = content[1:]
    # Expand tab characters to 4 spaces so the ``\s{indent}`` prefix used in
    # FIELD_RE / BARE_FIELD_RE / META_ITEM_RE matches tab-indented code too.
    # A single ``\t`` character otherwise contributes 1 to ``\s`` even though
    # it's visually 4 columns — tab-indented models used to have zero fields
    # detected. Line numbers are preserved (per-line expansion only).
    lines = [line.expandtabs(4) for line in content.splitlines()]
    all_defs: list[ParsedModel] = []
    in_string = _lines_inside_strings(lines)
    if field_aliases is None:
        field_aliases = _field_import_aliases(lines, in_string)
    app_name = _app_dir_for(file_path)[1] or "app"

    i = 0
    while i < len(lines):
        if i in in_string:
            i += 1
            continue
        line = lines[i]
        # A class indented inside a module-level ``if`` is still a model (see
        # _encloses_only_blocks). Match the dedented view so the ``^class``
        # anchor keeps meaning "start of the statement", not "column 0".
        class_indent = len(line) - len(line.lstrip())
        probe = line if class_indent == 0 else line.lstrip()
        class_match = CLASS_RE.match(probe)
        if class_match and class_indent and not _encloses_only_blocks(
            lines, i, class_indent
        ):
            i += 1
            continue
        class_header_end = i
        if not class_match and class_indent == 0 and CLASS_START_RE.match(line):
            joined_match, joined_end = _read_multiline_class(lines, i)
            if joined_match is not None:
                class_match = joined_match
                class_header_end = joined_end
            else:
                i = joined_end + 1
                continue
        if not class_match:
            i += 1
            continue

        model_name = class_match.group(1)
        base_classes = [
            s.strip()
            for s in (class_match.group(2) or "").split(",")
            if s.strip()
        ]
        # Drop classes whose ALL bases are known non-model tails
        # (ModelAdmin, Serializer, Manager, etc.) — they can't participate
        # in a model inheritance chain.
        if base_classes and all(
            NON_MODEL_TAIL.match(b.split(".")[-1]) for b in base_classes
        ):
            if non_model_names is not None:
                non_model_names.add(f"{scope}{model_name}")
            i += 1
            continue

        model = ParsedModel(
            name=model_name,
            app_name=app_name,
            file_path=file_path,
            line_number=i,
            base_classes=base_classes,
        )

        indent = _detect_class_indent(lines, class_header_end)
        rx = _build_body_regexes(indent)
        cond_stack: list[_CondChain] = []
        skip_indent: int | None = None
        root_scope = _FieldScope(items=model.fields)

        j = class_header_end + 1
        while j < len(lines):
            if j in in_string:
                j += 1
                continue
            inner = lines[j]
            # An indented class ends where the source dedents back out of it.
            # Column-0 classes keep the original terminator exactly, so files
            # without conditional models parse byte-for-byte as before.
            if (
                class_indent
                and inner.strip()
                and len(inner) - len(inner.lstrip()) <= class_indent
            ):
                break
            if re.match(r"^class\s+", inner) and not re.match(r"^\s+class", inner):
                break
            # A column-0 block opener (the swappable-model ``if``), function
            # or decorator also ends a column-0 class. Without it the body of
            # a plain class (``class OutOfStock(Exception)``) ran over every
            # following ``if not is_model_registered(...)`` block and hid the
            # models inside them (#226). Column-0 comments and blank lines
            # still stay inside the body. Lines inside strings were skipped above.
            if not class_indent and _COLUMN0_STATEMENT_RE.match(inner):
                break
            stripped = inner.strip()
            if not stripped or stripped.startswith("#"):
                j += 1
                continue

            line_indent = _line_indent_width(inner)
            if (cond_stack or skip_indent is not None) and stripped[0] in (")", "]", "}"):
                j += 1
                continue

            if skip_indent is not None:
                if line_indent > skip_indent:
                    j += 1
                    continue
                skip_indent = None

            cond_header = _cond_block_header_end(lines, j)

            while cond_stack:
                top = cond_stack[-1]
                if top.body_indent is None:
                    if line_indent > top.hdr_indent:
                        top.body_indent = line_indent
                        break
                    if (
                        cond_header is not None
                        and line_indent == top.hdr_indent
                        and top.can_continue(cond_header[0])
                    ):
                        break
                    _pop_cond_chain(cond_stack, root_scope)
                elif line_indent < top.body_indent:
                    if (
                        cond_header is not None
                        and line_indent == top.hdr_indent
                        and top.can_continue(cond_header[0])
                    ):
                        break
                    _pop_cond_chain(cond_stack, root_scope)
                else:
                    break

            if cond_stack and cond_header is not None:
                top = cond_stack[-1]
                if line_indent == top.hdr_indent and top.can_continue(cond_header[0]):
                    top.advance(cond_header[0])
                    j = cond_header[1] + 1
                    continue

            active_indent = (
                cond_stack[-1].body_indent
                if cond_stack and cond_stack[-1].body_indent is not None
                else indent
            )
            active_rx = rx if active_indent == indent else _build_body_regexes(active_indent)

            is_mptt_meta = line_indent == indent and bool(re.match(r"class\s+MPTTMeta\b", stripped))
            if not cond_stack and (rx["META_START_RE"].match(inner) or is_mptt_meta):
                k = j + 1
                while k < len(lines):
                    if k in in_string:
                        k += 1
                        continue
                    ml = lines[k]
                    if ml and not rx["META_BODY_RE"].match(ml) and ml.strip():
                        break
                    m2 = rx["META_ITEM_RE"].match(ml)
                    if m2:
                        value, k = _read_meta_value(lines, k, m2.group(2))
                        key = m2.group(1)
                        if not is_mptt_meta:
                            model.meta[key] = value
                        elif key in _MPTT_FIELD_DEFAULTS:
                            try:
                                field_name = ast.literal_eval(value)
                            except (ValueError, SyntaxError):
                                field_name = None
                            model.mptt_field_names[key] = (
                                field_name if isinstance(field_name, str) and field_name else None
                            )
                    k += 1
                j = k
                continue

            if line_indent == active_indent:
                if cond_header is not None and cond_header[0] in ("if", "try"):
                    cond_stack.append(
                        _CondChain(
                            hdr_indent=line_indent,
                            kind=cond_header[0],
                            branches=[_FieldScope()],
                        )
                    )
                    j = cond_header[1] + 1
                    continue
                if _METHOD_OR_CLASS_RE.match(inner):
                    skip_indent = line_indent
                    hdr_end = _find_block_header_end(lines, j)
                    j = hdr_end + 1 if hdr_end is not None else j + 1
                    continue

            if not cond_stack:
                removed = rx["REMOVED_RE"].match(inner)
                if removed:
                    # ``name = None`` drops a field inherited from an abstract base.
                    model.removed_fields.extend(re.split(r"\s*=\s*", removed[1]))
                    j += 1
                    continue

            alias_match = (
                active_rx["ALIASED_FIELD_RE"].match(inner) if field_aliases else None
            )
            aliased_type = field_aliases.get(alias_match[2]) if alias_match else None
            fm = (
                alias_match
                if aliased_type
                else active_rx["FIELD_RE"].match(inner)
                or active_rx["BARE_FIELD_RE"].match(inner)
            )
            if fm:
                fname, ftype = fm[1], aliased_type or fm[2]
                args_block, end_idx = _read_balanced_args(lines, j)
                if ftype in _NOT_A_FIELD:
                    j = end_idx + 1
                    continue
                relation_kind = (
                    "ManyToManyField"
                    if ftype == "TaggableManager"
                    else TREE_FIELD_ALIASES.get(ftype)
                    or (ftype if ftype in RELATION_TYPES else None)
                )
                is_rel = relation_kind is not None
                inner = args_block.removesuffix(")")
                parsed_field = ParsedField(
                    name=fname,
                    type=ftype,
                    args=inner.strip(),
                    is_relation=is_rel,
                    line_number=j,
                )
                if is_rel:
                    parsed_field.relation_kind = relation_kind  # type: ignore[assignment]
                    parsed_field.related_model = (
                        "taggit.Tag"
                        if ftype == "TaggableManager"
                        else _extract_related(inner)
                    )
                    parsed_field.on_delete = _extract_on_delete(
                        inner, positional=relation_kind != "ManyToManyField"
                    )
                    parsed_field.related_name = _extract_related_name(inner)
                    if relation_kind == "ManyToManyField":
                        explicit_through = _extract_through_model(inner)
                        parsed_field.through_model = (
                            explicit_through
                            if explicit_through is not None
                            else "taggit.TaggedItem"
                            if ftype == "TaggableManager"
                            else None
                        )
                target_scope = (
                    cond_stack[-1].current_scope() if cond_stack else root_scope
                )
                _record_field(target_scope, parsed_field, is_conditional=False)
                j = end_idx + 1
                continue

            j += 1

        while cond_stack:
            _pop_cond_chain(cond_stack, root_scope)

        all_defs.append(model)
        all_defs.extend(
            _nested_abstract_defs(
                file_path,
                lines,
                in_string,
                field_aliases,
                class_header_end + 1,
                j,
                indent,
                f"{scope}{model_name}",
                non_model_names,
            )
        )
        i = j

    return all_defs


_NESTED_CLASS_RE = re.compile(r"^\s*class\s+(?!Meta\b)([A-Za-z_]\w*)")


def _nested_abstract_defs(
    file_path: str,
    lines: Sequence[str],
    in_string: frozenset[int],
    field_aliases: dict[str, str],
    start: int,
    end: int,
    indent: int,
    outer: str,
    non_model_names: set[str] | None = None,
) -> list[ParsedModel]:
    """Abstract models declared in a class body, named ``Outer.Inner`` (#309).

    ``class NonUnique(models.Model)`` inside ``NamedMixin`` is a normal abstract
    model: Django copies its fields onto any class that names
    ``NamedMixin.NonUnique`` as a base. The body loop skips nested classes, so
    each one is parsed on its own, dedented, and kept only when abstract; a
    nested ``TextChoices`` or concrete class adds nothing, and its dotted name
    goes into *non_model_names* so a base naming it is not looked up by its
    last part.
    """
    out: list[ParsedModel] = []
    direct: list[ParsedModel] = []
    # Every nested class binds its name in the body, abstract model or not.
    bound: list[tuple[int, str]] = []
    k = start
    while k < end:
        header = None if k in in_string else _NESTED_CLASS_RE.match(lines[k])
        if not header or _line_indent_width(lines[k]) != indent:
            k += 1
            continue
        bound.append((k, header.group(1)))
        header_end = _find_block_header_end(lines, k)
        stop = (header_end if header_end is not None else k) + 1
        while stop < end and (
            stop in in_string
            or not lines[stop].strip()
            or lines[stop].lstrip().startswith("#")
            or _line_indent_width(lines[stop]) > indent
        ):
            stop += 1
        block = "\n".join(
            line[indent:] if not line[:indent].strip() else line.lstrip()
            for line in lines[k:stop]
        )
        # The block is parsed alone, so it gets the file's field-import aliases.
        for nested in _collect_defs(
            file_path,
            block,
            non_model_names,
            field_aliases=field_aliases,
            scope=f"{outer}.",
        ):
            if not _is_abstract(nested):
                continue
            # A class nested deeper comes back named and resolved in its own body.
            if "." not in nested.name:
                nested.name = f"{outer}.{nested.name}"
                direct.append(nested)
            nested.line_number += k
            for f in nested.fields:
                f.line_number += k
            out.append(nested)
        k = stop
    # A base naming a sibling defined above it (``class NonUnique(Common)``) is
    # that sibling: the class body is the scope its bases are read in, and only
    # names bound before the class statement are in it. A class body never sees
    # the enclosing one, so this applies to the direct children only.
    for d in direct:
        above = {name for line, name in bound if line < d.line_number}
        d.base_classes = [
            f"{outer}.{b}" if b.split(".")[0] in above else b for b in d.base_classes
        ]
    if non_model_names is not None:
        kept = {d.name for d in out}
        non_model_names.update(
            f"{outer}.{name}" for _, name in bound if f"{outer}.{name}" not in kept
        )
    return out


def _read_meta_value(
    lines: list[str], start: int, first_line: str
) -> tuple[str, int]:
    """Join a ``Meta`` entry that spans lines. Returns ``(value, last_index)``.

    ``Meta.indexes`` and ``Meta.constraints`` are almost always written one
    entry per line. The per-line regex captured only ``[`` for those, so every
    consumer saw an empty list and, in the case of ``suggest-index``, proposed
    indexes the model already had (#60). Continuation lines are pulled in
    until the brackets opened on the first line close again.

    A value that balances on its own first line is returned untouched, so
    every single-line ``Meta`` entry parses exactly as before.
    """
    def _depth(text: str) -> int:
        return (
            text.count("(") - text.count(")")
            + text.count("[") - text.count("]")
            + text.count("{") - text.count("}")
        )

    parts = [first_line.strip()]
    depth = _depth(parts[0])
    k = start
    # Bounded so an unclosed bracket cannot walk the rest of the file.
    while depth > 0 and k + 1 < len(lines) and k - start < 200:
        k += 1
        nxt = lines[k]
        if not nxt.strip():
            continue
        parts.append(nxt.strip())
        depth += _depth(nxt)
    return " ".join(parts).strip(), k


def _is_abstract(model: ParsedModel) -> bool:
    return model.meta.get("abstract", "").strip() in ("True", "1", "true")


def _pick_base(
    candidates: list[ParsedModel], model: ParsedModel
) -> ParsedModel | None:
    """Choose which same-named class ``model``'s base refers to.

    A base in the subclass's own directory wins (``order/models.py`` →
    ``order/abstract_models.py``). Failing that, a base in the same package
    (``order/models/line.py`` → ``order/abstract_models.py``), then one in the
    same app (``app/b/models.py`` → ``app/abstract_models.py`` when ``app`` is
    the enclosing app). Otherwise the first definition, as before.

    The package comes before the app: two sub-packages of one app
    (``app/a/``, ``app/b/``) can each define a ``Base``.
    """
    if not candidates:
        return None
    here = Path(model.file_path).parent
    for c in candidates:
        if Path(c.file_path).parent == here:
            return c
    package_dir = _package_dir(model.file_path)
    for c in candidates:
        if _package_dir(c.file_path) == package_dir:
            return c
    app_dir = _app_dir_for(model.file_path)[0]
    for c in candidates:
        if _app_dir_for(c.file_path)[0] == app_dir:
            return c
    return candidates[0]


def _resolved_base_name(
    model: ParsedModel,
    base: str,
    resolver: _WorkspaceResolver | None,
) -> str:
    """Class name that ``base`` refers to, following imports when possible.

    Falls back to the base's tail name for third-party bases and for the
    single-file ``parse_models_file`` path, where there is no import graph.
    """
    if resolver is not None:
        if resolver.is_model_root(Path(model.file_path), base):
            return "Model"
        found = resolver.resolve(Path(model.file_path), base)
        if found is not None:
            return found.name
    return base.split(".")[-1]


_MODEL_ROOT_TAIL = re.compile(
    r"^(Model|AbstractModel|AbstractBaseModel|TimeStampedModel"
    r"|PolymorphicModel|MPTTModel)$"
)
_DJANGO_MODEL_MODULE = re.compile(r"^django\.db\.models(\.base)?$")


def _has_model_ancestry(
    model: ParsedModel,
    by_name: dict[str, list[ParsedModel]],
    resolver: _WorkspaceResolver | None,
    seen: frozenset[tuple[str, str]] = frozenset(),
    known_non_models: dict[str, set[str]] | None = None,
) -> bool:
    """Whether ``model``'s base chain can reach a Django model root.

    ``Meta.abstract = True`` alone does not make a class an abstract *model*:
    Django copies fields only from bases that are models, so a class whose
    whole chain is workspace classes and known non-model tails
    (QuerySet/Manager and friends) must not contribute columns (#249
    follow-up).

    A base outside the workspace that the scanner cannot read is *unknown*,
    not proven non-model: it may itself be a Django model
    (``AbstractAudit(Group)``), and Django copies an abstract parent's local
    fields. Those stay inherited rather than being dropped on a name guess.
    """
    key = (
        str(resolver._resolved(Path(model.file_path)))
        if resolver is not None
        else model.file_path,
        model.name,
    )
    if key in seen:
        return False
    seen = seen | {key}
    non_models = (known_non_models or {}).get(model.file_path, ())
    for base in model.base_classes:
        tail = base.split(".")[-1]
        # ``object`` is never a model; without this it read as an unknown external base.
        if tail in non_models or base == "object":
            continue
        if re.search(r"models\.Model$", base) or _MODEL_ROOT_TAIL.match(tail):
            return True
        if resolver is not None and resolver.is_model_root(Path(model.file_path), base):
            return True
        known, parent = (
            resolver.lookup(Path(model.file_path), base)
            if resolver is not None
            else (False, None)
        )
        if parent is None and not known:
            parent = _pick_base(by_name.get(tail, []), model)
        if parent is not None:
            if parent is not model and _has_model_ancestry(
                parent, by_name, resolver, seen, known_non_models
            ):
                return True
            continue
        if known:
            # A workspace class the scanner dropped as a non-model
            # (a QuerySet/Manager mixin) cannot be an abstract model.
            continue
        # Unresolved external base: keep the inherited fields, as before.
        return True
    return False


def _django_field(name: str, type_: str, args: str, origin: str) -> ParsedField:
    return ParsedField(
        name=name, type=type_, args=args, is_relation=False, line_number=0,
        inherited_from=origin,
    )


_MPTT_FIELD_DEFAULTS = {
    "left_attr": "lft",
    "right_attr": "rght",
    "tree_id_attr": "tree_id",
    "level_attr": "level",
}


def _django_mptt_fields(field_names: dict[str, str | None]) -> list[ParsedField]:
    """Tree columns injected by django-mptt on models with its abstract base."""
    fields = []
    for option, default in _MPTT_FIELD_DEFAULTS.items():
        name = field_names.get(option, default)
        if name is None:
            continue
        args = "editable=False, db_index=True" if option == "tree_id_attr" else "editable=False"
        fields.append(_django_field(name, "PositiveIntegerField", args, "MPTTModel"))
    return fields


def _auth_m2m(name: str, target: str) -> ParsedField:
    return ParsedField(
        name=name,
        type="ManyToManyField",
        args=f'"{target}", blank=True, related_name="user_set", related_query_name="user"',
        is_relation=True,
        line_number=0,
        related_model=target,
        relation_kind="ManyToManyField",
        related_name="user_set",
        inherited_from="PermissionsMixin",
    )


_ABSTRACT_BASE_USER = (
    _django_field("password", "CharField", "max_length=128", "AbstractBaseUser"),
    _django_field("last_login", "DateTimeField", "blank=True, null=True", "AbstractBaseUser"),
)
_PERMISSIONS_MIXIN = (
    _django_field("is_superuser", "BooleanField", "default=False", "PermissionsMixin"),
    _auth_m2m("groups", "auth.Group"),
    _auth_m2m("user_permissions", "auth.Permission"),
)

# Fields of django.contrib.auth's abstract bases, which live in site-packages
# where a workspace scan never looks. ``is_active`` on AbstractBaseUser is a
# plain class attribute, not a field. A workspace class of the same name wins.
DJANGO_ABSTRACT_BASES: dict[str, tuple[ParsedField, ...]] = {
    "AbstractBaseUser": _ABSTRACT_BASE_USER,
    "PermissionsMixin": _PERMISSIONS_MIXIN,
    "AbstractUser": (
        *_ABSTRACT_BASE_USER,
        *_PERMISSIONS_MIXIN,
        _django_field("username", "CharField", "max_length=150, unique=True", "AbstractUser"),
        _django_field("first_name", "CharField", "max_length=150, blank=True", "AbstractUser"),
        _django_field("last_name", "CharField", "max_length=150, blank=True", "AbstractUser"),
        _django_field("email", "EmailField", "blank=True", "AbstractUser"),
        _django_field("is_staff", "BooleanField", "default=False", "AbstractUser"),
        _django_field("is_active", "BooleanField", "default=True", "AbstractUser"),
        _django_field("date_joined", "DateTimeField", "default=timezone.now", "AbstractUser"),
    ),
}


def _abstract_bases_fields(
    model: ParsedModel,
    by_name: dict[str, list[ParsedModel]],
    stack: tuple[tuple[str, str], ...] = (),
    resolver: _WorkspaceResolver | None = None,
    known_non_models: dict[str, set[str]] | None = None,
    mptt_overrides: dict[str, str | None] | None = None,
) -> list[ParsedField]:
    """Fields ``model`` inherits from its abstract bases, parent-first.

    Only *abstract* bases contribute. A concrete base is multi-table
    inheritance, where the parent keeps its own table and the child gains a
    ``parent_ptr`` rather than copies of the columns — counting those as the
    child's own would invent columns that no migration will ever create.

    Bases are walked in declaration order and the first declaration of a name
    wins, which is the direction Python's MRO resolves for the single-
    inheritance chains this actually matters for. A parent's own field, or its
    ``name = None``, hides that name from the parent's ancestors. ``stack``
    breaks cycles in malformed source instead of recursing forever.
    """
    out: list[ParsedField] = []
    taken: set[str] = set()
    model_key = (
        str(resolver._resolved(Path(model.file_path)))
        if resolver is not None
        else model.file_path,
        model.name,
    )
    current_stack = stack if stack else (model_key,)
    # django-mptt fills in options from bases after reading the child options.
    mptt_names = model.mptt_field_names | (mptt_overrides or {})
    model_path = Path(model.file_path)
    for base in model.base_classes:
        is_mptt_base = False
        known, parent = (
            resolver.lookup(model_path, base)
            if resolver is not None
            else (False, None)
        )
        target = parent.name if parent is not None else base.split(".")[-1]
        if parent is None:
            # A name the resolver says is a workspace class but not a model
            # (a dropped QuerySet/Manager base) cannot be an abstract model;
            # fall back to the name-only pick only for third-party bases.
            if known:
                continue
            is_mptt_base = (
                resolver is not None
                and resolver.model_root(model_path, base) == ("mptt.models", "MPTTModel")
            )
            if not is_mptt_base:
                parent = _pick_base(by_name.get(target, []), model)
        elif parent is model or (
            parent.name == model.name and parent.file_path == model.file_path
        ):
            parent = _pick_base(by_name.get(target, []), model)
        if parent is None:
            if is_mptt_base:
                contributed = _django_mptt_fields(mptt_names)
            else:
                contributed = [replace(f) for f in DJANGO_ABSTRACT_BASES.get(target, ())]
        elif parent is model or not _is_abstract(parent):
            continue
        else:
            parent_key = (
                str(resolver._resolved(Path(parent.file_path)))
                if resolver is not None
                else parent.file_path,
                parent.name,
            )
            if parent_key in current_stack:
                continue
            # ``Meta.abstract`` is not enough: Django copies fields only from an
            # abstract *model*, so a plain class marked abstract must not
            # contribute its attributes as columns (#249 follow-up).
            if not _has_model_ancestry(
                parent,
                by_name,
                resolver,
                frozenset({model_key}),
                known_non_models,
            ):
                continue
            hidden = {f.name for f in parent.fields} | set(parent.removed_fields)
            contributed = [
                *(
                    f
                    for f in _abstract_bases_fields(
                        parent,
                        by_name,
                        (*current_stack, parent_key),
                        resolver,
                        known_non_models,
                        mptt_names,
                    )
                    if f.name not in hidden
                ),
                *(
                    replace(f, inherited_from=f.inherited_from or parent.name)
                    for f in parent.fields
                ),
            ]
        for f in contributed:
            if f.name in taken:
                continue
            taken.add(f.name)
            out.append(f)
    return out


def _attach_inherited_fields(
    all_defs: list[ParsedModel],
    resolver: _WorkspaceResolver | None = None,
    known_non_models: dict[str, set[str]] | None = None,
) -> None:
    """Populate ``inherited_fields`` on every def, in place.

    Runs before the abstract classes are dropped — they are the only source
    of inherited fields, so the merge has to happen while they are still in
    the list. A field the class declares itself shadows the inherited one of
    the same name, exactly as an override does in Django.
    """
    by_name: dict[str, list[ParsedModel]] = {}
    for m in all_defs:
        # Keep every definition per name; ``_pick_base`` chooses one per
        # subclass. The resolver supplies the import graph for aliases.
        by_name.setdefault(m.name, []).append(m)
    for m in all_defs:
        own = {f.name for f in m.fields} | set(m.removed_fields)
        m.inherited_fields = [
            f
            for f in _abstract_bases_fields(
                m, by_name, resolver=resolver, known_non_models=known_non_models
            )
            if f.name not in own
        ]


def _resolve_and_filter(
    all_defs: list[ParsedModel],
    resolver: _WorkspaceResolver | None = None,
    known_non_models: dict[str, set[str]] | None = None,
) -> list[ParsedModel]:
    """Pass 2: transitive model resolution + abstract drop over ``all_defs``.

    ``all_defs`` may come from a single file (``parse_models_file``) or the
    union of every file in a scan (``scan_workspace``) — resolution matches
    each base's tail name against whatever definitions are present, so a
    base class defined in another file is found when the union is passed.

    A ``*Mixin`` base only suppresses the suffix guess when it resolves to a
    workspace class *from the subclass's own module*: a class in the same
    file, or one reached through that file's imports (including aliases).
    A same-named class elsewhere in the workspace must not hide a third-party
    mixin the scanner cannot see (a false negative).

    ``known_non_models`` carries the names ``_collect_defs`` dropped per
    file, so the single-file path treats a QuerySet/Manager base as
    workspace-defined too.
    """
    _attach_inherited_fields(all_defs, resolver, known_non_models)
    if resolver is None:
        names_by_file: dict[str, set[str]] = {}
        for m in all_defs:
            names_by_file.setdefault(m.file_path, set()).add(m.name)
        non_models_by_file = known_non_models or {}

        def is_workspace_defined(model: ParsedModel, base: str) -> bool:
            tail = base.split(".")[-1]
            return (
                tail in names_by_file.get(model.file_path, ())
                or tail in non_models_by_file.get(model.file_path, ())
            )
    else:
        known_def_names = {m.name for m in all_defs}

        def is_workspace_defined(model: ParsedModel, base: str) -> bool:
            found, target_def = resolver.lookup(Path(model.file_path), base)
            return bool(
                found
                and (target_def is None or target_def.name in known_def_names)
            )

    is_model_name = {
        m.name
        for m in all_defs
        if _looks_like_model(
            m.base_classes,
            functools.partial(is_workspace_defined, m),
            is_model_root=functools.partial(resolver.is_model_root, Path(m.file_path))
            if resolver is not None
            else None,
        )
    }
    resolved_bases = {
        id(m): [_resolved_base_name(m, b, resolver) for b in m.base_classes]
        for m in all_defs
    }
    changed = True
    while changed:
        changed = False
        for m in all_defs:
            if m.name in is_model_name:
                continue
            for target in resolved_bases[id(m)]:
                if target in is_model_name:
                    is_model_name.add(m.name)
                    changed = True
                    break

    result: list[ParsedModel] = []
    for m in all_defs:
        if m.name not in is_model_name:
            continue
        abstract_val = m.meta.get("abstract", "").strip()
        if abstract_val in ("True", "1", "true"):
            continue
        result.append(m)
    return result


_FROM_IMPORT_RE = re.compile(
    r"^[ \t]*from[ \t]+(\.+[\w.]*|[\w.]+)[ \t]+import[ \t]+(?:\(([^)]*)\)|([^\n]*))",
    re.M,
)
_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+([^\n]+)", re.M)

#: How many re-exports a base may sit behind (``core/db/__init__.py`` doing
#: ``from .bases import SoftDeletable`` is one). Bounds a malformed import
#: cycle rather than any real project.
_MAX_IMPORT_HOPS = 5


def _parse_imports(content: str) -> dict[str, tuple[str, str | None]]:
    """Map each name a module binds by import to ``(module, imported_name)``.

    ``from core.base import MyModel as M`` gives ``M -> ("core.base", "MyModel")``;
    ``import core.base as cb`` gives ``cb -> ("core.base", None)``, and a bare
    ``import core.base`` binds its head, ``core -> ("core", None)``. Regex
    rather than ``ast`` so ``src/parser.ts`` can read imports the same way.
    """
    out: dict[str, tuple[str, str | None]] = {}
    for m in _FROM_IMPORT_RE.finditer(content):
        names = m.group(2) if m.group(2) is not None else m.group(3)
        for raw in names.split(","):
            item = raw.split("#")[0].strip().rstrip("\\").strip()
            parts = item.split()
            if not parts or parts[0] == "*":
                continue
            alias = parts[2] if len(parts) == 3 and parts[1] == "as" else parts[0]
            out.setdefault(alias, (m.group(1), parts[0]))
    for m in _IMPORT_RE.finditer(content):
        for raw in m.group(1).split("#")[0].split(","):
            parts = raw.strip().split()
            if not parts:
                continue
            if len(parts) == 3 and parts[1] == "as":
                out.setdefault(parts[2], (parts[0], None))
            else:
                head = parts[0].split(".")[0]
                out.setdefault(head, (head, None))
    return out


def _join_module(module: str, name: str) -> str:
    return module + name if module.endswith(".") else f"{module}.{name}"


def _module_files(module: str, from_file: str, root: Path) -> list[Path]:
    """Candidate files for ``module`` as imported from ``from_file``.

    A relative module resolves against the importing file. An absolute one is
    tried from every directory between the importing file and ``root``, since
    the scan root is not necessarily the import root (``src/`` layouts, a repo
    holding several projects). Files outside ``root`` are never returned.
    """
    dots = len(module) - len(module.lstrip("."))
    parts = [p for p in module[dots:].split(".") if p]
    # Callers pass an already-resolved file, so no second ``resolve()`` here.
    here = Path(from_file).parent
    # ``from .x`` is this package, each extra dot one directory up.
    lineage = [here, *here.parents]
    bases = lineage[dots - 1 : dots] if dots else lineage
    out: list[Path] = []
    for base in bases:
        if base != root and root not in base.parents:
            break
        target = base.joinpath(*parts)
        if parts:
            out.append(target.with_name(target.name + ".py"))
        out.append(target / "__init__.py")
    return out


@dataclass(frozen=True)
class _ModuleEntry:
    """Parsed module contents used to resolve base names."""

    defs: dict[str, ParsedModel]
    imports: dict[str, tuple[str, str | None]]
    non_models: frozenset[str]


class _WorkspaceResolver:
    """Per-file import bindings and class defs for resolving base names.

    Each module is cached as a :class:`_ModuleEntry`: its defs by name, the
    local binding map from :func:`_parse_imports`, and the class names
    ``_collect_defs`` dropped as known non-models. That map is what makes an
    aliased base resolve: ``from .mixins import StateQuerySetMixin as
    StateMixin`` binds ``StateMixin`` to the original name and module, so the
    definition is found instead of guessed at by suffix. Modules outside
    ``root`` or matching an exclude are never read.

    ``lookup`` results are cached on the instance, keyed by ``(path, base)``.
    A new resolver is created for every scan, so a later scan in the same
    process never sees stale results.
    """

    def __init__(self, root: Path, excludes: Sequence[str] = ()) -> None:
        self.root = root
        self.excludes = excludes
        self.loaded: dict[Path, _ModuleEntry | None] = {}
        self._resolved_paths: dict[Path, Path] = {}
        self._lookups: dict[
            tuple[Path, str], tuple[bool, ParsedModel | None]
        ] = {}
        self._model_roots: dict[tuple[Path, str], tuple[str, str] | None] = {}

    def _resolved(self, path: Path) -> Path:
        """``path.resolve()`` once per path for the lifetime of the scan."""
        resolved = self._resolved_paths.get(path)
        if resolved is None:
            resolved = path.resolve()
            self._resolved_paths[path] = resolved
        return resolved

    def load(self, path: Path) -> _ModuleEntry | None:
        if path in self.loaded:
            return self.loaded[path]
        if not path.is_file():
            # Most candidates from _module_files do not exist. Resolving a
            # missing path still walks the filesystem on Windows, so cache the
            # miss before paying for it.
            self.loaded[path] = None
            return None
        path = self._resolved(path)
        if path in self.loaded:
            return self.loaded[path]
        self.loaded[path] = None
        try:
            rel = path.relative_to(self.root).as_posix()
        except ValueError:
            return None
        if any(fnmatch(rel, pat) for pat in self.excludes) or not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            defs: dict[str, ParsedModel] = {}
            non_models: set[str] = set()
            # A SQLAlchemy base (``class CoreModel(Model)`` on
            # Flask-AppBuilder) must not turn its importers into Django models.
            if not is_sqlalchemy_only_source(content):
                for d in _collect_defs(str(path), content, non_models):
                    defs.setdefault(d.name, d)
        except Exception:
            return None
        entry = _ModuleEntry(defs, _parse_imports(content), frozenset(non_models))
        self.loaded[path] = entry
        return entry

    def _lookup(
        self, path: Path, base: str, hops: int = 0
    ) -> tuple[bool, ParsedModel | None]:
        """Resolve ``base`` from ``path`` to ``(workspace_defined, def)``.

        ``workspace_defined`` is true when the name resolves to *any* class in
        the workspace, including one ``_collect_defs`` dropped as a known
        non-model (a QuerySet/Manager mixin); ``def`` is then ``None``.
        """
        entry = self.load(path)
        if entry is None or hops > _MAX_IMPORT_HOPS:
            return False, None
        defs, imports, non_models = entry.defs, entry.imports, entry.non_models
        *qual, name = base.split(".")
        if not qual:
            if name in defs:
                return True, defs[name]
            if name in non_models:
                return True, None
            target = imports.get(name)
            if target is None or target[1] is None:
                return False, None
            module, name = target[0], target[1]
        else:
            # ``Outer.Inner``: a class nested in a class (#309), defined here or
            # in the module ``Outer`` is imported from.
            if base in defs:
                return True, defs[base]
            if base in non_models:
                return True, None
            head = imports.get(qual[0])
            if head is None:
                module = qual[0]
            elif head[1] is None:
                module = head[0]
            else:
                nested = ".".join((head[1], *qual[1:], name))
                for candidate in _module_files(head[0], str(path), self.root):
                    found = self._lookup(candidate, nested, hops + 1)
                    if found[0]:
                        return found
                module = _join_module(head[0], head[1])
            for part in qual[1:]:
                module = _join_module(module, part)
        for candidate in _module_files(module, str(path), self.root):
            found = self._lookup(candidate, name, hops + 1)
            if found[0]:
                return found
        return False, None

    def lookup(
        self, path: Path, base: str
    ) -> tuple[bool, ParsedModel | None]:
        """Cached :meth:`_lookup` for the ``(path, base)`` pair."""
        path = self._resolved(path)
        key = (path, base)
        cached = self._lookups.get(key)
        if cached is not None:
            return cached
        result = self._lookup(path, base)
        self._lookups[key] = result
        return result

    def resolve(self, path: Path, base: str, hops: int = 0) -> ParsedModel | None:
        """Follow ``base`` from ``path`` through imports to a class def.

        ``base`` may be a bare name, an aliased import, or a dotted
        ``module.Name`` / ``pkg.module.Name`` reference. Re-exports are
        followed up to :data:`_MAX_IMPORT_HOPS`; anything that escapes the
        workspace resolves to ``None``. Known non-models resolve to ``None``
        here too; use :meth:`lookup` to tell them apart from third-party
        names.
        """
        if hops:
            return self._lookup(path, base, hops)[1]
        return self.lookup(path, base)[1]

    def _model_root(self, path: Path, base: str, hops: int = 0) -> tuple[str, str] | None:
        if hops > _MAX_IMPORT_HOPS:
            return None
        entry = self.load(path)
        if entry is None:
            return None
        defs, imports = entry.defs, entry.imports
        *qual, name = base.split(".")
        if not qual:
            if name in defs or name in entry.non_models:
                return None
            target = imports.get(name)
            if target is None:
                return None
            module, target_name = target[0], target[1]
            if target_name is None:
                return None
        else:
            head = imports.get(qual[0])
            if head is None:
                module = qual[0]
            elif head[1] is None:
                module = head[0]
            else:
                module = _join_module(head[0], head[1])
            for part in qual[1:]:
                module = _join_module(module, part)
            target_name = name

        if target_name == "Model" and _DJANGO_MODEL_MODULE.match(module):
            return module, target_name
        if _MODEL_ROOT_TAIL.match(target_name) and (
            _DJANGO_MODEL_MODULE.match(module)
            or module in ("model_utils.models", "polymorphic.models", "mptt.models")
        ):
            return module, target_name

        for candidate in _module_files(module, str(path), self.root):
            root = self._model_root(candidate, target_name, hops + 1)
            if root is not None:
                return root
        return None

    def model_root(self, path: Path, base: str) -> tuple[str, str] | None:
        """Resolve a Django model root to its import module and class name."""
        path = self._resolved(path)
        key = (path, base)
        if key not in self._model_roots:
            self._model_roots[key] = self._model_root(path, base)
        return self._model_roots[key]

    def is_model_root(self, path: Path, base: str) -> bool:
        """Does ``base`` from ``path`` resolve to a Django model root?"""
        return self.model_root(path, base) is not None


def _pull_imported_bases(
    all_defs: list[ParsedModel],
    root: Path,
    excludes: Sequence[str],
    resolver: _WorkspaceResolver | None = None,
) -> list[ParsedModel]:
    """Definitions of base classes that live outside the scanned model files.

    The scan reads ``models.py``-style files only, so ``class Person(MyBase)``
    with ``MyBase`` in ``core/base.py`` had nothing to resolve against and
    ``Person`` vanished (issue #135). This follows the subclass's own imports
    to the file that defines each unknown base — through re-exports and
    relative imports — and returns those classes so ``_resolve_and_filter``
    sees the whole chain. Only classes something actually inherits from are
    pulled, so helpers sharing that file never show up as models.
    """
    if resolver is None:
        resolver = _WorkspaceResolver(root, excludes)

    # Identity is (file, class), not just the class name: the same name may be
    # defined in several apps, and an import decides which one a base means.
    def norm(path: str) -> str:
        return str(resolver._resolved(Path(path)))

    known_defs = {(norm(m.file_path), m.name) for m in all_defs}
    queue = [(norm(m.file_path), b) for m in all_defs for b in m.base_classes]
    asked: set[tuple[str, str]] = set()
    pulled: list[ParsedModel] = []
    while queue:
        file_path, base = queue.pop()
        if (file_path, base) in asked:
            continue
        asked.add((file_path, base))
        found = resolver.resolve(Path(file_path), base)
        if found is None:
            continue
        key = (norm(found.file_path), found.name)
        if key in known_defs:
            continue
        known_defs.add(key)
        pulled.append(found)
        queue.extend((norm(found.file_path), b) for b in found.base_classes)
    return pulled


#: Files that declare models but are not named ``models.py``. Pluggable
#: Django frameworks keep their abstract bases here and leave ``models.py``
#: holding only the concrete subclasses — django-oscar does this in all 14 of
#: its apps. Without reading these, oscar's models parse as 82 classes with
#: zero fields between them: found, but empty, which reads as a schema that
#: lost its columns.
MODEL_SOURCE_FILES = frozenset({"models.py", "abstract_models.py"})


def _iter_python_files(root: Path, excludes: Sequence[str]) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name in MODEL_SOURCE_FILES or (
                os.path.basename(dirpath) == "models"
                and name.endswith(".py")
                and name != "__init__.py"
            ):
                full = Path(dirpath) / name
                rel_posix = full.relative_to(root).as_posix()
                if any(fnmatch(rel_posix, pat) for pat in excludes):
                    continue
                yield full


DEFAULT_EXCLUDES = (
    "**/migrations/**",
    "**/node_modules/**",
    "**/venv/**",
    "**/.venv/**",
    "**/env/**",
)


def iter_workspace_py_files(
    root: Path, extra_skip: frozenset = frozenset()
) -> Iterable[Path]:
    """Yield every ``.py`` file under ``root`` except vendored / VCS junk.

    Uses :data:`BROAD_SKIP_DIRS` plus ``extra_skip`` (e.g.
    ``{"site-packages", "dist"}`` for the n+1 scanner). Dot-prefixed
    directories are always skipped. Consolidates three earlier per-module
    copies of the same walk so a change to the skip list stays in one place.
    """
    skip = BROAD_SKIP_DIRS | extra_skip
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in skip
        ]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


def scan_workspace(
    root: str,
    exclude_globs: Sequence[str] = DEFAULT_EXCLUDES,
    include_sqlalchemy: bool = False,
) -> WorkspaceIndex:
    """Walk ``root``, parse every ``models.py`` (or ``models/*.py``), return an index.

    Collects class definitions from every file first, then resolves
    transitive model inheritance once over that union — so a model whose
    base class is defined in a *different* scanned file (e.g. a shared
    abstract base in ``common/models.py``) is still recognized, instead of
    only bases found within the same file (see issue #20).

    ``include_sqlalchemy`` adds the workspace's SQLAlchemy mapped classes
    (``orm = "sqlalchemy"``), found anywhere in the tree by
    :mod:`.sqlalchemy_parser`. Off by default: the Django-only analyses
    (drift, blast radius, cascade, signals, ...) must never see them. A
    ``models.py`` that is SQLAlchemy code without a Django import is never
    read as Django models, whichever way the flag is set.
    """
    root_path = Path(root).resolve()
    all_defs: list[ParsedModel] = []
    scanned_files = 0
    for py in _iter_python_files(root_path, exclude_globs):
        scanned_files += 1
        try:
            content = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if is_sqlalchemy_only_source(content):
            continue
        try:
            all_defs.extend(_collect_defs(str(py), content))
        except Exception as exc:
            print(
                f"warning: skipping {py}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue
    resolver = _WorkspaceResolver(root_path, exclude_globs)
    all_defs.extend(
        _pull_imported_bases(all_defs, root_path, exclude_globs, resolver)
    )

    found = _resolve_and_filter(all_defs, resolver)
    # Finding SQLAlchemy models means reading every file, which a big Django
    # project should not pay for: look only when there are no Django models
    # or the project declares SQLAlchemy.
    if include_sqlalchemy and (not found or declares_sqlalchemy(root_path)):
        sa_models, sa_files = scan_sqlalchemy_models(root_path, exclude_globs)
        found = [*found, *sa_models]
        scanned_files += sa_files

    apps: dict = {}
    for model in found:
        app_dir, app_name = _app_dir_for(model.file_path)
        app = apps.get(app_dir)
        if app is None:
            app = ParsedApp(name=app_name, path=app_dir)
            apps[app_dir] = app
        app.models.append(model)

    sorted_apps = sorted(apps.values(), key=lambda a: a.name)
    return WorkspaceIndex(
        apps=sorted_apps,
        scanned_at=int(time.time() * 1000),
        scanned_files=scanned_files,
    )
