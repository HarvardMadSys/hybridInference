// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AdminTabNav, revealHorizontally } from './AdminTabNav';

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/settings',
}));

describe('AdminTabNav', () => {
  afterEach(() => {
    cleanup();
  });

  it('shows routing as a top-level admin tab', () => {
    render(<AdminTabNav />);

    expect(screen.getByRole('link', { name: 'Providers' })).toHaveAttribute(
      'href',
      '/dashboard/admin/providers',
    );
    expect(screen.getByRole('link', { name: 'Routing' })).toHaveAttribute(
      'href',
      '/dashboard/admin/routing',
    );
    expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute(
      'href',
      '/dashboard/admin/settings',
    );
  });

  it('keeps Token Usage under Analytics, not as a tab of its own', () => {
    render(<AdminTabNav />);

    expect(screen.getByRole('link', { name: 'Analytics' })).toHaveAttribute(
      'href',
      '/dashboard/admin/analytics',
    );
    expect(screen.queryByRole('link', { name: 'Token Usage' })).not.toBeInTheDocument();
  });

  it('consolidates updates and broadcast email under an Announcements tab', () => {
    render(<AdminTabNav />);

    expect(screen.getByRole('link', { name: 'Announcements' })).toHaveAttribute(
      'href',
      '/dashboard/admin/announcements',
    );
    expect(screen.queryByRole('link', { name: 'Updates' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Broadcast Email' })).not.toBeInTheDocument();
  });

  it('lists the database-backed Configuration tab beside Settings', () => {
    render(<AdminTabNav />);

    expect(screen.getByRole('link', { name: 'Configuration' })).toHaveAttribute(
      'href',
      '/dashboard/admin/configuration',
    );
  });

  it('renames the audit tab to a top-level Log tab', () => {
    render(<AdminTabNav />);

    expect(screen.getByRole('link', { name: 'Log' })).toHaveAttribute(
      'href',
      '/dashboard/admin/log',
    );
    expect(screen.queryByRole('link', { name: 'Audit Log' })).not.toBeInTheDocument();
  });
});

describe('revealHorizontally', () => {
  function box(left: number, width: number) {
    return { left, right: left + width, width, top: 0, bottom: 20, height: 20 } as DOMRect;
  }

  function scrollerWith(scrollLeft: number) {
    const scroller = document.createElement('nav');
    Object.defineProperty(scroller, 'clientWidth', { value: 300 });
    scroller.scrollLeft = scrollLeft;
    scroller.getBoundingClientRect = () => box(0, 300);
    return scroller;
  }

  it('scrolls the row, not the page, until the tab is visible', () => {
    const scrollTo = vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
    const scroller = scrollerWith(0);
    const item = document.createElement('a');
    // 700px into the row, off the right edge of a 300px-wide scroller.
    item.getBoundingClientRect = () => box(700, 80);

    revealHorizontally(scroller, item);

    expect(scroller.scrollLeft).toBe(700 + 80 - 300 + 16);
    expect(scrollTo).not.toHaveBeenCalled();
    scrollTo.mockRestore();
  });

  it('scrolls back for a tab left of the visible part and leaves a visible one alone', () => {
    const scroller = scrollerWith(400);
    const item = document.createElement('a');
    // On screen at -350, i.e. 50px into the row.
    item.getBoundingClientRect = () => box(-350, 60);
    revealHorizontally(scroller, item);
    expect(scroller.scrollLeft).toBe(34);

    const visible = scrollerWith(100);
    item.getBoundingClientRect = () => box(20, 60);
    revealHorizontally(visible, item);
    expect(visible.scrollLeft).toBe(100);
  });
});
