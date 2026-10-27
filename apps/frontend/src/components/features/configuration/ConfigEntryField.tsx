'use client';

import { useId } from 'react';

import { FIELD_INVALID, TEXT_FIELD } from '@/components/features/admin/adminStyles';
import type { ConfigEntry } from '@/lib/api/config';

import {
  canMarkSecret,
  currentInput,
  draftError,
  hasStoredValue,
  isLocked,
  settleDraft,
  type ConfigDraft,
} from './configDrafts';

/**
 * Marks a field that holds a credential other than the administrator's own, so
 * password-manager extensions (1Password, LastPass, Bitwarden, Dashlane)
 * neither fill it nor offer to save what is typed into it. Browsers' built-in
 * managers go by `autocomplete` instead: give the field `"new-password"` (or
 * `"off"`) and its form `autoComplete="off"`.
 */
export const NOT_A_LOGIN_FIELD = {
  'data-1p-ignore': true,
  'data-lpignore': 'true',
  'data-bwignore': true,
  'data-form-type': 'other',
} as const;

type BadgeTone = 'red' | 'amber' | 'blue' | 'gray';

const BADGE_TONES: Record<BadgeTone, string> = {
  red: 'bg-red-100 text-red-700',
  amber: 'bg-amber-100 text-amber-800',
  blue: 'bg-blue-100 text-blue-700',
  gray: 'bg-gray-100 text-gray-600',
};

/**
 * Whether a saved change waits for a restart in the process that answered. A
 * key a live group owns is declared restart-required, and is until the group
 * is active; `apply_mode` says which applies now. An older backend omits it.
 */
export function appliesAfterRestart(entry: ConfigEntry): boolean {
  return entry.apply_mode ? entry.apply_mode === 'restart' : entry.restart_required;
}

