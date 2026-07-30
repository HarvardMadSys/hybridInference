'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent } from 'react';

// Arrow keys nudge the seam; Shift makes the step coarse enough to cross the
// pane in a few presses.
const KEY_STEP_PX = 16;
const KEY_STEP_COARSE_PX = 64;

// Panes in sibling subtrees share the shell's width, so widening the task list
// leaves the workspace less room. React re-renders don't cross that boundary and
// a ResizeObserver only reports a container the pane itself is inside, so every
// pane announces its own resize and the others re-measure.
export const PANE_RESIZE_EVENT = 'agents:pane-resize';

/**
 * Tell every pane to re-measure. Called for each step of a drag, and by anything
 * else that changes how much room is left — collapsing the task list, say.
 */
export function announcePaneResize(): void {
  window.dispatchEvent(new Event(PANE_RESIZE_EVENT));
}

/** Which side of the resizer the pane being sized sits on. */
export type PaneSide = 'start' | 'end';

export type ResizablePaneOptions = {
  /** localStorage key holding the last width, in CSS pixels. */
  storageKey: string;
  defaultWidth: number;
  minWidth: number;
  maxWidth: number;
  side: PaneSide;
  /** Cap as a share of the window; the fallback when no container is measured. */
  maxViewportFraction?: number;
  /**
   * Width the pane across the seam must keep — the real cap on this one. Needs
   * `containerRef` attached to the element holding both panes to have any effect.
   */
  siblingMinWidth?: number;
};

export type PaneResizerProps = {
  role: 'separator';
  'aria-orientation': 'vertical';
  'aria-valuenow': number;
  'aria-valuemin': number;
  'aria-valuemax': number;
  tabIndex: 0;
  onPointerDown: (event: PointerEvent<HTMLElement>) => void;
  onKeyDown: (event: KeyboardEvent<HTMLElement>) => void;
  onDoubleClick: () => void;
};

export type ResizablePane = {
  width: number;
  dragging: boolean;
  reset: () => void;
  /**
   * Optional: attach to the element holding both panes —
   * `<div ref={pane.containerRef}>` — to size against it instead of the window.
   */
  containerRef: (node: HTMLElement | null) => void;
  resizerProps: PaneResizerProps;
};

const DEFAULT_VIEWPORT_FRACTION = 0.6;

// A drag stops where the pane across the seam would stop being usable. Measuring
// the shared container is what makes that exact: the transcript's floor has to
// survive a wide task list too, and only the container knows what is left after
// the sidebar took its share.
//
// This is where the pane can actually reach right now, which is also the number
// the separator reports as its maximum — announcing the configured `maxWidth`
// would promise a screen reader a range the drag refuses to enter.
function upperBound(options: ResizablePaneOptions, containerWidth: number): number {
  const {
    minWidth,
    maxWidth,
    maxViewportFraction = DEFAULT_VIEWPORT_FRACTION,
    siblingMinWidth = 0,
  } = options;
  // A container that reports 0 is one that has not been laid out (or is
  // hidden); fall back to the window rule rather than clamping to the minimum.
  const outerCap =
    containerWidth > 0
      ? containerWidth - siblingMinWidth
      : typeof window === 'undefined'
        ? maxWidth
        : Math.round(window.innerWidth * maxViewportFraction);
  return Math.max(minWidth, Math.min(maxWidth, outerCap));
}

function clampWidth(next: number, options: ResizablePaneOptions, containerWidth: number): number {
  return Math.min(
    Math.max(Math.round(next), options.minWidth),
    upperBound(options, containerWidth),
  );
}

/**
 * Width state for a drag-resizable pane: pointer drag, keyboard nudges on the
 * separator, double-click reset, and a width persisted per browser.
 *
 * The remembered width and the rendered one are deliberately separate. A pane
 * that no longer fits is rendered narrower, but the width the user picked is
 * kept, so a window that narrows and widens again — or a reload that lands
 * mid-layout, before the shell has its real size — ends up back where they left
 * it instead of ratcheting down to the minimum.
 *
 * Drag tracking runs on `window` rather than through pointer capture so a fast
 * drag that outruns the 5px handle — or leaves it entirely — still resizes, and
 * still ends when the button comes up anywhere on the page.
 */
