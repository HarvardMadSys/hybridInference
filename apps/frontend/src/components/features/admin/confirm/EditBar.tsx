'use client';

import { BUTTON_GHOST, BUTTON_PRIMARY, BUTTON_SECONDARY } from '../adminStyles';

/**
 * Cancel and Save changes for a section or row being edited, with how many
 * changes are unsaved. Save is disabled for a no-op, while the save is
 * `blocked` and while a write runs.
 *
 * `inPlace` is for settings whose fields are always editable: the bar appears
 * with the first change (or an error to show), and its Cancel reads Discard.
 * Without it the bar belongs to an editor that was opened, and Cancel closes it.
 *
 * `sticky` keeps it at the bottom of the screen while a long section scrolls;
 * place it as the last child of the edited section.
 */
export function EditBar({
  target,
  changeCount,
  onCancel,
  onSave,
  busy = false,
  error,
  blocked,
  sticky = false,
  submit = false,
  inPlace = false,
}: {
  /** What is edited, for the buttons' accessible names: "Save changes to <target>". */
  target: string;
  changeCount: number;
  onCancel: () => void;
  /** Omit with `submit`: the Save button then submits its form, so Enter saves too. */
  onSave?: () => void;
  busy?: boolean;
  error?: string | null;
  /**
   * Why Save is unavailable for now, shown in place of the count: another open
   * edit that the save would drop, say.
   */
  blocked?: string | null;
  sticky?: boolean;
  submit?: boolean;
  inPlace?: boolean;
}) {
  if (inPlace && changeCount === 0 && !error && !busy) return null;
  return (
    <div
      className={`${
        sticky
          ? 'sticky bottom-0 z-10 mt-2 border-t border-gray-200 bg-white/95 py-3 backdrop-blur'
          : 'mt-3 border-t border-gray-100 pt-3'
      }${inPlace ? ' animate-admin-reveal' : ''}`}
    >
      {error ? (
        <p
          className="mb-2 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-600 [overflow-wrap:anywhere]"
          role="alert"
        >
          {error}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center justify-end gap-2">
        <span
          className={`mr-auto text-[12px] ${
            blocked
              ? 'font-medium text-red-600'
              : changeCount > 0
                ? 'font-medium text-amber-700'
                : 'text-gray-500'
          }`}
          role="status"
        >
          {blocked
            ? blocked
            : changeCount > 0
              ? `${changeCount} unsaved ${changeCount === 1 ? 'change' : 'changes'}`
              : 'No changes yet'}
        </span>
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          aria-label={inPlace ? `Discard changes to ${target}` : `Cancel editing ${target}`}
          className={BUTTON_GHOST}
        >
          {inPlace ? 'Discard' : 'Cancel'}
        </button>
        <button
          type={submit ? 'submit' : 'button'}
          onClick={submit ? undefined : onSave}
          disabled={busy || changeCount === 0 || Boolean(blocked)}
          aria-busy={busy || undefined}
          aria-label={`Save changes to ${target}`}
          className={BUTTON_PRIMARY}
        >
          {busy ? 'Saving…' : 'Save changes'}
        </button>
      </div>
    </div>
  );
}

/** The Edit button that opens a section or row for editing. */
export function EditButton({
  target,
  onClick,
  disabled = false,
  label = 'Edit',
  title,
}: {
  target: string;
  onClick: () => void;
  disabled?: boolean;
  label?: string;
  /** Why it is disabled, when that is not obvious. */
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={`${label} ${target}`}
      className={BUTTON_SECONDARY}
    >
      {label}
    </button>
  );
}
