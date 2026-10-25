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
  /** "Back to editing" for a reviewed edit; "Cancel" for a discrete action. */
  cancelLabel?: string;
  tone?: 'default' | 'danger';
  /** A typed confirmation for the most destructive actions: Confirm waits for this exact text. */
  requireText?: string;
  run: () => Promise<void>;
}

export interface ConfirmAction {
  /**
   * Open the confirmation. Ignored, and `false`, while another confirmation is
   * open (in flight or not): it is never replaced, and nothing is queued
   * behind it. Ask again once it is cancelled or confirmed.
   */
  request: (request: ConfirmRequest) => boolean;
  /** Render this once; it is `null` while nothing waits for confirmation. */
  dialog: ReactNode;
  /** Whether a confirmed write is running. */
  busy: boolean;
  /** Whether a confirmation is open. */
  open: boolean;
}

/**
 * The shared confirmation flow for every admin write: one dialog, a busy state
 * that blocks a second submission and closing, and failures kept in the dialog.
 */
export function useConfirmAction(): ConfirmAction {
  const [pending, setPending] = useState<ConfirmRequest | null>(null);
  const [busy, setBusy] = useState(false);
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

  const confirm = useCallback(async () => {
    const current = openRef.current;
    if (!current || inFlight.current) return;
    if (current.requireText !== undefined && typed !== current.requireText) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await current.run();
      openRef.current = null;
      setPending(null);
      setTyped('');
    } catch (e) {
      // Still open, with the error: it stays the confirmation until retried or cancelled.
      setError(getErrorMessage(e));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }, [typed]);

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

  return { request, dialog, busy, open: pending !== null };
}
