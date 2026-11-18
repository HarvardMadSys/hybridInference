// @vitest-environment jsdom
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { usePageRestored } from './usePageRestored';

afterEach(cleanup);

function show(persisted: boolean) {
  act(() => {
    window.dispatchEvent(Object.assign(new Event('pageshow'), { persisted }));
  });
}

function Page({ restore }: { restore: () => void }) {
  usePageRestored(restore);
  return null;
}

describe('usePageRestored', () => {
  it('runs only when the page comes back from the back-forward cache', () => {
    const restore = vi.fn();
    render(<Page restore={restore} />);

    // An ordinary load shows the page too, with nothing to restore.
    show(false);
    expect(restore).not.toHaveBeenCalled();

    show(true);
    expect(restore).toHaveBeenCalledTimes(1);
  });

  it('runs the callback of the latest render, and stops with the page', () => {
    const first = vi.fn();
    const latest = vi.fn();
    const { rerender, unmount } = render(<Page restore={first} />);
    rerender(<Page restore={latest} />);

    show(true);
    expect(first).not.toHaveBeenCalled();
    expect(latest).toHaveBeenCalledTimes(1);

    unmount();
    show(true);
    expect(latest).toHaveBeenCalledTimes(1);
  });
});
