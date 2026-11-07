// Unsaved edits to configuration entries, and the PATCH body they become.
//
// Shared by the admin Settings tab and the first-run setup wizard, so the
// rule for what counts as a change, and the JSON type each setting is sent as,
// exist once.

import type { ConfigCategory, ConfigEntry, ConfigScalar, ConfigValueType } from '@/lib/api/config';

/**
 * One entry's unsaved edit. No draft means "unchanged".
 *
 * Secrets never have a current value in the console, so their edits are
 * actions rather than values: `replace` with what the administrator typed, or
 * `clear`, which stores the empty string.
 */
export type ConfigDraft =
  | { kind: 'value'; value: string | boolean }
  | { kind: 'replace'; value: string }
  | { kind: 'clear' };

export type ConfigDrafts = Record<string, ConfigDraft>;

const INTEGER_PATTERN = /^[+-]?\d+$/;

/**
 * A setting that accepts no edit at all, as the backend reports it. A backend
 * that predates `locked` locks an immutable setting once it has a value.
 */
export function isLocked(entry: ConfigEntry): boolean {
  return entry.locked ?? (entry.immutable && entry.is_set);
}

/** What a non-secret editor shows before any edit. */
function initialInput(entry: ConfigEntry): string | boolean {
  if (entry.secret) return '';
  if (entry.type === 'bool') return entry.value === true;
  return entry.value === null || entry.value === undefined ? '' : String(entry.value);
}

/** What the editor shows now: the draft, or the entry's own value. */
export function currentInput(entry: ConfigEntry, draft: ConfigDraft | undefined): string | boolean {
  if (draft && draft.kind !== 'clear') return draft.value;
  return initialInput(entry);
}

function isNumeric(type: ConfigValueType): boolean {
  return type === 'int' || type === 'float';
}

function sameValue(type: ConfigValueType, a: string | boolean, b: string | boolean): boolean {
  if (typeof a === 'boolean' || typeof b === 'boolean') return a === b;
  if (isNumeric(type) && a.trim() !== '' && b.trim() !== '') {
    const left = Number(a);
    const right = Number(b);
    if (Number.isFinite(left) && Number.isFinite(right)) return left === right;
  }
  return a === b;
}

/** Whether saving would send something for this entry. */
export function isDirty(entry: ConfigEntry, draft: ConfigDraft | undefined): boolean {
  if (!draft || isLocked(entry)) return false;
  if (draft.kind === 'clear') return true;
  // An empty replacement is an editor left open, not an edit: clearing a
  // secret is its own, explicit action.
  if (draft.kind === 'replace') return draft.value !== '';
  return !sameValue(entry.type, draft.value, initialInput(entry));
}

/**
 * The draft to keep after an edit: `undefined` once a value edit is back where
 * it started, so "unsaved changes" counts only real changes. Secret actions
 * are kept as they are — an open replacement field stays open.
 */
export function settleDraft(
  entry: ConfigEntry,
  draft: ConfigDraft | undefined,
): ConfigDraft | undefined {
  if (draft?.kind === 'value' && !isDirty(entry, draft)) return undefined;
  return draft;
}

/** A message when a changed value cannot be sent as its type, else `null`. */
export function draftError(entry: ConfigEntry, draft: ConfigDraft | undefined): string | null {
  if (!draft || draft.kind === 'clear' || !isDirty(entry, draft)) return null;
  const raw = draft.value;
  if (typeof raw === 'boolean') return null;
  if (entry.type === 'int' && !INTEGER_PATTERN.test(raw.trim())) {
    return 'Enter a whole number.';
  }
  if (entry.type === 'float' && (raw.trim() === '' || !Number.isFinite(Number(raw)))) {
    return 'Enter a number.';
  }
  return null;
}

/**
 * The JSON value `PATCH /admin/config` takes: a boolean for `bool`, a number
 * for `int`/`float`, a string for everything else (a `list` as its
 * comma-separated string). Clearing a secret sends `""`.
 */
function patchValue(entry: ConfigEntry, draft: ConfigDraft): ConfigScalar {
  if (draft.kind === 'clear') return '';
  const raw = draft.value;
  if (typeof raw === 'boolean') return raw;
  if (isNumeric(entry.type)) return Number(raw.trim());
  return raw;
}

export interface BuiltPatch {
  values: Record<string, ConfigScalar>;
  /** Validation messages by key; nothing should be sent while any exist. */
  errors: Record<string, string>;
}

