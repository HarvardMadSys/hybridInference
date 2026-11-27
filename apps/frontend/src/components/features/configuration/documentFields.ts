// An application document -- the model catalog, the routing policy, the site
// configuration: a JSON object stored as one setting -- as flat fields, one
// per value, each a simple string labelled with where the value is
// (`models[0].route[0].base_url`). Admin edits a document this way, and the
// document is still saved whole, as JSON.
//
// A field keeps the type of the value it came from: a number stays a number,
// `true` a boolean, and a value never set (`null`) takes what is typed as
// text. An array of strings is one field, its items separated by commas; any
// other array is a field per item. An empty list or object shows, but holds
// nothing to type into: what it should hold is not known. Adding or removing
// a key or an item changes the document's shape, which the JSON view does.

export type JsonValue = null | boolean | number | string | JsonValue[] | JsonObject;
export interface JsonObject {
  [key: string]: JsonValue;
}
export type PathSegment = string | number;

/** What a field holds, from the value it came from. `empty` is `[]` or `{}`, shown only. */
export type FieldKind = 'string' | 'number' | 'boolean' | 'null' | 'list' | 'empty';

export interface DocumentField {
  /** Stable while the document keeps its shape: the path, as JSON. */
  id: string;
  path: PathSegment[];
  /** The path as people read it: `models[0].route[0].base_url`. */
  label: string;
  /** The path as a JSON Pointer, the way the gateway names a value it refuses. */
  pointer: string;
  kind: FieldKind;
  /** The value as the field shows it. */
  text: string;
}

export function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * The document `text` holds: blank is the empty document, as the gateway
 * reads it. `null` when the text is not a JSON object.
 */
export function parseDocument(text: string): JsonObject | null {
  if (!text.trim()) return {};
  try {
    const value: unknown = JSON.parse(text);
    return isJsonObject(value) ? value : null;
  } catch {
    return null;
  }
}

/** The document as JSON, indented, which is how a save from its fields stores it. */
export function documentText(document: JsonObject): string {
  return JSON.stringify(document, null, 2);
}

// A JSON number where it starts, read in place; `y` keeps the scan linear.
const NUMBER_AT = /-?\d+(\.\d+)?([eE][+-]?\d+)?/y;

/**
 * Whether reading `text` and writing it back keeps every value the gateway
 * reads: not when it holds an integer beyond 2^53, which JavaScript rounds.
 * Other changes a round trip makes leave the document the gateway reads as it
 * was: `1.0` written `1` is the same number, and a repeated key keeps its
 * last value, as the gateway's own reader does.
 */
export function keepsItsValues(text: string): boolean {
  let i = 0;
  while (i < text.length) {
    const char = text[i];
    if (char === '"') {
      // Past the string, its escapes included: a number in it is text.
      i += 1;
      while (i < text.length && text[i] !== '"') i += text[i] === '\\' ? 2 : 1;
      i += 1;
    } else if (char === '-' || (char >= '0' && char <= '9')) {
      NUMBER_AT.lastIndex = i;
      const match = NUMBER_AT.exec(text);
      if (!match) {
        i += 1;
        continue;
      }
      const integer = match[1] === undefined && match[2] === undefined;
      if (integer && !Number.isSafeInteger(Number(match[0]))) return false;
      i += match[0].length;
    } else {
      i += 1;
    }
  }
  return true;
}

const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_-]*$/;

/** `models[0].route[0].base_url`; a key that is no plain name is quoted: `labels["a.b"]`. */
export function pathLabel(path: readonly PathSegment[]): string {
  return path
    .map((segment, index) => {
      if (typeof segment === 'number') return `[${segment}]`;
      if (!IDENTIFIER.test(segment)) return `[${JSON.stringify(segment)}]`;
      return index === 0 ? segment : `.${segment}`;
    })
    .join('');
}

/** RFC 6901: `/models/0/route/0/base_url`. */
export function pathPointer(path: readonly PathSegment[]): string {
  return path
    .map((segment) => `/${String(segment).replace(/~/g, '~0').replace(/\//g, '~1')}`)
    .join('');
}

/**
 * Whether an array reads as one comma-separated field and back unchanged:
 * every item a string that holds no comma or line break and is not padded or
 * empty.
 */
function isPlainList(value: readonly JsonValue[]): value is string[] {
  return value.every(
    (item) =>
      typeof item === 'string' && item !== '' && item === item.trim() && !/[,\r\n]/.test(item),
  );
}

