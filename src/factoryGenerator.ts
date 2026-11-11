import { ParsedField, ParsedModel, WorkspaceIndex } from './types';

/**
 * Factory generator — turn a `ParsedModel` into a factory_boy scaffold.
 *
 * factory_boy is the canonical test-fixture library in the Django world:
 * ~4M downloads/month on PyPI, first-class docs on the Django site. Users
 * hate writing the boilerplate; every model needs a factory, and every
 * factory needs Faker providers keyed by field type.
 *
 * We produce a string of Python code with:
 *
 *   - a `factory` + `factory_boy` + `faker` import shim
 *   - one `DjangoModelFactory` subclass per target model
 *   - a Faker provider chosen per field type (see `providerFor`)
 *   - SubFactory references for FKs pointing at other models we know about
 *   - a `post_generation` hook for M2M with a `through` model
 *
 * The result is opened as an untitled document — the user reviews, edits,
 * and saves wherever their team prefers (`app/factories.py`, `conftest.py`,
 * a dedicated `tests/factories/` package).
 *
 * Public API: `generateFactoryCode(model, index)` returns Python source.
 * Callers: `src/extension.ts` command handler + `codeLensProvider.ts`
 * CodeLens above each model class + tree context menu.
 */

/** Map a Django field type to a Faker provider expression. */
function providerFor(field: ParsedField): string {
  const t = field.type;
  // Choices in `args` (e.g. `choices=STATUS_CHOICES`) — favour the choices.
  if (/\bchoices\s*=/.test(field.args)) {
    return choicesProvider(field.args);
  }

  switch (t) {
    case 'EmailField':
      return "factory.Faker('email')";
    case 'URLField':
      return "factory.Faker('url')";
    case 'SlugField':
      return "factory.Faker('slug')";
    case 'UUIDField':
      return 'factory.Faker(\'uuid4\')';
    case 'IPAddressField':
    case 'GenericIPAddressField':
      return "factory.Faker('ipv4')";
    case 'BooleanField':
    case 'NullBooleanField':
      return "factory.Faker('pybool')";
    case 'DateField':
      return "factory.Faker('date_object')";
    case 'DateTimeField':
      return "factory.Faker('date_time', tzinfo=timezone.utc)";
    case 'TimeField':
      return "factory.Faker('time_object')";
    case 'DurationField':
      return "datetime.timedelta(days=1)";
    case 'JSONField':
      return "factory.Dict({})";
    case 'BinaryField':
      return "b''";
    case 'FileField':
      return "factory.django.FileField(filename='sample.txt')";
    case 'ImageField':
      return "factory.django.ImageField(color='blue')";
    case 'FilePathField':
      return "factory.Faker('file_path')";
    case 'DecimalField': {
      const maxDigits = matchIntKwarg(field.args, 'max_digits') ?? 10;
      const decimalPlaces = matchIntKwarg(field.args, 'decimal_places') ?? 2;
      return `factory.Faker('pydecimal', left_digits=${
        maxDigits - decimalPlaces
      }, right_digits=${decimalPlaces})`;
    }
    case 'FloatField':
      return "factory.Faker('pyfloat')";
    case 'IntegerField':
    case 'SmallIntegerField':
    case 'BigIntegerField':
    case 'PositiveIntegerField':
    case 'PositiveSmallIntegerField':
    case 'PositiveBigIntegerField': {
      const isPositive = t.startsWith('Positive');
      const min = isPositive ? 0 : -1000;
      return `factory.Faker('random_int', min=${min}, max=1000)`;
    }
    case 'AutoField':
    case 'BigAutoField':
    case 'SmallAutoField':
      // factory_boy leaves auto-pk to the DB; skip.
      return '';
    case 'CharField': {
      const maxLen = matchIntKwarg(field.args, 'max_length') ?? 100;
      if (maxLen <= 20) return `factory.Faker('word')`;
      if (maxLen <= 80) return `factory.Faker('sentence', nb_words=4)`;
      return `factory.Faker('sentence', nb_words=10)`;
    }
    case 'TextField':
      return "factory.Faker('paragraph')";
  }

  // GeoDjango types
  if (/^Point/.test(t) || /^Polygon/.test(t) || /^LineString/.test(t)) {
    return "None  # TODO: build a valid GeoDjango geometry";
  }

  // Fallback for anything unknown.
  return `factory.Faker('word')  # TODO: replace with a suitable provider for ${t}`;
}