/** The `values` of a PATCH for the changed entries among `entries`. */
export function buildPatch(entries: readonly ConfigEntry[], drafts: ConfigDrafts): BuiltPatch {
  const values: Record<string, ConfigScalar> = {};
  const errors: Record<string, string> = {};
  for (const entry of entries) {
    const draft = drafts[entry.key];
    if (!draft || !isDirty(entry, draft)) continue;
    const error = draftError(entry, draft);
    if (error) {
      errors[entry.key] = error;
      continue;
    }
    values[entry.key] = patchValue(entry, draft);
  }
  return { values, errors };
}

export function countDirty(entries: readonly ConfigEntry[], drafts: ConfigDrafts): number {
  return entries.filter((entry) => isDirty(entry, drafts[entry.key])).length;
}

/** Drafts without the given keys: what remains after those keys were saved. */
export function withoutKeys<T>(
  record: Record<string, T>,
  keys: readonly string[],
): Record<string, T> {
  if (keys.length === 0) return record;
  const next = { ...record };
  for (const key of keys) delete next[key];
  return next;
}

export interface ConfigGroup {
  id: string;
  label: string;
  description: string;
  entries: ConfigEntry[];
}

function fallbackLabel(id: string): string {
  return id ? id.charAt(0).toUpperCase() + id.slice(1) : 'Other';
}

/**
 * Entries under their categories, in the order the response lists the
 * categories. An entry whose category the response does not describe still
 * shows, in a group named after its id, and empty categories are dropped.
 */
export function groupEntries(
  categories: readonly ConfigCategory[],
  entries: readonly ConfigEntry[],
): ConfigGroup[] {
  const groups: ConfigGroup[] = categories.map((category) => ({
    id: category.id,
    label: category.label || fallbackLabel(category.id),
    description: category.description,
    entries: [],
  }));
  const byId = new Map(groups.map((group) => [group.id, group]));
  for (const entry of entries) {
    let group = byId.get(entry.category);
    if (!group) {
      group = {
        id: entry.category,
        label: fallbackLabel(entry.category),
        description: '',
        entries: [],
      };
      byId.set(group.id, group);
      groups.push(group);
    }
    group.entries.push(entry);
  }
  return groups.filter((group) => group.entries.length > 0);
}

/** Case-insensitive match on the name, description, category or models. */
export function matchesQuery(entry: ConfigEntry, query: string, categoryLabel = ''): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [entry.key, entry.description, categoryLabel, ...(entry.used_by ?? [])].some((text) =>
    text?.toLowerCase().includes(needle),
  );
}

/**
 * Split a `"KEY: reason"` error detail, when `KEY` is one of `keys`, so the
 * reason can be shown on that setting.
 */
export function errorForKey(
  message: string,
  keys: Iterable<string>,
): { key: string; reason: string } | null {
  const match = /^([A-Z][A-Z0-9_]*):\s*([\s\S]+)$/.exec(message.trim());
  if (!match) return null;
  const known = new Set(keys);
  return known.has(match[1]) ? { key: match[1], reason: match[2] } : null;
}

/** A custom variable name the backend accepts. */
export const CUSTOM_KEY_PATTERN = /^[A-Z][A-Z0-9_]{1,127}$/;

/**
 * Words that make a name a credential's: the backend's `SECRET_NAME_WORDS`
 * (serving/config/app_config_registry.py), which it matches anywhere in the
 * upper-cased name. Keep the two in step.
 */
const SECRET_NAME_WORDS = [
  'KEY',
  'TOKEN',
  'SECRET',
  'PASSWORD',
  'PASS',
  'PWD',
  'WEBHOOK',
  'PRIVATE',
  'AUTH',
  'CREDENTIAL',
  'BEARER',
  'CERT',
  'DSN',
  'SA_JSON',
] as const;

/** The backend's own rule (`is_secret_name`) for which variable names are secret. */
export function looksSecret(name: string): boolean {
  const upper = name.toUpperCase();
  return SECRET_NAME_WORDS.some((word) => upper.includes(word));
}

/**
 * Whether a database row holds a value for the entry, so Reset has something
 * to remove: one in force (`source` is `database`), or one refused (`invalid`
 * says why, and the environment or default applies in its place). A reset row
 * stores no value, and `DELETE` answers 404 for it.
 */
export function hasStoredValue(entry: ConfigEntry): boolean {
  return entry.updated_at != null && (entry.source === 'database' || entry.invalid != null);
}

/**
 * Whether "Mark as secret" applies: a variable from a configuration file, a
 * numbered provider key or a custom one (a built-in setting's flag is fixed)
 * that is not secret yet, is not locked, and has a row in the database — a
 * value or a reset — for the flag to stay on. A backend that does not report
 * `origin` predates marking, so it is offered nothing.
 */
export function canMarkSecret(entry: ConfigEntry): boolean {
  if (entry.origin === undefined || entry.origin === 'static') return false;
  return !entry.secret && !isLocked(entry) && entry.updated_at != null;
}