function kindOf(value: null | boolean | number | string): FieldKind {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return 'boolean';
  if (typeof value === 'number') return 'number';
  return 'string';
}

/** The document's values as fields, in the document's own order. */
export function documentFields(document: JsonObject): DocumentField[] {
  const fields: DocumentField[] = [];
  const add = (path: PathSegment[], kind: FieldKind, text: string) =>
    fields.push({
      id: JSON.stringify(path),
      path,
      label: pathLabel(path),
      pointer: pathPointer(path),
      kind,
      text,
    });
  const visit = (value: JsonValue, path: PathSegment[]) => {
    if (Array.isArray(value)) {
      // Empty, it could hold strings or objects alike: shown, and filled in JSON.
      if (value.length === 0) add(path, 'empty', '[]');
      else if (isPlainList(value)) add(path, 'list', value.join(', '));
      else value.forEach((item, index) => visit(item, [...path, index]));
    } else if (isJsonObject(value)) {
      const entries = Object.entries(value);
      if (entries.length === 0) add(path, 'empty', '{}');
      for (const [key, item] of entries) visit(item, [...path, key]);
    } else {
      add(path, kindOf(value), value === null ? '' : String(value));
    }
  };
  for (const [key, value] of Object.entries(document)) visit(value, [key]);
  return fields;
}

// A JSON number, as the gateway reads one: no hex, no `Infinity`, no blank.
const JSON_NUMBER = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$/;

/** The value `text` stands for in a field of `kind`, or why it cannot be one. */
export function fieldValue(
  kind: FieldKind,
  text: string,
): { value: JsonValue; error?: undefined } | { error: string } {
  const trimmed = text.trim();
  switch (kind) {
    case 'string':
      return { value: text };
    case 'list':
      return {
        value: text
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean),
      };
    case 'number':
      return JSON_NUMBER.test(trimmed) && Number.isFinite(Number(trimmed))
        ? { value: Number(trimmed) }
        : { error: 'Enter a number.' };
    case 'boolean':
      if (trimmed.toLowerCase() === 'true') return { value: true };
      if (trimmed.toLowerCase() === 'false') return { value: false };
      return { error: 'Enter true or false.' };
    case 'null':
      // Nothing was ever set, so nothing says what this holds: what is typed
      // is text -- `2024` stays "2024" -- unless it is left empty or `null`.
      if (trimmed === '' || trimmed === 'null') return { value: null };
      return { value: text };
    case 'empty':
      return { error: 'Add to it in JSON.' };
  }
}

/** Put `value` at `path`, which the document already has. A key is set as the document's own. */
function setAt(document: JsonObject, path: readonly PathSegment[], value: JsonValue): void {
  let node: JsonValue = document;
  for (const segment of path.slice(0, -1)) {
    node = (node as Record<string | number, JsonValue>)[segment];
  }
  const last = path[path.length - 1];
  if (Array.isArray(node)) {
    node[last as number] = value;
  } else {
    // Defined rather than assigned: a key named `__proto__` stays a key.
    Object.defineProperty(node, last, {
      value,
      writable: true,
      enumerable: true,
      configurable: true,
    });
  }
}

/**
 * `document` with what was typed into its fields, by field id. A text that
 * is not a value of its field's kind leaves the value as it was, and says why.
 */
export function withFieldTexts(
  document: JsonObject,
  fields: readonly DocumentField[],
  texts: Readonly<Record<string, string>>,
): { document: JsonObject; errors: Record<string, string> } {
  const next = JSON.parse(JSON.stringify(document)) as JsonObject;
  const errors: Record<string, string> = {};
  for (const field of fields) {
    const text = texts[field.id];
    if (text === undefined) continue;
    const result = fieldValue(field.kind, text);
    if (result.error !== undefined) errors[field.id] = result.error;
    else setAt(next, field.path, result.value);
  }
  return { document: next, errors };
}

/** Whether two JSON values are the same, whatever the order of their keys. */
export function sameJson(a: JsonValue, b: JsonValue): boolean {
  if (a === b) return true;
  if (Array.isArray(a)) {
    return Array.isArray(b) && a.length === b.length && a.every((item, i) => sameJson(item, b[i]));
  }
  if (isJsonObject(a)) {
    if (!isJsonObject(b)) return false;
    const keys = Object.keys(a);
    return (
      keys.length === Object.keys(b).length &&
      keys.every((key) => Object.prototype.hasOwnProperty.call(b, key) && sameJson(a[key], b[key]))
    );
  }
  return false;
}
