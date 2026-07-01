'use client';

import {
  type KeyboardEvent,
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
    // Only drag-scroll for touch/pen. Emulating drag for mouse would break
    // native text selection (copying request IDs, model names, errors), and
    // mouse/trackpad users can already scroll horizontally natively.
    if (event.pointerType === 'mouse' || event.button !== 0 || isInteractiveTarget(event.target))
      return;

    const el = scrollRef.current;
    if (!el || el.scrollWidth <= el.clientWidth) return;

    if (clearMovedTimerRef.current !== null) {
      window.clearTimeout(clearMovedTimerRef.current);
      clearMovedTimerRef.current = null;
    }
    // Record the drag origin but do NOT capture the pointer yet. Capturing on
    // pointerdown retargets the follow-up `click` to this container, so a
    // clickable child (e.g. an expandable row) never receives its click. Capture
    // is deferred to handlePointerMove, once the gesture passes the slop
    // threshold and is unambiguously a drag.
    dragRef.current = {
      active: true,
      moved: false,
      pointerId: event.pointerId,
      startScrollLeft: el.scrollLeft,
      startX: event.clientX,
    };
  }, []);

  const handlePointerMove = useCallback((event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag.active || drag.pointerId !== event.pointerId) return;

    const el = scrollRef.current;
    if (!el) return;

    const deltaX = event.clientX - drag.startX;
    if (Math.abs(deltaX) <= DRAG_THRESHOLD_PX) return;

    if (!drag.moved) {
      // First movement past the threshold: now that this is a real drag, capture
      // the pointer so panning keeps tracking even if it leaves the scroll area.
      // Plain taps never reach here, so their `click` is left untouched and
      // child click handlers keep working.
      drag.moved = true;
      try {
        el.setPointerCapture(event.pointerId);
      } catch {
        // setPointerCapture throws if the pointer is no longer active (e.g.
        // released between this move being queued and handled). Panning still
        // works without capture, so there's nothing to recover from.
      }
    }
    el.scrollLeft = drag.startScrollLeft - deltaX;
    event.preventDefault();
  }, []);

  const endDrag = useCallback((event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag.active || drag.pointerId !== event.pointerId) return;

    drag.active = false;
    drag.pointerId = null;
    try {
      if (event.currentTarget.hasPointerCapture(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId);
      }
    } catch {
      // These throw if the pointer is no longer active or the environment
      // doesn't implement pointer capture; there's nothing to recover from.
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

  const handleKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    const el = scrollRef.current;
    if (!el) return;
    // Arrow/Home/End scroll the focused region so keyboard-only users can pan
    // it (the tabIndex/role below only make it focusable — WCAG 2.1.1).
    const step = Math.max(48, el.clientWidth * 0.5);
    if (event.key === 'ArrowLeft') el.scrollLeft -= step;
    else if (event.key === 'ArrowRight') el.scrollLeft += step;
    else if (event.key === 'Home') el.scrollLeft = 0;
    else if (event.key === 'End') el.scrollLeft = el.scrollWidth;
    else return;
    // Only prevent default for keys we handled, so Tab/typing still behave.
    event.preventDefault();
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
    // Observe the scrollable content too so overflow is recomputed when it grows
    // or shrinks. firstElementChild keeps this generic (table, list, grid, …).
    const content = el.firstElementChild;
    if (content) resizeObserver?.observe(content);

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
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onClickCapture={handleClickCapture}
      onKeyDown={hasOverflow ? handleKeyDown : undefined}
      aria-label={ariaLabel}
      // Make the scrollable region keyboard-focusable when it overflows so
      // keyboard-only users can scroll it with the arrow keys (WCAG 2.1.1).
      tabIndex={hasOverflow ? 0 : undefined}
      role={hasOverflow ? 'region' : undefined}
    >
      {children}
    </div>
  );
}
