'use client';

import { useEffect, useId, useRef, type ReactNode } from 'react';

/**
 * A modal panel for the page's chart zoom and export: focus moves to its first
 * control when it opens and back to the trigger when it closes, Escape and the
 * backdrop close it, and Tab stays inside.
 */
export function InfraDialog({
  title,
  description,
  actions,
  children,
  wide = false,
  onClose,
}: {
  title: string;
  description?: ReactNode;
  /** Buttons beside the title; the first one takes the focus. */
  actions: ReactNode;
  children: ReactNode;
  wide?: boolean;
  onClose: () => void;
}) {
  const titleId = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    const restore = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    containerRef.current?.querySelector<HTMLElement>('button, select, input')?.focus();
    return () => restore?.focus();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const container = containerRef.current;
      if (!container) return;
      const focusables = container.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled])',
      );
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

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
    >
      <div
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`flex max-h-[calc(100dvh-2rem)] w-full flex-col gap-3 overflow-y-auto rounded-2xl border border-gray-200 bg-white p-5 shadow-lg ${
          wide ? 'max-w-5xl' : 'max-w-md'
        }`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="flex flex-wrap items-start justify-between gap-3">
          <h2 id={titleId} className="text-lg font-semibold text-gray-900">
            {title}
          </h2>
          <div className="flex flex-wrap gap-2">{actions}</div>
        </header>
        {description ? <div className="space-y-1 text-sm text-gray-600">{description}</div> : null}
        {children}
      </div>
    </div>
  );
}

/** The small outlined button the charts and dialogs use. */
export const smallButton =
  'rounded-md border border-gray-200 bg-white px-2.5 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-crimson/40 disabled:opacity-50';