/** Build a factory.Iterator expression from a `choices=` argument. */
function choicesProvider(args: string): string {
  const expr = extractChoicesExpr(args);
  const fallback = `factory.Iterator([...])  # TODO: fill from ${extractChoiceName(args) ?? 'choices'}`;
  if (!expr) return fallback;

  // TextChoices / IntegerChoices: Kind.choices → Kind.values
  const enumMatch = /^([A-Za-z_][\w]*)\.choices$/.exec(expr);
  if (enumMatch) {
    return `factory.Iterator(${enumMatch[1]}.values)  # TODO: import ${enumMatch[1]} (or <Model>.${enumMatch[1]} if it is nested)`;
  }

  // A name or dotted name, e.g. STATUS_CHOICES or shop.STATUS_CHOICES.
  if (/^[A-Za-z_][\w.]*$/.test(expr)) {
    return `factory.Iterator([value for value, _label in ${expr}])  # TODO: import ${expr}`;
  }

  const values = inlineChoiceValues(expr);
  if (values) {
    return `factory.Iterator([${values.join(', ')}])`;
  }

  return fallback;
}

/** The raw `choices=` value: a name, or a balanced list/tuple. */
function extractChoicesExpr(args: string): string | undefined {
  const m = /\bchoices\s*=\s*/.exec(args);
  if (!m) return undefined;
  const rest = args.slice(m.index + m[0].length);
  // Only a plain name or bracket group can be read. Anything after it other
  // than the next argument (a call, subscript or operator such as `+`) would
  // make the iterator miss values, so leave the placeholder instead.
  const endsExpr = (tail: string): boolean => {
    const t = tail.trimStart();
    return t === '' || t.startsWith(',');
  };
  if (rest.startsWith('[') || rest.startsWith('(')) {
    const end = matchingBracket(rest);
    if (end < 0 || !endsExpr(rest.slice(end + 1))) return undefined;
    return rest.slice(0, end + 1);
  }
  const name = /^[A-Za-z_][\w.]*/.exec(rest)?.[0];
  if (!name) return undefined;
  if (!endsExpr(rest.slice(name.length))) return undefined;
  return name;
}

function matchingBracket(src: string): number {
  const stack: string[] = [];
  let quote: string | null = null;
  for (let i = 0; i < src.length; i++) {
    const ch = src[i];
    if (quote) {
      if (ch === '\\') {
        i++;
        continue;
      }
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      continue;
    }
    if (ch === '(' || ch === '[') stack.push(ch === '(' ? ')' : ']');
    else if (ch === ')' || ch === ']') {
      if (stack.pop() !== ch) return -1;
      if (stack.length === 0) return i;
    }
  }
  return -1;
}

/**
 * First element of each `(value, label)` pair in an inline list/tuple.
 * Returns null for grouped choices or anything we cannot read safely.
 */
function inlineChoiceValues(expr: string): string[] | null {
  const inner = expr.trim();
  if (!(inner.startsWith('[') || inner.startsWith('(')) || matchingBracket(inner) !== inner.length - 1) {
    return null;
  }
  const body = inner.slice(1, -1).trim();
  if (!body) return null;
  const pairs = splitTopLevel(body);
  const values: string[] = [];
  for (const pair of pairs) {
    const item = pair.trim();
    if (!item) continue;
    if (!(item.startsWith('(') || item.startsWith('[')) || matchingBracket(item) !== item.length - 1) {
      return null;
    }
    const parts = splitTopLevel(item.slice(1, -1));
    if (parts.length < 2) return null;
    const value = parts[0].trim();
    const label = parts[1].trim();
    // Grouped choices: the "label" is itself a list of pairs.
    if (label.startsWith('[') || label.startsWith('(')) return null;
    if (!value) return null;
    values.push(value);
  }
  return values.length ? values : null;
}

/** Split on commas that are not inside quotes or brackets. */
function splitTopLevel(src: string): string[] {
  const out: string[] = [];
  let start = 0;
  let depth = 0;
  let quote: string | null = null;
  for (let i = 0; i < src.length; i++) {
    const ch = src[i];
    if (quote) {
      if (ch === '\\') {
        i++;
        continue;
      }
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      continue;
    }
    if (ch === '(' || ch === '[') depth++;
    else if (ch === ')' || ch === ']') depth = Math.max(0, depth - 1);
    else if (ch === ',' && depth === 0) {
      out.push(src.slice(start, i));
      start = i + 1;
    }
  }
  out.push(src.slice(start));
  return out;
}

