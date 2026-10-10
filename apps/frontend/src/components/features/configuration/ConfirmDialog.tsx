'use client';

import { useEffect, useId, useRef, type ReactNode } from 'react';

/**
 * A modal confirmation: focus moves to Cancel when it opens and back to the
 * trigger when it closes, Escape and the backdrop cancel, and Tab stays inside.
 * Nothing closes it while `busy`, so a request in flight cannot lose its UI.
 */
export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  busyLabel,
  tone = 'default',
  busy = false,
  error,
  onConfirm,
  onCancel,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  busyLabel?: string;
  tone?: 'default' | 'danger';
  busy?: boolean;
  /** Shown inside the dialog, so a failed confirm can be retried in place. */
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const titleId = useId();
  const bodyId = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  // The key handler is installed once; these keep it reading the latest props.
  const busyRef = useRef(busy);
  const onCancelRef = useRef(onCancel);
  useEffect(() => {
    busyRef.current = busy;
    onCancelRef.current = onCancel;
  });

  useEffect(() => {
    const restore = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cancelRef.current?.focus();
    return () => restore?.focus();
  }, []);

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        if (!busyRef.current) onCancelRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const container = containerRef.current;
      if (!container) return;
      const focusables = container.querySelectorAll<HTMLElement>('button:not([disabled])');
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !container.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const confirmClass =
    tone === 'danger'
      ? 'bg-red-600 hover:bg-red-700 focus:ring-red-600'
      : 'bg-gray-900 hover:bg-gray-700 focus:ring-gray-900';

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={() => {
        if (!busy) onCancel();
      }}
    >
      <div
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        className="w-full max-w-md rounded-xl bg-white p-5 shadow-lg"
        onClick={(event) => event.stopPropagation()}
      >
        <h3 id={titleId} className="text-[15px] font-semibold text-gray-900">
          {title}
        </h3>
        <div id={bodyId} className="mt-2 space-y-2 text-[13px] text-gray-600">
          {children}
        </div>
        {error ? (
          <p className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-[12px] text-red-700" role="alert">
            {error}
          </p>
        ) : null}
        <div className="mt-4 flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="rounded-md px-3 py-1.5 text-[13px] font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            aria-busy={busy || undefined}
            className={`rounded-md px-3 py-1.5 text-[13px] font-medium text-white focus:outline-none focus:ring-2 focus:ring-offset-2 disabled:opacity-40 ${confirmClass}`}
          >
            {busy ? (busyLabel ?? confirmLabel) : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
