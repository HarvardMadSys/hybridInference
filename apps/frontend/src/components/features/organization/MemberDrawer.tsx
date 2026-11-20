'use client';

import { useEffect, useId, useRef, type ReactNode } from 'react';
import { useT } from '@/components/providers/useT';
import { BUTTON_SECONDARY } from '@/components/features/admin/adminStyles';

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]';

/**
 * The details of one member, in a panel at the right side of the window. The
 * list stays behind it, dimmed, so the reader keeps the place in it.
 *
 * It is a modal layer, as a confirmation is: focus moves to Close when it
 * opens and back to the member's row when it closes, Tab stays inside, and
 * Escape, Close and a click on the dimmed list close it. A press that began
 * in the panel and ended on the dimmed list does not: that is the end of a
 * selection of text. On a phone the panel has the width of the screen.
 *
 * `suspended` is for the time a confirmation is open over the panel. The
 * confirmation then has the keys: Escape closes it alone, and its own Tab
 * order holds.
 */
export function MemberDrawer({
  title,
  subtitle,
  suspended = false,
  onClose,
  children,
}: {
  /** The member's name. */
  title: string;
  /** The member's email, when the name is not the email. */
  subtitle?: string | null;
  suspended?: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  const t = useT();
  const titleId = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  // Whether the press that is now down began on the dimmed list itself.
  const pressedOutside = useRef(false);
  // The key handler is installed once; these keep it reading the latest props.
  const suspendedRef = useRef(suspended);
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    suspendedRef.current = suspended;
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    const restore = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    return () => restore?.focus();
  }, []);

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (suspendedRef.current) return;
      if (event.key === 'Escape') {
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const container = containerRef.current;
      if (!container) return;
      const focusables = container.querySelectorAll<HTMLElement>(FOCUSABLE);
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !container.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !container.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return (
    <div
      className="fixed inset-0 z-40 flex justify-end bg-black/40"
      onMouseDown={(event) => {
        pressedOutside.current = event.target === event.currentTarget;
      }}
      onClick={(event) => {
        // A click reaches this element also when the button went down inside
        // the panel and came up here; only a press on the dimmed list closes.
        if (pressedOutside.current && event.target === event.currentTarget) onClose();
        pressedOutside.current = false;
      }}
    >
      <div
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="flex h-full w-full max-w-[60rem] flex-col border-gray-200 bg-gray-50 shadow-xl sm:border-l"
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-gray-200 bg-white px-5 py-4">
          <div className="min-w-0">
            <h2
              id={titleId}
              className="truncate text-[16px] font-semibold tracking-tight text-gray-900"
            >
              {title}
            </h2>
            {subtitle ? <p className="truncate text-[12px] text-gray-500">{subtitle}</p> : null}
          </div>
          <button ref={closeRef} type="button" className={BUTTON_SECONDARY} onClick={onClose}>
            {t('organization.member.close', 'Close')}
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-5">{children}</div>
      </div>
    </div>
  );
}
