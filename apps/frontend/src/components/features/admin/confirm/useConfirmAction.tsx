'use client';

import { useCallback, useRef, useState, type ReactNode } from 'react';

import { ConfirmDialog } from '@/components/features/configuration/ConfirmDialog';
import { getErrorMessage } from '@/lib/utils/errors';

/**
 * One write the administrator is asked to confirm.
 *
 * `run` performs the write. When it resolves the dialog closes; when it throws
 * the dialog stays open, busy no more, with the error inside it, so the write
 * can be retried or the administrator can go back without losing anything.
 */
export interface ConfirmRequest {
  title: string;
  body: ReactNode;
  confirmLabel: string;
  busyLabel?: string;
  /** "Cancel" unless what is kept has a better name ("Keep running", "Keep my changes"). */
  cancelLabel?: string;
  tone?: 'default' | 'danger';
  /** A typed confirmation for the most destructive actions: Confirm waits for this exact text. */
  requireText?: string;
  /**
   * What the write saves, when several in-place editors share this
   * confirmation: `savingKey` then tells each one whether the save is its own.
   */
  key?: string;
  run: () => Promise<void>;
}

/**
 * One write made by the click that asks for it, with no confirmation: a save,
 * a create, or an action as easy to undo as it is to do.
 */
export interface DirectWrite {
  /** What the write saves, as for a confirmation: `savingKey` names it while it runs. */
  key?: string;
  run: () => Promise<void>;
}

export interface ConfirmAction {
  /**
   * Open the confirmation. Ignored, and `false`, while another confirmation is
   * open (in flight or not) or a write runs: it is never replaced, and nothing
   * is queued behind it. Ask again once it is cancelled or confirmed.
   */
  request: (request: ConfirmRequest) => boolean;
  /**
   * Make the write at once. The same guard holds it: ignored while another
   * write runs or a confirmation is open, and nothing is queued. Resolves to
   * the failure's message, for the caller to show where the write was asked
   * for, or to `null` once the write succeeded or was ignored.
   */
  perform: (write: DirectWrite) => Promise<string | null>;
  /** Render this once; it is `null` while nothing waits for confirmation. */
  dialog: ReactNode;
  /** Whether a write runs, confirmed or direct. */
  busy: boolean;
  /** The `key` of the write that is running, if it has one. */
  savingKey: string | null;
  /** Whether a confirmation is open. */
  open: boolean;
}

/**
 * The shared flow for every admin write: one busy state that blocks a second
 * submission, and one dialog for the writes confirmed first, which keeps their
 * failures inside it.
 */
export function useConfirmAction(): ConfirmAction {
  const [pending, setPending] = useState<ConfirmRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [typed, setTyped] = useState('');
  const inFlight = useRef(false);
  // The open confirmation, set the moment it opens. A request that resolves
  // after a read (a count, a preview) can arrive while another confirmation is
  // open; replacing that one would turn the button under the administrator's
  // pointer into a different write. State alone would miss two requests made
  // before React re-renders.
  const openRef = useRef<ConfirmRequest | null>(null);

  const request = useCallback((next: ConfirmRequest) => {
    if (inFlight.current || openRef.current) return false;
    openRef.current = next;
    setError(null);
    setTyped('');
    setPending(next);
    return true;
  }, []);

  const close = useCallback(() => {
    if (inFlight.current) return;
    openRef.current = null;
    setPending(null);
    setError(null);
    setTyped('');
  }, []);

  /** Run one write as the busy one; the caller has checked that none runs. */
  const runWrite = useCallback(async (key: string | undefined, run: () => Promise<void>) => {
    inFlight.current = true;
    setBusy(true);
    setSavingKey(key ?? null);
    try {
      await run();
    } finally {
      inFlight.current = false;
      setBusy(false);
      setSavingKey(null);
    }
  }, []);

  const perform = useCallback(
    async (write: DirectWrite) => {
      if (inFlight.current || openRef.current) return null;
      try {
        await runWrite(write.key, write.run);
        return null;
      } catch (e) {
        return getErrorMessage(e);
      }
    },
    [runWrite],
  );

  const confirm = useCallback(async () => {
    const current = openRef.current;
    if (!current || inFlight.current) return;
    if (current.requireText !== undefined && typed !== current.requireText) return;
    setError(null);
    try {
      await runWrite(current.key, current.run);
      openRef.current = null;
      setPending(null);
      setTyped('');
    } catch (e) {
      // Still open, with the error: it stays the confirmation until retried or cancelled.
      setError(getErrorMessage(e));
    }
  }, [typed, runWrite]);

  const dialog = pending ? (
    <ConfirmDialog
      title={pending.title}
      confirmLabel={pending.confirmLabel}
      busyLabel={pending.busyLabel ?? `${pending.confirmLabel}…`}
      cancelLabel={pending.cancelLabel ?? 'Cancel'}
      tone={pending.tone}
      busy={busy}
      error={error}
      confirmDisabled={pending.requireText !== undefined && typed !== pending.requireText}
      onConfirm={() => void confirm()}
      onCancel={close}
    >
      {pending.body}
      {pending.requireText !== undefined ? (
        <label className="block text-[12px] text-gray-700">
          Type <span className="font-mono font-semibold">{pending.requireText}</span> to confirm.
          <input
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            disabled={busy}
            autoComplete="off"
            spellCheck={false}
            aria-label={`Type ${pending.requireText} to confirm`}
            className="mt-1 w-full rounded-md border border-gray-300 px-2.5 py-1.5 font-mono text-[13px] text-gray-900 focus:border-gray-500 focus:outline-none"
          />
        </label>
      ) : null}
    </ConfirmDialog>
  ) : null;

  return {
    request,
    perform,
    dialog,
    busy,
    savingKey,
    open: pending !== null,
  };
}
