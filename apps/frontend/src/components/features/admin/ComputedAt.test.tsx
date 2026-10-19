// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { ComputedAt, formatComputedAgo } from './ComputedAt';

afterEach(cleanup);

const NOW = Date.parse('2026-10-19T12:00:00Z');

it('says how long ago a cached view was computed', () => {
  expect(formatComputedAgo('2026-10-19T11:59:40Z', NOW)).toBe('just now');
  expect(formatComputedAgo('2026-10-19T11:57:00Z', NOW)).toBe('3m ago');
  expect(formatComputedAgo('2026-10-19T09:00:00Z', NOW)).toBe('3h ago');
  expect(formatComputedAgo('2026-10-17T12:00:00Z', NOW)).toBe('2d ago');
  // A server clock slightly ahead never reads as the future.
  expect(formatComputedAgo('2026-10-19T12:00:05Z', NOW)).toBe('just now');
});

it('shows the exact UTC time on hover, and nothing before the first result', () => {
  const { rerender } = render(<ComputedAt at={null} />);
  expect(screen.queryByText(/Computed/)).not.toBeInTheDocument();
  rerender(<ComputedAt at="2026-10-19T11:57:00Z" />);
  expect(screen.getByText(/^Computed /)).toHaveAttribute(
    'title',
    'Computed at 2026-10-19 11:57:00 UTC',
  );
});
