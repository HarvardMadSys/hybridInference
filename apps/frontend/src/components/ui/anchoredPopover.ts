'use client';

import { useLayoutEffect, useState, type CSSProperties, type RefObject } from 'react';

function sameStyle(a: CSSProperties, b: CSSProperties): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]) as Set<keyof CSSProperties>;
  for (const key of keys) if (a[key] !== b[key]) return false;
  return true;
}

/**
 * Before it is measured the popup is already fixed (out of the page's flow,
 * so it can never push or scroll anything) and invisible.
 */
const UNPLACED: CSSProperties = { position: 'fixed', top: 0, left: 0, visibility: 'hidden' };

export interface AnchoredPopoverOptions {
  /** Space between the trigger and the popup. */
  gap?: number;
  /** Distance kept from the viewport edges. */
  margin?: number;
  /** Tallest the popup may be; it scrolls inside beyond this. */
  maxHeight?: number;
  /** Below this much room underneath, open above if there is more room there. */
  preferredHeight?: number;
  /** Fixed popup width; by default at least the trigger's, up to `maxWidth`. */
  width?: number;
  maxWidth?: number;
}

/**
 * Places a `position: fixed` popup against its trigger: below it, or above
 * when there is more room there, never wider or taller than the viewport, and
 * moved back inside it when the trigger is near an edge. Follows the trigger
 * on scroll (of any ancestor) and resize while `open`.
 *
 * Fixed positioning lets the popup escape a table's overflow or a dialog's
 * clipping without a global z-index scheme or a scroll lock.
 */
export function useAnchoredPopover(
  open: boolean,
  triggerRef: RefObject<HTMLElement | null>,
  popoverRef: RefObject<HTMLElement | null>,
  {
    gap = 4,
    margin = 8,
    maxHeight = 320,
    preferredHeight = 240,
    width,
    maxWidth = 384,
  }: AnchoredPopoverOptions = {},
): CSSProperties {
  const [style, setStyle] = useState<CSSProperties>(UNPLACED);

  useLayoutEffect(() => {
    if (!open) {
      setStyle(UNPLACED);
      return;
    }
    const place = () => {
      const trigger = triggerRef.current;
      if (!trigger) return;
      const rect = trigger.getBoundingClientRect();
      const viewport = window.visualViewport;
      const left0 = viewport?.offsetLeft ?? 0;
      const top0 = viewport?.offsetTop ?? 0;
      const viewportWidth = viewport?.width ?? window.innerWidth;
      const viewportHeight = viewport?.height ?? window.innerHeight;

      const widest = Math.max(0, viewportWidth - margin * 2);
      const minWidth = Math.min(width ?? Math.max(rect.width, 176), widest);
      const maxW = Math.min(width ?? Math.max(maxWidth, minWidth), widest);
      // The popup's own width once its content is laid out, to keep it on
      // screen; before the first layout, assume the widest it may be.
      const measured = popoverRef.current?.offsetWidth || maxW;
      const actual = Math.min(Math.max(measured, minWidth), maxW);
      const left = Math.max(
        left0 + margin,
        Math.min(rect.left, left0 + viewportWidth - actual - margin),
      );

      const below = Math.max(0, top0 + viewportHeight - rect.bottom - gap - margin);
      const above = Math.max(0, rect.top - top0 - gap - margin);
      const placeBelow = below >= preferredHeight || below >= above;
      const next: CSSProperties = {
        position: 'fixed',
        left,
        minWidth,
        maxWidth: maxW,
        ...(width !== undefined ? { width: minWidth } : {}),
        maxHeight: Math.min(maxHeight, placeBelow ? below : above),
        ...(placeBelow
          ? { top: rect.bottom + gap }
          : { bottom: window.innerHeight - rect.top + gap }),
      };
      // Same place, same object: nothing downstream re-runs for a no-op.
      setStyle((current) => (sameStyle(current, next) ? current : next));
    };
    // The page or a container scrolling moves the trigger; the popup's own
    // list scrolling does not, and re-placing then would only churn.
    const onScroll = (event: Event) => {
      if (event.target instanceof Node && popoverRef.current?.contains(event.target)) return;
      place();
    };
    place();
    // Once more after the content's real width is known.
    const frame = window.requestAnimationFrame(place);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', onScroll, true);
    window.visualViewport?.addEventListener('resize', place);
    window.visualViewport?.addEventListener('scroll', place);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', onScroll, true);
      window.visualViewport?.removeEventListener('resize', place);
      window.visualViewport?.removeEventListener('scroll', place);
    };
  }, [open, triggerRef, popoverRef, gap, margin, maxHeight, preferredHeight, width, maxWidth]);

  return style;
}

/**
 * Where a popup opened from `trigger` should be rendered: inside the nearest
 * dialog (so the dialog's own focus and click handling still contain it),
 * else inside the console's theme wrapper (whose dark-mode rules are scoped
 * to `.console-theme`), else the body.
 */
export function popoverContainer(trigger: HTMLElement | null): HTMLElement | null {
  if (typeof document === 'undefined') return null;
  return (
    trigger?.closest<HTMLElement>('[role="dialog"]') ??
    trigger?.closest<HTMLElement>('.console-theme') ??
    document.body
  );
}
