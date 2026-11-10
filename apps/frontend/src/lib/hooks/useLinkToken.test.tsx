// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The search parameters Next hands the page: the address as it is now, so
// taking the token out of it changes them, as Next's router does.
vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

import { useLinkToken } from './useLinkToken';

let seen: (string | null)[];

function Probe() {
  seen.push(useLinkToken());
  return null;
}

beforeEach(() => {
  seen = [];
});

afterEach(() => {
  cleanup();
  window.history.replaceState(null, '', '/');
});

describe('useLinkToken', () => {
  it('reads the token, then takes it out of the address and this history entry', () => {
    window.history.replaceState(null, '', '/reset-password?token=live-token&from=email#form');
    const length = window.history.length;

    render(<Probe />);

    expect(seen[0]).toBe('live-token');
    expect(window.location.pathname).toBe('/reset-password');
    expect(window.location.search).toBe('?from=email');
    expect(window.location.hash).toBe('#form');
    expect(window.history.length).toBe(length);
  });

  // The address no longer has it, and the page must not decide the link had none.
  it('keeps the token once the address has lost it', () => {
    window.history.replaceState(null, '', '/verify-email?token=live-token');
    const { rerender } = render(<Probe />);

    rerender(<Probe />);

    expect(window.location.search).toBe('');
    expect(seen.at(-1)).toBe('live-token');
  });

  it('is null for an address without one, and leaves the address alone', () => {
    window.history.replaceState(null, '', '/reset-password?from=email');

    render(<Probe />);

    expect(seen[0]).toBeNull();
    expect(window.location.search).toBe('?from=email');
  });
});