export function useResizablePane(options: ResizablePaneOptions): ResizablePane {
  const { storageKey, defaultWidth, minWidth, maxWidth, side } = options;
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const [width, setWidth] = useState(defaultWidth);
  const [reachableMax, setReachableMax] = useState(maxWidth);
  const [drag, setDrag] = useState<{ startX: number; startWidth: number } | null>(null);
  const widthRef = useRef(width);
  const desiredRef = useRef(defaultWidth);
  const announceRef = useRef(false);
  const containerNodeRef = useRef<HTMLElement | null>(null);
  const observerRef = useRef<ResizeObserver | null>(null);

  /** Render the remembered width, narrowed to whatever the layout allows now. */
  const settle = useCallback(() => {
    const upper = upperBound(optionsRef.current, containerNodeRef.current?.clientWidth ?? 0);
    const clamped = Math.min(Math.max(desiredRef.current, optionsRef.current.minWidth), upper);
    widthRef.current = clamped;
    setWidth(clamped);
    setReachableMax(upper);
  }, []);

  // A gesture is authoritative: it is already limited by the layout, so the
  // handle never runs a dead zone where dragging back does nothing.
  const applyGesture = useCallback(
    (next: number) => {
      desiredRef.current = clampWidth(
        next,
        optionsRef.current,
        containerNodeRef.current?.clientWidth ?? 0,
      );
      announceRef.current = true;
      settle();
    },
    [settle],
  );

  // Watch the container for size changes no one announced. A callback ref is how
  // React reports the node arriving, changing, or going away, so the observer
  // follows it without an effect re-checking on every render. Stable identity:
  // `settle` never changes, so React does not re-run this between renders.
  const containerRef = useCallback(
    (node: HTMLElement | null) => {
      observerRef.current?.disconnect();
      observerRef.current = null;
      containerNodeRef.current = node;
      if (!node || typeof ResizeObserver === 'undefined') return;
      const observer = new ResizeObserver(settle);
      observer.observe(node);
      observerRef.current = observer;
    },
    [settle],
  );

  // Announce once the new width is actually in the DOM, so the panes that
  // re-measure see this layout and not the one from a frame ago. Only gestures
  // announce — a pane reacting to someone else's resize must not answer back.
  useEffect(() => {
    if (!announceRef.current) return;
    announceRef.current = false;
    announcePaneResize();
  }, [width]);

  // Keyboard resizing persists on every keypress, so held arrows would rewrite
  // the same value dozens of times a second once the seam reaches a bound.
  const persist = useCallback(() => {
    const value = String(desiredRef.current);
    try {
      if (window.localStorage.getItem(storageKey) === value) return;
      window.localStorage.setItem(storageKey, value);
    } catch {
      // Blocked storage only loses the remembered width, never the resize.
    }
  }, [storageKey]);

  // Restore after mount, not during render: the server renders `defaultWidth`,
  // so reading storage in the initial state would be a hydration mismatch. Only
  // the pane's own bounds apply here — the layout gets its say in `settle`.
  useEffect(() => {
    let stored: string | null = null;
    try {
      stored = window.localStorage.getItem(storageKey);
    } catch {
      stored = null;
    }
    const parsed = Number(stored);
    if (stored !== null && Number.isFinite(parsed) && parsed > 0) {
      desiredRef.current = Math.min(Math.max(Math.round(parsed), minWidth), maxWidth);
    }
    settle();
  }, [maxWidth, minWidth, settle, storageKey]);

  // Re-settle whenever the available space changes: a resized window, or another
  // pane taking a different share of it.
  useEffect(() => {
    window.addEventListener('resize', settle);
    window.addEventListener(PANE_RESIZE_EVENT, settle);
    return () => {
      window.removeEventListener('resize', settle);
      window.removeEventListener(PANE_RESIZE_EVENT, settle);
    };
  }, [settle]);

  useEffect(() => {
    if (!drag) return;
    function onMove(event: globalThis.PointerEvent) {
      const delta = event.clientX - drag!.startX;
      applyGesture(drag!.startWidth + (side === 'start' ? delta : -delta));
    }
    function onEnd() {
      setDrag(null);
      persist();
    }
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onEnd);
    window.addEventListener('pointercancel', onEnd);
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onEnd);
      window.removeEventListener('pointercancel', onEnd);
    };
  }, [applyGesture, drag, persist, side]);

  // Without this, dragging across the transcript selects text under the cursor
  // and the I-beam flickers over every element the pointer crosses.
  useEffect(() => {
    if (!drag) return;
    const { body } = document;
    const previousSelect = body.style.userSelect;
    const previousCursor = body.style.cursor;
    body.style.userSelect = 'none';
    body.style.cursor = 'col-resize';
    return () => {
      body.style.userSelect = previousSelect;
      body.style.cursor = previousCursor;
    };
  }, [drag]);

  const onPointerDown = useCallback((event: PointerEvent<HTMLElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    setDrag({ startX: event.clientX, startWidth: widthRef.current });
  }, []);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLElement>) => {
      const grow = side === 'start' ? 1 : -1;
      const step = event.shiftKey ? KEY_STEP_COARSE_PX : KEY_STEP_PX;
      if (event.key === 'ArrowLeft') applyGesture(widthRef.current - step * grow);
      else if (event.key === 'ArrowRight') applyGesture(widthRef.current + step * grow);
      else if (event.key === 'Home') applyGesture(side === 'start' ? minWidth : maxWidth);
      else if (event.key === 'End') applyGesture(side === 'start' ? maxWidth : minWidth);
      else return;
      event.preventDefault();
      persist();
    },
    [applyGesture, maxWidth, minWidth, persist, side],
  );

  const reset = useCallback(() => {
    applyGesture(defaultWidth);
    persist();
  }, [applyGesture, defaultWidth, persist]);

  return {
    width,
    dragging: drag !== null,
    reset,
    containerRef,
    resizerProps: {
      role: 'separator',
      'aria-orientation': 'vertical',
      'aria-valuenow': width,
      'aria-valuemin': minWidth,
      'aria-valuemax': reachableMax,
      tabIndex: 0,
      onPointerDown,
      onKeyDown,
      onDoubleClick: reset,
    },
  };
}
