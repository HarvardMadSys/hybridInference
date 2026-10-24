'use client';

/**
 * Cancel and Save changes for a section or row being edited, with how many
 * changes are unsaved. Save is disabled for a no-op and while a write runs.
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
  sticky = false,
  submit = false,
}: {
  /** What is edited, for the buttons' accessible names: "Save changes to <target>". */
  target: string;
  changeCount: number;
  onCancel: () => void;
  /** Omit with `submit`: the Save button then submits its form, so Enter saves too. */
  onSave?: () => void;
  busy?: boolean;
  error?: string | null;
  sticky?: boolean;
  submit?: boolean;
}) {
  return (
    <div
      className={
        sticky
          ? 'sticky bottom-0 z-10 mt-2 border-t border-gray-200 bg-white/95 py-3 backdrop-blur'
          : 'mt-3 border-t border-gray-100 pt-3'
      }
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
          className={`mr-auto text-[12px] ${changeCount > 0 ? 'font-medium text-amber-700' : 'text-gray-500'}`}
          role="status"
        >
          {changeCount > 0
            ? `${changeCount} unsaved ${changeCount === 1 ? 'change' : 'changes'}`
            : 'No changes yet'}
        </span>
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          aria-label={`Cancel editing ${target}`}
          className="rounded-md px-3 py-1.5 text-[13px] font-medium text-gray-600 hover:bg-gray-100 disabled:opacity-40"
        >
          Cancel
        </button>
        <button
          type={submit ? 'submit' : 'button'}
          onClick={submit ? undefined : onSave}
          disabled={busy || changeCount === 0}
          aria-busy={busy || undefined}
          aria-label={`Save changes to ${target}`}
          className="rounded-md bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:opacity-40"
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
}: {
  target: string;
  onClick: () => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={`${label} ${target}`}
      className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-[13px] font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-40"
    >
      {label}
    </button>
  );
}
