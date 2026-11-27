/**
 * The look that the charts of the two consoles have in common: the box of a
 * tooltip, the text of an axis, and the lines of the grid and of the axis.
 * A chart of its own design keeps its own values.
 */

export const CHART_TOOLTIP_STYLE = {
  fontSize: 12,
  borderRadius: 8,
  border: '1px solid #e5e7eb',
  boxShadow: '0 1px 3px rgba(0,0,0,0.08)',
} as const;

export const CHART_AXIS_TICK = { fontSize: 11, fill: '#9ca3af' } as const;

export const CHART_GRID_STROKE = '#f3f4f6';

export const CHART_AXIS_LINE = { stroke: '#e5e7eb' } as const;
