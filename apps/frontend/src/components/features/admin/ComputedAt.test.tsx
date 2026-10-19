// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ComputedAt, formatComputedAgo } from './ComputedAt';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

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

it('keeps the age current while the page stays open', () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-19T12:00:00Z'));
  render(<ComputedAt at="2026-10-19T12:00:00Z" />);
  expect(screen.getByText('Computed just now')).toBeInTheDocument();

  act(() => {
    vi.advanceTimersByTime(6 * 60 * 1000);
  });
  expect(screen.getByText('Computed 6m ago')).toBeInTheDocument();
});

it('says when the server is computing new numbers', () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-19T12:00:00Z'));
  const { rerender } = render(<ComputedAt at={null} refreshing />);
  expect(screen.getByText('Computing…')).toBeInTheDocument();
  rerender(<ComputedAt at="2026-10-19T11:57:00Z" refreshing />);
  expect(screen.getByText('Computed 3m ago · refreshing…')).toBeInTheDocument();
});
