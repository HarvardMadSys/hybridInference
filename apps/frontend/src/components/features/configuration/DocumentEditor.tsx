'use client';

import { Fragment, useEffect, useId, useMemo, useState, type ReactNode } from 'react';

import { CONTROL, FIELD_INVALID, TEXT_FIELD } from '@/components/features/admin/adminStyles';
import type { ConfigEntry } from '@/lib/api/config';

import { currentInput, settleDraft, type ConfigDraft } from './configDrafts';
import {
  documentFields,
  documentText,
  keepsItsValues,
  parseDocument,
  pathLabel,
  sameJson,
  withFieldTexts,
  type DocumentField,
  type FieldKind,
} from './documentFields';
import { NOT_A_LOGIN_FIELD } from './notALoginField';

type View = 'fields' | 'json';

/** With more fields than this, a filter narrows the list. */
const FILTER_FROM = 12;

/** What to type, beside a field that is not plain text. */
const KIND_HINTS: Partial<Record<FieldKind, string>> = {
  number: 'number',
  boolean: 'true or false',
  list: 'comma-separated',
  // Never set: a number or true/false is read as one, anything else as text.
  null: '"quote" to keep as text',
};

/** The draft without a field's complaint: what the valid fields made of the document. */
function withoutError(entry: ConfigEntry, draft: ConfigDraft): ConfigDraft | undefined {
  return draft.kind === 'value' ? settleDraft(entry, { kind: 'value', value: draft.value }) : draft;
}

/**
 * An application document's editor: one field per value (see
 * `documentFields`), each a simple string, or the whole document as JSON,
 * which is where an entry is added or removed. Either way the draft is the
 * document's JSON text, so saving, discarding and conflicts work as they do
 * for any setting.
 *
 * It opens on the fields when the document has a value to type into, and on
 * the JSON otherwise. Text that is not a JSON object has no fields to show,
 * and nor does a document with an integer beyond 2^53: writing it back from
 * fields would round that integer, though nobody edited it.
 */
export function DocumentEditor({
  entry,
  draft,
  onChange,
  json,
  labelId,
  describedBy,
  serverError,
  disabled = false,
}: {
  entry: ConfigEntry;
  draft: ConfigDraft | undefined;
  onChange: (key: string, draft: ConfigDraft | undefined) => void;
  /** The document's JSON view: the setting's own text editor. */
  json: ReactNode;
  /** The setting's name, which labels the fields as a group. */
  labelId: string;
  describedBy?: string;
  /** Why the last save was refused; the JSON Pointers in it mark the fields it names. */
  serverError?: string;
  disabled?: boolean;
}) {
  const text = String(currentInput(entry, draft));
  const parsed = useMemo(() => parseDocument(text), [text]);
  const exact = useMemo(() => keepsItsValues(text), [text]);
  const fieldsAvailable = parsed !== null && exact;
  const [view, setView] = useState<View>(() =>
    fieldsAvailable && documentFields(parsed).some((field) => field.kind !== 'empty')
      ? 'fields'
      : 'json',
  );
  const shown: View = view === 'fields' && fieldsAvailable ? 'fields' : 'json';

  const choose = (next: View) => {
    // A field that is not a value of its type stays behind in the fields:
    // the JSON shows, and saves, the document the other fields made.
    if (next === 'json' && draft?.kind === 'value' && draft.error) {
      onChange(entry.key, withoutError(entry, draft));
    }
    setView(next);
  };

  let note = 'The whole document as JSON.';
  if (shown === 'fields') note = 'One field per value. Add or remove entries in JSON.';
  else if (parsed === null) note = 'This text is not a JSON object, so it has no fields to show.';
  else if (!exact) {
    note = 'This document holds a number too large to edit as fields without rounding it.';
  }

  return (
    <div className="max-w-4xl">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11px] text-gray-500">{note}</p>
        <div
          role="group"
          aria-label={`Edit ${entry.key} as`}
          className="inline-flex shrink-0 rounded-md border border-gray-200 bg-gray-50 p-0.5 text-[12px]"
        >
          {(['fields', 'json'] as const).map((option) => (
            <button
              key={option}
              type="button"
              aria-pressed={shown === option}
              disabled={option === 'fields' && !fieldsAvailable}
              title={
                option === 'fields' && parsed === null
                  ? 'Correct the JSON to edit it as fields'
                  : undefined
              }
              onClick={() => choose(option)}
              className={`rounded px-2.5 py-0.5 font-medium transition disabled:cursor-not-allowed disabled:opacity-40 ${
                shown === option
                  ? 'bg-white text-gray-900 shadow-sm'
                  : 'text-gray-500 hover:text-gray-900'
              }`}
            >
              {option === 'fields' ? 'Fields' : 'JSON'}
            </button>
          ))}
        </div>
      </div>
      {shown === 'fields' ? (
        <DocumentFields
          entry={entry}
          draft={draft}
          onChange={onChange}
          labelId={labelId}
          describedBy={describedBy}
          serverError={serverError}
          disabled={disabled}
        />
      ) : (
        json
      )}
    </div>
  );
}

