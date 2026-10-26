/** A mark that wants a label beside it, in screen pixels. */
export interface LabelRequest {
  key: string;
  x: number;
  y: number;
  /** Clearance from the mark's center to where the label may start. */
  r: number;
  /** Estimated label width. */
  width: number;
}

export interface PlacedLabel {
  key: string;
  x: number;
  y: number;
  anchor: 'start' | 'end' | 'middle';
}

/** A box is [left, top, right, bottom]. */
type Box = [number, number, number, number];

const LABEL_HEIGHT = 15;
const EDGE = 4;

/** Rough rendered width of a label at ~12.5px: names and digits differ a little. */
export function estimateLabelWidth(name: string, value: string): number {
  return name.length * 7 + value.length * 6.6 + 6;
}

/**
 * Greedy direct labeling: requests are placed in priority order, each trying
 * right, left, above and below its mark, and keeping the first slot that stays
 * in bounds and clear of every placed label and every mark. A label that fits
 * nowhere is dropped rather than drawn over something else — in a dense region
 * only the most important names survive.
 */
export function placeLabels(
  requests: readonly LabelRequest[],
  bounds: { width: number; height: number },
  marks: readonly { x: number; y: number; r: number }[] = [],
): PlacedLabel[] {
  const taken: Box[] = marks.map(({ x, y, r }) => [x - r, y - r, x + r, y + r]);
  const overlaps = (box: Box) =>
    taken.some(
      (other) => box[0] < other[2] && box[2] > other[0] && box[1] < other[3] && box[3] > other[1],
    );
  const placed: PlacedLabel[] = [];
  for (const request of requests) {
    const gap = request.r + 5;
    const { x, y, width } = request;
    const slots: { box: Box; label: PlacedLabel }[] = [
      {
        box: [x + gap, y - 8, x + gap + width, y - 8 + LABEL_HEIGHT],
        label: { key: request.key, x: x + gap, y: y + 4, anchor: 'start' },
      },
      {
        box: [x - gap - width, y - 8, x - gap, y - 8 + LABEL_HEIGHT],
        label: { key: request.key, x: x - gap, y: y + 4, anchor: 'end' },
      },
      {
        box: [x - width / 2, y - gap - LABEL_HEIGHT, x + width / 2, y - gap],
        label: { key: request.key, x, y: y - gap - 3, anchor: 'middle' },
      },
      {
        box: [x - width / 2, y + gap, x + width / 2, y + gap + LABEL_HEIGHT],
        label: { key: request.key, x, y: y + gap + 11, anchor: 'middle' },
      },
    ];
    const slot = slots.find(
      ({ box }) =>
        box[0] >= EDGE &&
        box[1] >= EDGE &&
        box[2] <= bounds.width - EDGE &&
        box[3] <= bounds.height - EDGE &&
        !overlaps(box),
    );
    if (!slot) continue;
    taken.push(slot.box);
    placed.push(slot.label);
  }
  return placed;
}
