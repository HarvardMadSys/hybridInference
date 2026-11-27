// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LEAVE_MESSAGE, useLeaveGuard } from './useLeaveGuard';

const followed = vi.fn();

function Page({ unsaved }: { unsaved: number }) {
  useLeaveGuard(unsaved);
  // The console's links act on the click in React, after the guard has had it.
  const follow = (event: React.MouseEvent) => {
    event.preventDefault();
    followed();
  };
  return (
    <>
      <a href="/dashboard/admin/routing" onClick={follow}>
        Routing
      </a>
      <a href="/dashboard/admin/providers/models?tab=x" onClick={follow}>
        Same page
      </a>
      <a href="/dashboard/admin/routing" target="_blank" rel="noreferrer">
        New tab
      </a>
      <a href="https://docs.example.test/admin">Elsewhere</a>
    </>
  );
}

const confirm = vi.spyOn(window, 'confirm');

beforeEach(() => {
  window.history.replaceState(null, '', '/dashboard/admin/providers/models');
  confirm.mockReset();
  followed.mockReset();
});

afterEach(() => {
  cleanup();
});

const unload = () => {
  const event = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
};

describe('useLeaveGuard', () => {
  it('lets a page with nothing unsaved go without asking', () => {
    render(<Page unsaved={0} />);

    fireEvent.click(screen.getByText('Routing'));

    expect(confirm).not.toHaveBeenCalled();
    expect(followed).toHaveBeenCalledTimes(1);
    expect(unload()).toBe(false);
  });

  it('asks before another page of the console, and stays when told to', () => {
    confirm.mockReturnValue(false);
    render(<Page unsaved={2} />);

    expect(fireEvent.click(screen.getByText('Routing'))).toBe(false);

    expect(confirm).toHaveBeenCalledWith(LEAVE_MESSAGE);
    expect(followed).not.toHaveBeenCalled();
  });

  it('follows the link once the administrator confirms', () => {
    confirm.mockReturnValue(true);
    render(<Page unsaved={1} />);

    fireEvent.click(screen.getByText('Routing'));

    expect(confirm).toHaveBeenCalledTimes(1);
    expect(followed).toHaveBeenCalledTimes(1);
  });

  it('does not ask about a link that leaves the page standing', () => {
    render(<Page unsaved={1} />);

    fireEvent.click(screen.getByText('Same page'));
    fireEvent.click(screen.getByText('New tab'));
    fireEvent.click(screen.getByText('Routing'), { ctrlKey: true });
    fireEvent.click(screen.getByText('Elsewhere'));

    expect(confirm).not.toHaveBeenCalled();
  });

  it('has the browser warn before a reload or close, and stops once it is saved', () => {
    const { rerender } = render(<Page unsaved={1} />);
    expect(unload()).toBe(true);

    rerender(<Page unsaved={0} />);
    expect(unload()).toBe(false);
    fireEvent.click(screen.getByText('Routing'));
    expect(confirm).not.toHaveBeenCalled();
  });
});
