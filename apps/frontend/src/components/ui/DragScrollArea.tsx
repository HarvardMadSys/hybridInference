'use client';

import {
  type MouseEvent,
  type PointerEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';

const DRAG_THRESHOLD_PX = 4;

type DragState = {
  active: boolean;
  moved: boolean;
  pointerId: number | null;
  startScrollLeft: number;
  startX: number;
};

function isInteractiveTarget(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    target.closest('a, button, input, select, textarea, summary, [role="button"]') !== null
  );
}

/**
 * Horizontal scroll wrapper for wide content (e.g. data tables) that scrolls
 * reliably on touch, trackpad, and mouse.
 *
 * Native `overflow-x-auto` inside a vertically-scrolling page is unreliable on
 * mobile: the browser's touch axis-lock frequently keeps a swipe on the
 * vertical axis, so horizontal swipes only nudge the content a little before
 * stalling. Setting `touch-action: pan-y` hands vertical panning to the browser
 * (so the page still scrolls) while horizontal movement is delivered to the
 * pointer handlers here, which drive `scrollLeft` directly — 1:1 with the
 * finger/cursor. A drag that actually moved suppresses the trailing click so a
 * scroll gesture never toggles a clickable row.
 */
export function DragScrollArea({
  children,
  className = '',
  ariaLabel,
}: {
  children: ReactNode;
  className?: string;
  ariaLabel?: string;
}): JSX.Element {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const clearMovedTimerRef = useRef<number | null>(null);
  const dragRef = useRef<DragState>({
    active: false,
    moved: false,
    pointerId: null,
    startScrollLeft: 0,
    startX: 0,
  });
  const [hasOverflow, setHasOverflow] = useState(false);

  const updateOverflow = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    setHasOverflow(el.scrollWidth > el.clientWidth + 1);
  }, []);

  const handlePointerDown = useCallback((event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || isInteractiveTarget(event.target)) return;

    const el = scrollRef.current;
    if (!el || el.scrollWidth <= el.clientWidth) return;

    if (clearMovedTimerRef.current !== null) {
      window.clearTimeout(clearMovedTimerRef.current);
      clearMovedTimerRef.current = null;
    }
    dragRef.current = {
      active: true,
      moved: false,
      pointerId: event.pointerId,
      startScrollLeft: el.scrollLeft,
      startX: event.clientX,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }, []);

  const handlePointerMove = useCallback((event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag.active || drag.pointerId !== event.pointerId) return;

    const el = scrollRef.current;
    if (!el) return;

    const deltaX = event.clientX - drag.startX;
    if (Math.abs(deltaX) <= DRAG_THRESHOLD_PX) return;

    drag.moved = true;
    el.scrollLeft = drag.startScrollLeft - deltaX;
    event.preventDefault();
  }, []);

  const endDrag = useCallback((event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag.active || drag.pointerId !== event.pointerId) return;

    drag.active = false;
    drag.pointerId = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    if (drag.moved) {
      // Keep `moved` set briefly so the trailing click (fired after pointerup)
      // is suppressed, then reset it.
      clearMovedTimerRef.current = window.setTimeout(() => {
        dragRef.current.moved = false;
        clearMovedTimerRef.current = null;
      }, 300);
    }
  }, []);

  const handleClickCapture = useCallback((event: MouseEvent<HTMLDivElement>) => {
    if (!dragRef.current.moved) return;
    if (clearMovedTimerRef.current !== null) {
      window.clearTimeout(clearMovedTimerRef.current);
      clearMovedTimerRef.current = null;
    }
    dragRef.current.moved = false;
    event.preventDefault();
    event.stopPropagation();
  }, []);

  useEffect(() => {
    updateOverflow();

    const el = scrollRef.current;
    if (!el) return undefined;

    const resizeObserver =
      typeof ResizeObserver !== 'undefined' ? new ResizeObserver(updateOverflow) : null;
    resizeObserver?.observe(el);
    const table = el.querySelector('table');
    if (table) resizeObserver?.observe(table);

    window.addEventListener('resize', updateOverflow);
    return () => {
      resizeObserver?.disconnect();
      window.removeEventListener('resize', updateOverflow);
      if (clearMovedTimerRef.current !== null) {
        window.clearTimeout(clearMovedTimerRef.current);
        clearMovedTimerRef.current = null;
      }
    };
  }, [updateOverflow]);

  return (
    <div
      ref={scrollRef}
      className={`touch-pan-y overflow-x-auto overscroll-x-contain ${
        hasOverflow ? 'cursor-grab active:cursor-grabbing' : ''
      } ${className}`}
      onScroll={updateOverflow}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onClickCapture={handleClickCapture}
      aria-label={ariaLabel}
    >
      {children}
    </div>
  );
}