/** Extract `choices=SOMETHING`, return `SOMETHING` or undefined. */
function extractChoiceName(args: string): string | undefined {
  const m = /\bchoices\s*=\s*([A-Za-z_][\w.]*)/.exec(args);
  return m?.[1];
}

function matchIntKwarg(args: string, name: string): number | undefined {
  const re = new RegExp(`\\b${name}\\s*=\\s*(\\d+)`);
  const m = re.exec(args);
  return m ? parseInt(m[1], 10) : undefined;
}

/** Find a ParsedModel by its short or dotted name in the index. */
function findModelByRef(index: WorkspaceIndex, ref: string): ParsedModel | undefined {
  if (!ref) return undefined;
  const dotted = ref.split('.');
  const short = dotted[dotted.length - 1];
  for (const app of index.apps) {
    for (const model of app.models) {
      if (model.name === short) return model;
    }
  }
  return undefined;
}

/**
 * Produce the factory class body for one model. Returns Python source (no
 * imports; the caller aggregates imports once at the top of the output).
 */
function factoryClassFor(
  model: ParsedModel,
  index: WorkspaceIndex,
  emitted: Set<string>,
  pending: ParsedModel[],
): string {
  const factoryName = `${model.name}Factory`;
  const lines: string[] = [];
  lines.push(`class ${factoryName}(factory.django.DjangoModelFactory):`);
  lines.push(`    class Meta:`);
  lines.push(`        model = ${model.name}`);
  lines.push('');

  const postM2m: string[] = [];

  for (const field of model.fields) {
    if (field.type === 'AutoField' || field.type === 'BigAutoField' || field.type === 'SmallAutoField') {
      continue;
    }

    if (field.isRelation) {
      const relatedModel = findModelByRef(index, field.relatedModel ?? '');
      if (field.relationKind === 'ForeignKey' || field.relationKind === 'OneToOneField') {
        const ref = relatedModel ? `${relatedModel.name}Factory` : "'self'";
        if (relatedModel && !emitted.has(relatedModel.name) && relatedModel !== model) {
          pending.push(relatedModel);
        }
        lines.push(`    ${field.name} = factory.SubFactory(${ref})`);
      } else if (field.relationKind === 'ManyToManyField') {
        // M2M — must be added post-generation.
        postM2m.push(field.name);
      }
      continue;
    }

    const provider = providerFor(field);
    if (!provider) continue;
    lines.push(`    ${field.name} = ${provider}`);
  }

  if (postM2m.length) {
    lines.push('');
    lines.push(`    @factory.post_generation`);
    lines.push(`    def _add_m2m(self, create, extracted, **kwargs):`);
    lines.push(`        if not create:`);
    lines.push(`            return`);
    for (const name of postM2m) {
      lines.push(
        `        if extracted and '${name}' in extracted:`,
      );
      lines.push(`            for item in extracted['${name}']:`);
      lines.push(`                self.${name}.add(item)`);
    }
  }

  return lines.join('\n');
}

/** Assemble the full output document. */
export function generateFactoryCode(
  model: ParsedModel,
  index: WorkspaceIndex,
): string {
  const emitted = new Set<string>();
  const pending: ParsedModel[] = [model];
  const factories: string[] = [];

  while (pending.length > 0) {
    const next = pending.shift()!;
    if (emitted.has(next.name)) continue;
    emitted.add(next.name);
    factories.push(factoryClassFor(next, index, emitted, pending));
  }

  const importedModelNames = [...emitted].join(', ');
  const header = [
    '"""',
    `Auto-generated by ORM Lens from ${model.appName}.${model.name}.`,
    '',
    'Review each factory before committing:',
    '  - swap Faker providers for choices/enums where appropriate',
    '  - narrow the Meta.model import path to the real app label',
    '  - decide whether M2M fields want post_generation or Trait',
    '',
    'Install: pip install factory-boy faker',
    '"""',
    '',
    'import datetime',
    'from datetime import timezone',
    '',
    'import factory',
    '',
    `# TODO: adjust the import path to your project layout.`,
    `from ${model.appName}.models import ${importedModelNames}`,
    '',
    '',
  ].join('\n');

  return header + factories.join('\n\n\n') + '\n';
}
