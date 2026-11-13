import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

/**
 * Dark mode repaints light surfaces, and a fill that stayed near-black on
 * those surfaces stops being a bar. This pins the Recent Requests volume ink
 * against the well it sits on.
 */

const css = readFileSync(resolve(__dirname, 'console-theme.css'), 'utf8');

function channel(hex: string, index: number): number {
  const value = Number.parseInt(hex.slice(1 + index * 2, 3 + index * 2), 16) / 255;
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const [r, g, b] = [0, 1, 2].map((index) => channel(hex, index));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG contrast ratio. Non-text UI needs 3:1. */
function contrast(a: string, b: string): number {
  const [lighter, darker] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (lighter + 0.05) / (darker + 0.05);
}

function variable(name: string): string {
  const match = css.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`));
  if (!match) throw new Error(`missing ${name}`);
  return match[1];
}

describe('request volume bars in dark mode', () => {
  it('lightens the neutral ink against the dark well', () => {
    expect(css).toMatch(/\.request-volume-ink\s*\{[^}]*background-color:\s*var\(--console-text\)/);
    // The well is bg-gray-50, which dark mode paints as the canvas.
    expect(css).toMatch(/\.bg-gray-50[\s\S]*?background-color:\s*var\(--console-canvas\)/);
    // Labeled buttons invert on their own rule and must not share this fill.
    expect(css).toMatch(/:is\(\.bg-black, \.bg-gray-900\)\.text-white/);
    expect(
      contrast(variable('--console-text'), variable('--console-canvas')),
    ).toBeGreaterThanOrEqual(3);
  });
});
