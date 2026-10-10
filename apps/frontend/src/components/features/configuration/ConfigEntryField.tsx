'use client';

import { useId } from 'react';

import type { ConfigEntry } from '@/lib/api/config';

import { currentInput, draftError, isLocked, settleDraft, type ConfigDraft } from './configDrafts';

type BadgeTone = 'red' | 'amber' | 'blue' | 'gray';

const BADGE_TONES: Record<BadgeTone, string> = {
  red: 'bg-red-100 text-red-700',
  amber: 'bg-amber-100 text-amber-800',
  blue: 'bg-blue-100 text-blue-700',
  gray: 'bg-gray-100 text-gray-600',
};

export function configBadges(entry: ConfigEntry): Array<{ label: string; tone: BadgeTone }> {
  const badges: Array<{ label: string; tone: BadgeTone }> = [];
  if (entry.missing) badges.push({ label: 'Missing', tone: 'red' });
  if (entry.invalid) badges.push({ label: 'Invalid', tone: 'red' });
  if (entry.required) badges.push({ label: 'Required', tone: 'gray' });
  if (entry.secret) badges.push({ label: 'Secret', tone: 'gray' });
  if (entry.pending_restart) badges.push({ label: 'Pending restart', tone: 'amber' });
  else if (entry.restart_required) badges.push({ label: 'Restart required', tone: 'gray' });
  // Only a value comes from somewhere: an empty variable in the environment is
  // reported as its source, but "From environment" beside "Missing" misleads.
  if (entry.source === 'environment' && entry.is_set) {
    badges.push({ label: 'From environment', tone: 'blue' });
  }
  if (entry.environment_ignored) badges.push({ label: 'Environment ignored', tone: 'amber' });
  if (entry.immutable) badges.push({ label: 'Immutable', tone: 'gray' });
  if (entry.custom) badges.push({ label: 'Custom', tone: 'gray' });
  return badges;
}

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const minutes = Math.floor((Date.now() - then) / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(iso).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

const INPUT_CLASS =
  'w-full rounded-md border border-gray-300 bg-white px-2.5 py-1.5 text-[13px] text-gray-900 placeholder:text-gray-400 focus:border-gray-500 focus:outline-none disabled:bg-gray-50 disabled:text-gray-500';
const INPUT_ERROR_CLASS =
  'w-full rounded-md border border-red-300 bg-white px-2.5 py-1.5 text-[13px] text-gray-900 placeholder:text-gray-400 focus:border-red-500 focus:outline-none';
const LINK_BUTTON_CLASS =
  'rounded-md px-2 py-1 text-[12px] font-medium text-gray-600 transition hover:bg-gray-100 hover:text-gray-900 disabled:opacity-40';

/**
 * One setting: its name and badges, what it is for, and an editor for its type.
 *
 * A secret's value never reaches this component's output. The API sends
 * `null` for it, and the component does not read `value` for a secret even if
 * a misbehaving backend sent one: the editor shows "Set" / "Not set", and a
 * replacement is typed into an empty field.
 */
export function ConfigEntryField({
  entry,
  draft,
  onChange,
  serverError,
  onReset,
  disabled = false,
}: {
  entry: ConfigEntry;
  draft: ConfigDraft | undefined;
  onChange: (key: string, draft: ConfigDraft | undefined) => void;
  /** The reason the last save gave for this key. */
  serverError?: string;
  /** Offer Reset (remove the database row). Omitted in the setup wizard. */
  onReset?: (entry: ConfigEntry) => void;
  disabled?: boolean;
}) {
  const baseId = useId();
  const controlId = `${baseId}-control`;
  const descriptionId = `${baseId}-description`;
  const notesId = `${baseId}-notes`;
  const errorId = `${baseId}-error`;

  const locked = isLocked(entry);
  const validation = draftError(entry, draft);
  const error = validation ?? serverError ?? null;
  const usedBy = entry.used_by ?? [];
  const describedBy =
    [entry.description ? descriptionId : null, notesId, error ? errorId : null]
      .filter(Boolean)
      .join(' ') || undefined;
  const canReset = Boolean(onReset) && entry.source === 'database' && !entry.immutable;

  // An edit back to the starting value is no edit, so it leaves no draft.
  const setValue = (value: string | boolean) =>
    onChange(entry.key, settleDraft(entry, { kind: 'value', value }));

  let control: React.ReactNode;
  if (locked) {
    control = (
      <output id={controlId} className="text-[12px] text-gray-600" aria-describedby={describedBy}>
        {entry.secret ? 'Set' : String(entry.value ?? '')}
        <span className="ml-1 text-gray-400">· cannot be changed once set</span>
      </output>
    );
  } else if (entry.secret) {
    control = (
      <SecretEditor
        entry={entry}
        draft={draft}
        controlId={controlId}
        describedBy={describedBy}
        invalid={Boolean(error)}
        disabled={disabled}
        onChange={onChange}
      />
    );
  } else if (entry.type === 'bool') {
    const on = currentInput(entry, draft) === true;
    control = (
      <div className="flex items-center gap-2">
        <button
          id={controlId}
          type="button"
          role="switch"
          aria-checked={on}
          aria-describedby={describedBy}
          disabled={disabled}
          onClick={() => setValue(!on)}
          className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-gray-400 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-40 ${
            on ? 'bg-gray-900' : 'bg-gray-200'
          }`}
        >
          <span
            className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
              on ? 'translate-x-5' : 'translate-x-0'
            }`}
          />
        </button>
        <span className="text-[12px] text-gray-500" aria-hidden>
          {on ? 'On' : 'Off'}
        </span>
      </div>
    );
  } else if (entry.type === 'text') {
    control = (
      <textarea
        id={controlId}
        rows={4}
        spellCheck={false}
        value={String(currentInput(entry, draft))}
        placeholder={entry.default !== null ? String(entry.default) : undefined}
        disabled={disabled}
        aria-describedby={describedBy}
        aria-invalid={error ? true : undefined}
        onChange={(event) => setValue(event.target.value)}
        className={`${error ? INPUT_ERROR_CLASS : INPUT_CLASS} font-mono`}
      />
    );
  } else {
    const numeric = entry.type === 'int' || entry.type === 'float';
    control = (
      <input
        id={controlId}
        type={numeric ? 'number' : 'text'}
        inputMode={
          entry.type === 'int' ? 'numeric' : entry.type === 'float' ? 'decimal' : undefined
        }
        step={entry.type === 'int' ? 1 : entry.type === 'float' ? 'any' : undefined}
        spellCheck={false}
        autoComplete="off"
        value={String(currentInput(entry, draft))}
        placeholder={entry.default !== null ? String(entry.default) : undefined}
        disabled={disabled}
        aria-describedby={describedBy}
        aria-invalid={error ? true : undefined}
        onChange={(event) => setValue(event.target.value)}
        className={`${error ? INPUT_ERROR_CLASS : INPUT_CLASS} ${numeric ? 'max-w-[12rem]' : ''}`}
      />
    );
  }

  const badges = configBadges(entry);

  return (
    <div className="py-4" data-config-key={entry.key}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <label
              htmlFor={controlId}
              className="break-all font-mono text-[13px] font-medium text-gray-900"
            >
              {entry.key}
            </label>
            {badges.map((badge) => (
              <span
                key={badge.label}
                className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${BADGE_TONES[badge.tone]}`}
              >
                {badge.label}
              </span>
            ))}
          </div>
          {entry.description ? (
            <p id={descriptionId} className="mt-0.5 text-[12px] text-gray-500">
              {entry.description}
            </p>
          ) : null}
        </div>
        {canReset && onReset ? (
          <button
            type="button"
            onClick={() => onReset(entry)}
            disabled={disabled}
            aria-label={`${entry.custom ? 'Delete' : 'Reset'} ${entry.key}`}
            className={LINK_BUTTON_CLASS}
          >
            {entry.custom ? 'Delete' : 'Reset'}
          </button>
        ) : null}
      </div>

      <div className="mt-2">{control}</div>

      <div id={notesId} className="mt-1 space-y-0.5 text-[11px]">
        {entry.type === 'list' && !locked ? (
          <p className="text-gray-400">A comma-separated list.</p>
        ) : null}
        {usedBy.length > 0 ? (
          <p className="text-gray-500">
            Used by <span className="font-mono">{usedBy.join(', ')}</span>
          </p>
        ) : null}
        {!entry.secret && entry.default !== null && entry.source !== 'default' ? (
          <p className="text-gray-400">
            Default: <span className="font-mono">{String(entry.default)}</span>
          </p>
        ) : null}
        {entry.immutable && !entry.is_set ? (
          <p className="text-gray-500">Cannot be changed once it is set.</p>
        ) : null}
        {entry.environment_ignored ? (
          <p className="text-amber-700">
            The environment also sets this variable; the database value wins. Remove it from .env.
          </p>
        ) : null}
        {entry.invalid ? (
          <p className="text-red-600">The stored value could not be applied: {entry.invalid}</p>
        ) : null}
        {entry.source === 'database' && entry.updated_at ? (
          <p className="text-gray-400">
            Updated {relativeTime(entry.updated_at)}
            {entry.updated_by ? ` by ${entry.updated_by}` : ''}
          </p>
        ) : null}
      </div>

      {error ? (
        <p id={errorId} className="mt-1 text-[12px] text-red-600" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function SecretEditor({
  entry,
  draft,
  controlId,
  describedBy,
  invalid,
  disabled,
  onChange,
}: {
  entry: ConfigEntry;
  draft: ConfigDraft | undefined;
  controlId: string;
  describedBy: string | undefined;
  invalid: boolean;
  disabled: boolean;
  onChange: (key: string, draft: ConfigDraft | undefined) => void;
}) {
  if (draft?.kind === 'clear') {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <output
          id={controlId}
          className="text-[12px] text-amber-700"
          aria-describedby={describedBy}
        >
          Cleared when you save
        </output>
        <button
          type="button"
          onClick={() => onChange(entry.key, undefined)}
          disabled={disabled}
          aria-label={`Keep ${entry.key}`}
          className={LINK_BUTTON_CLASS}
        >
          Undo
        </button>
      </div>
    );
  }

  // A secret with no value has nothing to protect, so its field is open; one
  // that is set needs an explicit Replace first.
  const editing = draft?.kind === 'replace' || !entry.is_set;
  if (!editing) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <output
          id={controlId}
          aria-describedby={describedBy}
          className="inline-flex min-w-[6rem] items-center rounded-md border border-gray-200 bg-gray-50 px-2.5 py-1.5 text-[13px] text-gray-500"
        >
          Set
        </output>
        <button
          type="button"
          onClick={() => onChange(entry.key, { kind: 'replace', value: '' })}
          disabled={disabled}
          aria-label={`Replace ${entry.key}`}
          className={LINK_BUTTON_CLASS}
        >
          Replace
        </button>
        <button
          type="button"
          onClick={() => onChange(entry.key, { kind: 'clear' })}
          disabled={disabled}
          aria-label={`Clear ${entry.key}`}
          className={`${LINK_BUTTON_CLASS} text-red-600 hover:bg-red-50 hover:text-red-700`}
        >
          Clear
        </button>
      </div>
    );
  }

  const value = draft?.kind === 'replace' ? draft.value : '';
  const placeholder = entry.is_set ? 'Enter a new value' : 'Not set';
  const update = (next: string) => onChange(entry.key, { kind: 'replace', value: next });
  const shared = {
    id: controlId,
    value,
    placeholder,
    disabled,
    spellCheck: false,
    'aria-describedby': describedBy,
    'aria-invalid': invalid ? true : undefined,
    // Keep password managers from offering the administrator's own password.
    'data-1p-ignore': true,
    'data-lpignore': 'true',
    autoFocus: entry.is_set,
  } as const;

  return (
    <div className="flex flex-wrap items-start gap-2">
      <div className="min-w-0 flex-1">
        {entry.type === 'text' ? (
          <textarea
            {...shared}
            rows={4}
            onChange={(event) => update(event.target.value)}
            className={`${invalid ? INPUT_ERROR_CLASS : INPUT_CLASS} font-mono`}
          />
        ) : (
          <input
            {...shared}
            type="password"
            autoComplete="new-password"
            onChange={(event) => update(event.target.value)}
            className={invalid ? INPUT_ERROR_CLASS : INPUT_CLASS}
          />
        )}
      </div>
      {entry.is_set ? (
        <button
          type="button"
          onClick={() => onChange(entry.key, undefined)}
          disabled={disabled}
          aria-label={`Keep the current ${entry.key}`}
          className={LINK_BUTTON_CLASS}
        >
          Cancel
        </button>
      ) : null}
    </div>
  );
}
