import { describe, expect, it, vi } from 'vitest';

import {
  computeLayout,
  drawChart,
  formatTick,
  formatTipTime,
  FULL_WINDOW,
  hoverAt,
  panWindow,
  seriesCsv,
  zoomWindow,
  type ChartSeries,
} from './chartCanvas';

const T0 = 1_795_800_000_000;
const MIN = 60_000;

function line(key: string, values: Array<number | null>, color = '#000'): ChartSeries {
  return { key, label: key, color, points: values.map((v, i) => ({ t: T0 + i * MIN, v })) };
}

const SERIES = [line('a', [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110]), line('b', [5, null, 7])];

describe('computeLayout', () => {
  it('has nothing to lay out without a single value', () => {
    expect(computeLayout([line('a', [null, null])], {}, FULL_WINDOW, 720, 180)).toBeNull();
    expect(computeLayout([], {}, FULL_WINDOW, 720, 180)).toBeNull();
  });

  it('spans the data, from 0 to at least the requested top, raised by the values shown', () => {
    const layout = computeLayout(SERIES, { max: 100 }, FULL_WINDOW, 720, 180);
    expect(layout).toMatchObject({ t0: T0, t1: T0 + 10 * MIN, min: 0, max: 110 });
    expect(layout?.innerW).toBe(720 - 48 - 12);
    // Narrow charts keep less room for the labels.
    expect(computeLayout(SERIES, {}, FULL_WINDOW, 400, 180)?.pad.l).toBe(34);
    // Only the values inside the window raise the top.
    const zoomed = computeLayout(SERIES, { max: 100 }, { tMin: T0, tMax: T0 + 2 * MIN }, 720, 180);
    expect(zoomed?.max).toBe(100);
  });
});

describe('hoverAt', () => {
  const layout = computeLayout(SERIES, {}, FULL_WINDOW, 720, 180);

  it('finds the nearest time with a point, and each series’ value there', () => {
    // Two minutes in: series b has a point there too.
    const x = (layout?.pad.l ?? 0) + (2 / 10) * (layout?.innerW ?? 0);
    const hover = hoverAt(SERIES, layout, x + 3);
    expect(hover?.t).toBe(T0 + 2 * MIN);
    expect(hover?.rows).toEqual([
      { label: 'a', color: '#000', v: 30 },
      { label: 'b', color: '#000', v: 7 },
    ]);
  });

  it('leaves out a series with no point near that time', () => {
    const x = (layout?.pad.l ?? 0) + (8 / 10) * (layout?.innerW ?? 0);
    expect(hoverAt(SERIES, layout, x)?.rows.map((r) => r.label)).toEqual(['a']);
  });

  it('has nothing outside the plot', () => {
    expect(hoverAt(SERIES, layout, 2)).toBeNull();
    expect(hoverAt(SERIES, null, 100)).toBeNull();
  });
});

describe('zooming and panning', () => {
  const layout = computeLayout(SERIES, {}, FULL_WINDOW, 720, 180);
  if (!layout) throw new Error('no layout');

  it('narrows the window by a fifth around the pointer, and widens it back within the data', () => {
    const middle = layout.pad.l + layout.innerW / 2;
    const zoomedIn = zoomWindow(layout, middle, -100);
    expect((zoomedIn.tMax ?? 0) - (zoomedIn.tMin ?? 0)).toBeCloseTo(8 * MIN);
    expect(zoomedIn.tMin).toBeCloseTo(T0 + MIN);
    const out = zoomWindow(layout, middle, 100);
    expect(out).toEqual({ tMin: T0, tMax: T0 + 10 * MIN });
  });

  it('never narrows the window under a minute', () => {
    let win = FULL_WINDOW;
    for (let i = 0; i < 40; i += 1) {
      const next = computeLayout(SERIES, {}, win, 720, 180);
      if (!next) throw new Error('no layout');
      win = zoomWindow(next, next.pad.l, -100);
    }
    expect((win.tMax ?? 0) - (win.tMin ?? 0)).toBe(MIN);
  });

  it('pans with the drag and stops at the data’s edges', () => {
    const zoomed = computeLayout(SERIES, {}, { tMin: T0 + 2 * MIN, tMax: T0 + 4 * MIN }, 720, 180);
    if (!zoomed) throw new Error('no layout');
    const back = panWindow(zoomed, zoomed.innerW / 2);
    expect(back).toEqual({ tMin: T0 + MIN, tMax: T0 + 3 * MIN });
    expect(panWindow(zoomed, zoomed.innerW * 10)).toEqual({ tMin: T0, tMax: T0 + 2 * MIN });
    expect(panWindow(zoomed, -zoomed.innerW * 10)).toEqual({
      tMin: T0 + 8 * MIN,
      tMax: T0 + 10 * MIN,
    });
  });
});

describe('seriesCsv', () => {
  it('writes one row per time and one column per series, quoting labels that need it', () => {
    const csv = seriesCsv([line('a, GPU 0', [1, 2]), line('b', [null, 3])]);
    expect(csv.split('\n')).toEqual([
      'time_iso,unix_ms,"a, GPU 0",b',
      `${new Date(T0).toISOString()},${T0},1,`,
      `${new Date(T0 + MIN).toISOString()},${T0 + MIN},2,3`,
    ]);
  });
});

describe('time labels', () => {
  it('write the clock within a day and a half, then the date, then the year', () => {
    expect(formatTick(T0, 15 * 60_000)).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(formatTick(T0, 2 * 3_600_000)).toMatch(/^\d{2}:\d{2}$/);
    expect(formatTick(T0, 7 * 86_400_000)).toMatch(/^\d{1,2}\/\d{1,2} \d{2}:\d{2}$/);
    expect(formatTick(T0, 90 * 86_400_000)).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(formatTipTime(T0, 3_600_000)).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(formatTipTime(T0, 7 * 86_400_000)).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });
});

describe('drawChart', () => {
  function fakeContext() {
    return {
      clearRect: vi.fn(),
      fillText: vi.fn(),
      measureText: vi.fn(() => ({ width: 30 })),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      stroke: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn(),
      save: vi.fn(),
      restore: vi.fn(),
    };
  }
  const colors = { text: '#666', grid: '#eee', surface: '#fff' };

  it('says so when there is nothing to draw', () => {
    const ctx = fakeContext();
    drawChart(
      ctx as unknown as CanvasRenderingContext2D,
      null,
      [],
      {},
      colors,
      null,
      { width: 720, height: 180 },
      'No samples',
    );
    expect(ctx.fillText).toHaveBeenCalledWith('No samples', 48, 28);
  });

  it('draws a line per series, a dot on its last point and the pointer’s column', () => {
    const ctx = fakeContext();
    const layout = computeLayout(SERIES, {}, FULL_WINDOW, 720, 180);
    const hover = hoverAt(SERIES, layout, (layout?.pad.l ?? 0) + 1);
    drawChart(
      ctx as unknown as CanvasRenderingContext2D,
      layout,
      SERIES,
      { format: (v) => `${v}%` },
      colors,
      hover,
      { width: 720, height: 180 },
      'No samples',
    );
    // Five grid labels, written with the chart's format, and three time labels.
    expect(ctx.fillText).toHaveBeenCalledTimes(8);
    expect(ctx.fillText).toHaveBeenCalledWith('110%', 4, expect.any(Number));
    // Series a is a line of 11 points; b's two points are a line too.
    expect(ctx.lineTo).toHaveBeenCalledTimes(5 + 10 + 1 + 1);
    // A last-point dot per series, and one per series under the pointer.
    expect(ctx.arc).toHaveBeenCalledTimes(4);
  });
});