function configBadges(entry: ConfigEntry): Array<{ label: string; tone: BadgeTone }> {
  const badges: Array<{ label: string; tone: BadgeTone }> = [];
  if (entry.missing) badges.push({ label: 'Missing', tone: 'red' });
  if (entry.invalid) badges.push({ label: 'Invalid', tone: 'red' });
  if (entry.required) badges.push({ label: 'Required', tone: 'gray' });
  if (entry.secret) badges.push({ label: 'Secret', tone: 'gray' });
  if (entry.pending_restart) badges.push({ label: 'Pending restart', tone: 'amber' });
  else if (entry.apply_mode === 'managed') badges.push({ label: 'Applies live', tone: 'blue' });
  else if (appliesAfterRestart(entry)) badges.push({ label: 'Restart required', tone: 'gray' });
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

const INPUT_CLASS = TEXT_FIELD;
const INPUT_ERROR_CLASS = `${TEXT_FIELD} ${FIELD_INVALID}`;
const LINK_BUTTON_CLASS =
  'rounded-md px-2 py-1 text-[12px] font-medium text-gray-600 transition hover:bg-gray-100 hover:text-gray-900 disabled:opacity-40';

/**
 * One setting: its name and badges, what it is for, and an editor for its type.
 *
 * A secret's value never reaches this component's output. The API sends
 * `null` for it, and the component does not read `value` for a secret even if
 * a misbehaving backend sent one: the editor shows "Set" / "Not set", and a
 * replacement is typed into an empty field.
 *
 * Render it inside a `<form autoComplete="off">`, so that browsers do not
 * offer to save a secret typed here as a password for the console.
 *
 * In the admin Configuration tab, Reset and "Mark as secret" are staged like
 * any other edit (`resetPending`, `markSecretPending`) and written only when
 * the category is saved.
 */
export function ConfigEntryField({
  entry,
  label = entry.key,
  allowSecretClear = true,
  presentation = 'default',
  layout = 'stacked',
  draft,
  onChange,
  serverError,
  onReset,
  onMarkSecret,
  resetPending = false,
  markSecretPending = false,
  disabled = false,
}: {
  entry: ConfigEntry;
  label?: string;
  allowSecretClear?: boolean;
  /** Compact cards supply their own privacy and restart notes. */
  presentation?: 'default' | 'compact';
  /**
   * `split` puts the name and description beside the editor on wide screens,
   * for a settings list in a wide card; both stack on narrow ones.
   */
  layout?: 'stacked' | 'split';
  draft: ConfigDraft | undefined;
  onChange: (key: string, draft: ConfigDraft | undefined) => void;
  /** The reason the last save gave for this key. */
  serverError?: string;
  /** Offer Reset (remove the database row). Omitted in the setup wizard. */
  onReset?: (entry: ConfigEntry) => void;
  /** Offer "Mark as secret" where it applies. Omitted in the setup wizard. */
  onMarkSecret?: (entry: ConfigEntry) => void;
  /** Reset is staged: the save removes the stored value. */
  resetPending?: boolean;
  /** "Mark as secret" is staged: the save marks it. */
  markSecretPending?: boolean;
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
  // Also for a stored value the backend refused: that entry reports what
  // applies in its place, and the row is still there to remove.
  const canReset = Boolean(onReset) && hasStoredValue(entry) && !entry.immutable && !locked;
  const offerMarkSecret = !resetPending && Boolean(onMarkSecret) && canMarkSecret(entry);

  // An edit back to the starting value is no edit, so it leaves no draft.
  const setValue = (value: string | boolean) =>
    onChange(entry.key, settleDraft(entry, { kind: 'value', value }));

  let control: React.ReactNode;
  if (resetPending && !locked) {
    control = (
      <output id={controlId} className="text-[12px] text-amber-700" aria-describedby={describedBy}>
        {entry.custom ? 'Deleted when you save' : 'Reset when you save'}
      </output>
    );
  } else if (locked) {
    // Locked need not mean set: an immutable setting is also locked, empty,
    // once what it falls back to is in use.
    let shown = 'Not set';
    if (entry.is_set) shown = entry.secret ? 'Set' : String(entry.value ?? '');
    control = (
      <output id={controlId} className="text-[12px] text-gray-600" aria-describedby={describedBy}>
        {shown}{' '}
        <span className="text-gray-400">
          · {entry.is_set ? 'cannot be changed once set' : 'can no longer be set'}
        </span>
      </output>
    );
  } else if (entry.secret) {
    control = (
      <SecretEditor
        entry={entry}
        label={label}
        allowClear={allowSecretClear}
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
        autoComplete="off"
        {...NOT_A_LOGIN_FIELD}
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
        // Not a login either: SMTP_USERNAME is no username for the console.
        autoComplete="off"
        {...NOT_A_LOGIN_FIELD}
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

  const compact = presentation === 'compact';
  const split = layout === 'split';
  const badges = configBadges(entry).filter(
    (badge) =>
      !compact ||
      !['Secret', 'Restart required', 'Pending restart', 'Applies live'].includes(badge.label),
  );

  return (
    <div
      className={
        compact
          ? undefined
          : split
            ? 'py-4 lg:grid lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] lg:gap-8'
            : 'py-4'
      }
      data-config-key={entry.key}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <label
              htmlFor={controlId}
              className={`break-all text-[13px] font-medium text-gray-900 ${compact ? '' : 'font-mono'}`}
            >
              {label}
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
        {offerMarkSecret || canReset ? (
          <div className="flex items-center gap-1">
            {offerMarkSecret && onMarkSecret ? (
              <button
                type="button"
                onClick={() => onMarkSecret(entry)}
                disabled={disabled}
                aria-label={
                  markSecretPending ? `Keep ${entry.key} visible` : `Mark ${entry.key} as secret`
                }
                className={LINK_BUTTON_CLASS}
              >
                {markSecretPending ? 'Keep visible' : 'Mark as secret'}
              </button>
            ) : null}
            {canReset && onReset ? (
              <button
                type="button"
                onClick={() => onReset(entry)}
                disabled={disabled}
                aria-label={
                  resetPending
                    ? `Undo ${entry.custom ? 'delete' : 'reset'} of ${entry.key}`
                    : `${entry.custom ? 'Delete' : 'Reset'} ${entry.key}`
                }
                className={LINK_BUTTON_CLASS}
              >
                {resetPending ? 'Undo' : entry.custom ? 'Delete' : 'Reset'}
              </button>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className={split ? 'mt-2 min-w-0 lg:mt-0 lg:max-w-2xl' : undefined}>
        <div className={split ? undefined : 'mt-2'}>{control}</div>

        <div id={notesId} className="mt-1 space-y-0.5 text-[11px]">
          {markSecretPending && offerMarkSecret ? (
            <p className="text-amber-700">
              Becomes secret when you save. This cannot be undone from the console.
            </p>
          ) : null}
          {entry.type === 'list' && !locked && !resetPending ? (
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
          {entry.immutable && !entry.is_set && !locked ? (
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
          {entry.updated_at && !compact ? (
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
    </div>
  );
}

function SecretEditor({
  entry,
  label,
  allowClear,
  draft,
  controlId,
  describedBy,
  invalid,
  disabled,
  onChange,
}: {
  entry: ConfigEntry;
  label: string;
  allowClear: boolean;
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
          aria-label={`Keep ${label}`}
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
          aria-label={`Replace ${label}`}
          className={LINK_BUTTON_CLASS}
        >
          Replace
        </button>
        {allowClear ? (
          <button
            type="button"
            onClick={() => onChange(entry.key, { kind: 'clear' })}
            disabled={disabled}
            aria-label={`Clear ${label}`}
            className={`${LINK_BUTTON_CLASS} text-red-600 hover:bg-red-50 hover:text-red-700`}
          >
            Clear
          </button>
        ) : null}
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
    // Keep password managers from offering the administrator's own password,
    // or from offering to save this secret as one.
    ...NOT_A_LOGIN_FIELD,
    autoFocus: entry.is_set,
  } as const;

  return (
    <div className="flex flex-wrap items-start gap-2">
      <div className="min-w-0 flex-1">
        {entry.type === 'text' ? (
          <textarea
            {...shared}
            rows={4}
            autoComplete="off"
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
          aria-label={`Keep the current ${label}`}
          className={LINK_BUTTON_CLASS}
        >
          Cancel
        </button>
      ) : null}
    </div>
  );
}
