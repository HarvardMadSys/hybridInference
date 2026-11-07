import { describe, expect, it } from 'vitest';
import { estimateLabelWidth, placeLabels } from './globeLabels';

const BOUNDS = { width: 400, height: 300 };

describe('placeLabels', () => {
  it('puts a label to the right of its mark when there is room', () => {
    expect(placeLabels([{ key: 'USA', x: 100, y: 100, r: 10, width: 80 }], BOUNDS)).toEqual([
      { key: 'USA', x: 115, y: 104, anchor: 'start' },
    ]);
  });

  it('flips to the left at the right edge', () => {
    const [label] = placeLabels([{ key: 'JPN', x: 380, y: 100, r: 6, width: 80 }], BOUNDS);
    expect(label).toMatchObject({ anchor: 'end', x: 369 });
  });

  it('never covers another mark', () => {
    const marks = [
      { x: 100, y: 100, r: 10 },
      { x: 150, y: 100, r: 12 },
    ];
    const [label] = placeLabels([{ key: 'GBR', x: 100, y: 100, r: 10, width: 80 }], BOUNDS, marks);
    expect(label.anchor).not.toBe('start');
  });

  it('keeps the first-come label and drops one with nowhere left to go', () => {
    const crowd = [
      { key: 'DEU', x: 200, y: 150, r: 4, width: 70 },
      { key: 'FRA', x: 200, y: 150, r: 4, width: 70 },
      { key: 'NLD', x: 200, y: 150, r: 4, width: 70 },
      { key: 'BEL', x: 200, y: 150, r: 4, width: 70 },
      { key: 'LUX', x: 200, y: 150, r: 4, width: 70 },
    ];
    const placed = placeLabels(crowd, BOUNDS);
    expect(placed.map((label) => label.key)).toEqual(['DEU', 'FRA', 'NLD', 'BEL']);
  });

  it('estimates a longer name as wider', () => {
    expect(estimateLabelWidth('United Kingdom', '3.4k')).toBeGreaterThan(
      estimateLabelWidth('Japan', '3.4k'),
    );
  });
});