/** What was typed into the fields, and the document it was typed onto. */
interface Typing {
  /** The draft this was typed onto, or that the last edit here made. */
  draft: ConfigDraft | undefined;
  /** The saved document's text then, which a reload or another save can replace. */
  saved: string;
  /** The document's text when typing began: the fields are its fields. */
  base: string;
  /** What each field holds now, by field id; a field not typed into is absent. */
  texts: Record<string, string>;
}

function problemSummary(invalid: readonly DocumentField[], errors: Record<string, string>): string {
  if (invalid.length === 1) return `${invalid[0].label}: ${errors[invalid[0].id]}`;
  return `${invalid.length} fields are not valid: ${invalid.map((field) => field.label).join(', ')}.`;
}

/** Whether a refusal names this field's value, or a value within it. */
function namedIn(reason: string | undefined, field: DocumentField): boolean {
  return Boolean(
    reason && (reason.includes(`${field.pointer} `) || reason.includes(`${field.pointer}/`)),
  );
}

/**
 * The document's fields. What is typed stays as typed -- a number half
 * written, a list being extended -- and the draft is the document with every
 * field that converts to its type. One that does not is said beside the field,
 * and in the draft, which holds the save back until it is corrected.
 */
function DocumentFields({
  entry,
  draft,
  onChange,
  labelId,
  describedBy,
  serverError,
  disabled,
}: {
  entry: ConfigEntry;
  draft: ConfigDraft | undefined;
  onChange: (key: string, draft: ConfigDraft | undefined) => void;
  labelId: string;
  describedBy?: string;
  serverError?: string;
  disabled: boolean;
}) {
  const baseId = useId();
  const savedText = String(currentInput(entry, undefined));
  const [typing, setTyping] = useState<Typing>(() => ({
    draft,
    saved: savedText,
    base: String(currentInput(entry, draft)),
    texts: {},
  }));
  const [filter, setFilter] = useState('');

  let current = typing;
  // Changed somewhere else -- Discard, the JSON view, a save, a reload -- or,
  // with nothing unsaved here, a newer saved document after a conflict,
  // another key's save or a restore: the fields start again from the
  // document as it is now, so an edit never builds on one that is gone.
  if (typing.draft !== draft || (draft === undefined && typing.saved !== savedText)) {
    current = { draft, saved: savedText, base: String(currentInput(entry, draft)), texts: {} };
    setTyping(current);
  }

  // Back after a filter elsewhere hid the fields: what did not convert was
  // left behind with them, so the draft no longer holds the save back for it.
  useEffect(() => {
    if (draft?.kind === 'value' && draft.error) onChange(entry.key, withoutError(entry, draft));
    // Only on mount: later, the complaint belongs to what is typed here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const saved = useMemo(() => parseDocument(savedText), [savedText]);
  const base = useMemo(() => parseDocument(current.base) ?? {}, [current.base]);
  const fields = useMemo(() => documentFields(base), [base]);
  const errors = useMemo(
    () => withFieldTexts(base, fields, current.texts).errors,
    [base, fields, current.texts],
  );

  const change = (field: DocumentField, value: string) => {
    const texts = { ...current.texts, [field.id]: value };
    const applied = withFieldTexts(base, fields, texts);
    const invalid = fields.filter((candidate) => applied.errors[candidate.id] !== undefined);
    let next: ConfigDraft | undefined;
    if (invalid.length === 0 && saved !== null && sameJson(applied.document, saved)) {
      next = undefined;
    } else {
      next = {
        kind: 'value',
        // A document whose values did not change keeps its own text.
        value: sameJson(applied.document, base) ? current.base : documentText(applied.document),
        ...(invalid.length > 0 ? { error: problemSummary(invalid, applied.errors) } : {}),
      };
    }
    setTyping({ draft: next, saved: savedText, base: current.base, texts });
    onChange(entry.key, next);
  };

  if (fields.length === 0) {
    return (
      <p className="rounded-md bg-gray-50 px-3 py-2 text-[12px] text-gray-500">
        No values yet. Add them in JSON.
      </p>
    );
  }

  const needle = filter.trim().toLowerCase();
  const matches = (field: DocumentField) =>
    !needle ||
    field.label.toLowerCase().includes(needle) ||
    (current.texts[field.id] ?? field.text).toLowerCase().includes(needle);
  const shown = fields.filter(matches).length;

  return (
    <div className="space-y-2">
      {fields.length > FILTER_FROM ? (
        <input
          type="search"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="Filter fields by name or value"
          aria-label={`Filter ${entry.key} fields`}
          className={`${CONTROL} w-full`}
        />
      ) : null}
      <div
        role="group"
        aria-labelledby={labelId}
        aria-describedby={describedBy}
        className="divide-y divide-gray-100 rounded-md border border-gray-200"
      >
        {fields.map((field, index) =>
          matches(field) ? (
            <FieldRow
              key={field.id}
              id={`${baseId}-${index}`}
              field={field}
              text={current.texts[field.id] ?? field.text}
              error={errors[field.id]}
              flagged={namedIn(serverError, field)}
              disabled={disabled}
              onChange={change}
            />
          ) : null,
        )}
        {shown === 0 ? (
          <p className="px-3 py-2 text-[12px] text-gray-500">No field matches the filter.</p>
        ) : null}
      </div>
    </div>
  );
}

function FieldRow({
  id,
  field,
  text,
  error,
  flagged,
  disabled,
  onChange,
}: {
  id: string;
  field: DocumentField;
  text: string;
  error?: string;
  /** Named by the reason the last save was refused. */
  flagged: boolean;
  disabled: boolean;
  onChange: (field: DocumentField, value: string) => void;
}) {
  const hint = KIND_HINTS[field.kind];
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const invalid = Boolean(error) || flagged;
  const control = {
    id,
    value: text,
    disabled,
    spellCheck: false,
    autoComplete: 'off',
    ...NOT_A_LOGIN_FIELD,
    'aria-invalid': invalid || undefined,
    'aria-describedby':
      [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(' ') || undefined,
    className: invalid ? `${TEXT_FIELD} ${FIELD_INVALID}` : TEXT_FIELD,
  };
  const row =
    'grid grid-cols-1 gap-1 px-3 py-2 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] sm:gap-3';
  if (field.kind === 'empty') {
    // Whether it takes strings or objects is not known: nothing to type into.
    return (
      <div className={row} data-document-field={field.label}>
        <span className="min-w-0 font-mono text-[12px] text-gray-800 [overflow-wrap:anywhere] sm:pt-1.5">
          <PathLabel field={field} />
        </span>
        <p className="min-w-0 text-[12px] text-gray-400 sm:pt-1.5">
          {field.text === '[]'
            ? 'An empty list. Add its items in JSON.'
            : 'Empty. Add its keys in JSON.'}
        </p>
      </div>
    );
  }
  return (
    <div className={row} data-document-field={field.label}>
      <div className="min-w-0 sm:pt-1.5">
        <label
          htmlFor={id}
          className="font-mono text-[12px] text-gray-800 [overflow-wrap:anywhere]"
        >
          <PathLabel field={field} />
        </label>
        {hint ? (
          <span id={hintId} className="ml-1.5 whitespace-nowrap text-[11px] text-gray-400">
            {hint}
          </span>
        ) : null}
      </div>
      <div className="min-w-0">
        {/* A string with line breaks keeps them: an input would drop them. */}
        {field.kind === 'string' && field.text.includes('\n') ? (
          <textarea
            {...control}
            rows={Math.min(8, field.text.split('\n').length)}
            onChange={(event) => onChange(field, event.target.value)}
          />
        ) : (
          <input
            {...control}
            type="text"
            inputMode={field.kind === 'number' ? 'decimal' : undefined}
            placeholder={field.kind === 'null' ? 'null' : undefined}
            onChange={(event) => onChange(field, event.target.value)}
          />
        )}
        {error ? (
          <p id={errorId} className="mt-0.5 text-[11px] text-red-600">
            {error}
          </p>
        ) : null}
      </div>
    </div>
  );
}

/** A field's path, which a narrow column breaks before an index or after a dot. */
function PathLabel({ field }: { field: DocumentField }) {
  return field.path.map((segment, index) => {
    const part = pathLabel([segment]);
    const text =
      typeof segment === 'string' && index > 0 && !part.startsWith('[') ? `.${part}` : part;
    return (
      <Fragment key={index}>
        {index > 0 ? <wbr /> : null}
        {text}
      </Fragment>
    );
  });
}
