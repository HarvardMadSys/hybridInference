'use client';

import type { ResizablePane } from './useResizablePane';

/**
 * The draggable seam between two panes.
 *
 * The hit area is 5px wide with a 1px line drawn inside it, so the seam still
 * reads as the panel border it replaces while being wide enough to grab. It is
 * a focusable `separator`, which is what makes the width reachable by keyboard
 * (arrows nudge, Home/End jump, double-click resets).
 */
export function PaneResizer({
  pane,
  label,
  controls,
  className = '',
}: {
  pane: ResizablePane;
  label: string;
  controls?: string;
  className?: string;
}): JSX.Element {
  return (
    <div
      {...pane.resizerProps}
      aria-label={label}
      aria-controls={controls}
      title={`${label} (double-click to reset)`}
      className={`group relative w-[5px] shrink-0 cursor-col-resize touch-none select-none focus:outline-none ${className}`}
    >
      <span
        aria-hidden="true"
        className={`pointer-events-none absolute inset-y-0 left-1/2 w-px -translate-x-1/2 transition-colors ${
          pane.dragging
            ? 'bg-crimson'
            : 'bg-gray-200 group-hover:bg-gray-400 group-focus-visible:bg-crimson'
        }`}
      />
    </div>
  );
}
