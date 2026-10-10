'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { restartBackend, waitForBackendRestart } from '@/lib/api/config';
import { getErrorMessage } from '@/lib/utils/errors';

import { ConfirmDialog } from './ConfirmDialog';

type Phase = 'idle' | 'confirm' | 'requesting' | 'waiting' | 'back' | 'timeout';

/** The command an operator runs when the backend cannot restart itself. */
export const MANUAL_RESTART_COMMAND = 'docker restart hybridinference-backend';

/**
 * "Restart backend", with the confirmation and the wait.
 *
 * After the backend accepts the request this polls `/health` until it has gone
 * down and come back, then calls `onRestarted` — which should load a fresh
 * page, because the server-rendered site configuration (the setup gate, the
 * configuration banner) was read before the restart.
 */
export function RestartBackendButton({
  onRestarted,
  className,
}: {
  onRestarted: () => void;
  className?: string;
}) {
  const [phase, setPhase] = useState<Phase>('idle');
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  const waitForBackend = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setPhase('waiting');
    const result = await waitForBackendRestart({ signal: controller.signal });
    if (result === 'restarted') {
      setPhase('back');
      onRestarted();
    } else if (result === 'timeout') {
      setPhase('timeout');
    }
  }, [onRestarted]);

  const confirm = async () => {
    setPhase('requesting');
    setError(null);
    try {
      await restartBackend();
    } catch (e) {
      // Keep the dialog open with the reason — typically 409, the process is
      // not under Docker or systemd and would not come back.
      setError(getErrorMessage(e));
      setPhase('confirm');
      return;
    }
    await waitForBackend();
  };

  const busy = phase === 'requesting' || phase === 'waiting' || phase === 'back';

  return (
    <div className="space-y-2">
      <button
        type="button"
        onClick={() => {
          setError(null);
          setPhase('confirm');
        }}
        disabled={busy}
        className={
          className ??
          'rounded-md bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-white transition hover:bg-gray-700 disabled:opacity-40'
        }
      >
        Restart backend
      </button>

      {phase === 'waiting' || phase === 'back' ? (
        <p className="flex items-center gap-2 text-[12px] text-gray-600" role="status">
          <span
            className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-300 border-t-gray-900"
            aria-hidden
          />
          {phase === 'back'
            ? 'The backend is back. Reloading…'
            : 'Restarting the backend. This page reloads when it answers again.'}
        </p>
      ) : null}

      {phase === 'timeout' ? (
        <div className="rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-700" role="alert">
          <p>
            The backend has not come back after two minutes. Check its log (
            <code className="rounded bg-red-100 px-1">docker logs hybridinference-backend</code>)
            and reload this page once it is running.
          </p>
          <button
            type="button"
            onClick={() => void waitForBackend()}
            className="mt-1.5 font-medium underline underline-offset-2"
          >
            Keep waiting
          </button>
        </div>
      ) : null}

      {phase === 'confirm' || phase === 'requesting' ? (
        <ConfirmDialog
          title="Restart the backend?"
          confirmLabel="Restart backend"
          busyLabel="Restarting…"
          tone="danger"
          busy={phase === 'requesting'}
          error={error}
          onConfirm={() => void confirm()}
          onCancel={() => {
            setError(null);
            setPhase('idle');
          }}
        >
          <p>
            The backend process exits and Docker or systemd starts it again. Requests in flight —
            including streaming responses — are dropped, and the API is unavailable until it is
            back, usually within a few seconds.
          </p>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}

/** Shown instead of the button when the backend cannot restart itself. */
export function ManualRestartHint() {
  return (
    <p className="text-[12px] text-gray-600">
      This backend cannot restart itself (it is not running under Docker or systemd, or it runs
      several worker processes). Restart it to apply these settings, for example with{' '}
      <code className="rounded bg-gray-100 px-1 font-mono text-gray-800">
        {MANUAL_RESTART_COMMAND}
      </code>
      .
    </p>
  );
}
