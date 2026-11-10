'use client';

import { BUTTON_GHOST, BUTTON_PRIMARY } from '../adminStyles';

/**
 * Cancel and Save changes for a section or row being edited, with how many
 * changes are unsaved. Save writes at once. It is disabled for a no-op, while
 * the save is `blocked` and while a write runs. A failed save shows its
 * `error` here, and what it did not write stays as drafts.
 *
 * `busy` is this bar's own save, which reads "Saving…". `held` is any other
 * write on the same write flow: the bar waits for it, since the flow would
 * ignore a save started meanwhile.
 *
 * `inPlace` is for settings whose fields are always editable: the bar appears
 * with the first change (or an error to show), and its Cancel reads Discard.
 * A write in flight alone does not bring it up, as one write flow often serves
 * other actions on the screen too.
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
  held = false,
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
  held?: boolean;
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
  if (inPlace && changeCount === 0 && !error) return null;
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
          disabled={busy || held}
          aria-label={inPlace ? `Discard changes to ${target}` : `Cancel editing ${target}`}
          className={BUTTON_GHOST}
        >
          {inPlace ? 'Discard' : 'Cancel'}
        </button>
        <button
          type={submit ? 'submit' : 'button'}
          onClick={submit ? undefined : onSave}
          disabled={busy || held || changeCount === 0 || Boolean(blocked)}
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
