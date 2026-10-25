// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AdminNotice, AdminSection, AdminSectionNav, AdminSubTabs } from './AdminPrimitives';

afterEach(cleanup);

const TABS = [
  { key: 'updates', label: 'Updates', tabId: 'updates-tab', panelId: 'updates-panel' },
  { key: 'email', label: 'Email', tabId: 'email-tab', panelId: 'email-panel' },
] as const;

describe('AdminSubTabs', () => {
  it('marks the active tab and names the panel each tab controls', () => {
    render(<AdminSubTabs label="Channels" tabs={TABS} active="email" />);

    expect(screen.getByRole('tablist', { name: 'Channels' })).toBeInTheDocument();
    const email = screen.getByRole('tab', { name: 'Email' });
    expect(email).toHaveAttribute('aria-selected', 'true');
    expect(email).toHaveAttribute('aria-controls', 'email-panel');
    expect(email).toHaveAttribute('id', 'email-tab');
    expect(screen.getByRole('tab', { name: 'Updates' })).toHaveAttribute('aria-selected', 'false');
  });

  it('reports the tab that was chosen', () => {
    const onSelect = vi.fn();
    render(<AdminSubTabs label="Channels" tabs={TABS} active="updates" onSelect={onSelect} />);

    fireEvent.click(screen.getByRole('tab', { name: 'Email' }));
    expect(onSelect).toHaveBeenCalledWith('email');
  });
});

describe('AdminSection', () => {
  it('is a region named by its title, with its description and actions', () => {
    render(
      <AdminSection
        id="history"
        title="Send History"
        description="Every broadcast."
        actions={<button>Go</button>}
      >
        <p>Body</p>
      </AdminSection>,
    );

    const region = screen.getByRole('region', { name: 'Send History' });
    expect(region).toHaveAttribute('id', 'history');
    expect(region).toHaveTextContent('Every broadcast.');
    expect(region).toHaveTextContent('Body');
    expect(screen.getByRole('button', { name: 'Go' })).toBeInTheDocument();
  });
});

describe('AdminNotice', () => {
  it('stays quiet unless given a role, and offers a labelled dismiss', () => {
    const onDismiss = vi.fn();
    const { rerender } = render(<AdminNotice onDismiss={onDismiss}>Could not load.</AdminNotice>);

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(onDismiss).toHaveBeenCalledTimes(1);

    rerender(<AdminNotice role="alert">Could not load.</AdminNotice>);
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load.');
    expect(screen.queryByRole('button', { name: 'Dismiss' })).not.toBeInTheDocument();
  });
});

describe('AdminSectionNav', () => {
  const items = [
    { id: 'general', label: 'General' },
    { id: 'email', label: 'Email' },
    { id: 'network', label: 'Network' },
  ];

  function Page({ list = items }: { list?: typeof items }) {
    return (
      <>
        <AdminSectionNav label="Categories" items={list} />
        {list.map((item) => (
          <section key={item.id} id={item.id} aria-label={item.label}>
            {item.label}
          </section>
        ))}
      </>
    );
  }

  let scrollIntoView: ReturnType<typeof vi.fn>;
  let reducedMotion = false;

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
    scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView as unknown as Element['scrollIntoView'];
    reducedMotion = false;
    vi.stubGlobal(
      'matchMedia',
      vi.fn(() => ({ matches: reducedMotion })),
    );
    window.history.replaceState(null, '', '/settings');
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    // @ts-expect-error jsdom has no scrollIntoView of its own
    delete Element.prototype.scrollIntoView;
  });

  it('links to each section and marks the first one to start with', () => {
    render(<AdminSectionNav label="Categories" items={items} />);

    expect(screen.getByRole('navigation', { name: 'Categories' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Email' })).toHaveAttribute('href', '#email');
    expect(screen.getByRole('link', { name: 'General' })).toHaveAttribute(
      'aria-current',
      'location',
    );
  });

  it('glides to the chosen section, records the hash and moves focus there', () => {
    render(<Page />);

    fireEvent.click(screen.getByRole('link', { name: 'Network' }));

    const network = screen.getByRole('region', { name: 'Network' });
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' });
    expect(scrollIntoView.mock.contexts[0]).toBe(network);
    expect(window.location.hash).toBe('#network');
    expect(network).toHaveFocus();
    expect(screen.getByRole('link', { name: 'Network' })).toHaveAttribute(
      'aria-current',
      'location',
    );
    expect(screen.getByRole('link', { name: 'General' })).not.toHaveAttribute('aria-current');
  });

  it('jumps without animation for readers who prefer less motion', () => {
    reducedMotion = true;
    render(<Page />);

    fireEvent.click(screen.getByRole('link', { name: 'Email' }));

    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto', block: 'start' });
  });

  it('keeps the marker on the target while the page scrolls past other sections', async () => {
    const nextFrame = () =>
      act(() => new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve())));
    render(<Page />);
    fireEvent.click(screen.getByRole('link', { name: 'Email' }));

    // jsdom lays nothing out, so every section reports a top above the
    // reading line: once tracking resumes, the last one is "being read".
    fireEvent.scroll(window);
    await nextFrame();
    expect(screen.getByRole('link', { name: 'Email' })).toHaveAttribute('aria-current', 'location');

    act(() => {
      vi.advanceTimersByTime(1000);
    });
    fireEvent.scroll(window);
    await nextFrame();
    expect(screen.getByRole('link', { name: 'Network' })).toHaveAttribute(
      'aria-current',
      'location',
    );
  });

  it('marks a section on screen at the end of the page, preferring the chosen one', async () => {
    const nextFrame = () =>
      act(() => new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve())));
    render(<Page />);
    // Scrolled to the very end: General is above the window, Email crosses
    // the reading line, Network is too short to ever reach it.
    const tops: Record<string, number> = { general: -900, email: 100, network: 400 };
    for (const [id, top] of Object.entries(tops)) {
      document.getElementById(id)!.getBoundingClientRect = () =>
        ({ top, bottom: top + 200, left: 0, right: 0, width: 0, height: 200 }) as DOMRect;
    }
    vi.stubGlobal('innerHeight', 800);
    vi.stubGlobal('scrollY', 1000);
    Object.defineProperty(document.documentElement, 'scrollHeight', {
      value: 1800,
      configurable: true,
    });

    fireEvent.scroll(window);
    await nextFrame();
    expect(screen.getByRole('link', { name: 'Network' })).toHaveAttribute(
      'aria-current',
      'location',
    );

    fireEvent.click(screen.getByRole('link', { name: 'Email' }));
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    fireEvent.scroll(window);
    await nextFrame();
    expect(screen.getByRole('link', { name: 'Email' })).toHaveAttribute('aria-current', 'location');

    // @ts-expect-error restore jsdom's own value
    delete document.documentElement.scrollHeight;
  });

  it('leaves a modified click (new tab, new window) to the browser', () => {
    render(<Page />);

    const link = screen.getByRole('link', { name: 'Email' });
    const notPrevented = fireEvent.click(link, { ctrlKey: true });

    expect(notPrevented).toBe(true);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it('opens at a section named in the address once it exists', () => {
    window.history.replaceState(null, '', '/settings#email');
    const { rerender } = render(<Page list={[]} />);
    expect(scrollIntoView).not.toHaveBeenCalled();

    rerender(<Page />);

    expect(scrollIntoView.mock.contexts[0]).toBe(screen.getByRole('region', { name: 'Email' }));
    expect(screen.getByRole('link', { name: 'Email' })).toHaveAttribute('aria-current', 'location');
  });

  it('follows Back and Forward between sections', () => {
    render(<Page />);
    fireEvent.click(screen.getByRole('link', { name: 'Network' }));
    expect(window.location.hash).toBe('#network');

    // Back to the entry before the jump, then Forward again.
    window.history.replaceState(null, '', '/settings#email');
    act(() => {
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    expect(screen.getByRole('link', { name: 'Email' })).toHaveAttribute('aria-current', 'location');

    window.history.replaceState(null, '', '/settings#network');
    act(() => {
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(screen.getByRole('link', { name: 'Network' })).toHaveAttribute(
      'aria-current',
      'location',
    );
  });

  it('ignores a malformed address hash instead of failing', () => {
    window.history.replaceState(null, '', '/settings#%E0%A4%A');

    expect(() => render(<Page />)).not.toThrow();
    expect(screen.getByRole('navigation', { name: 'Categories' })).toBeInTheDocument();
  });

  it('marks the first listed section when a filter removes the marked one', () => {
    const { rerender } = render(<Page />);
    fireEvent.click(screen.getByRole('link', { name: 'Network' }));

    rerender(<Page list={[items[1]]} />);

    expect(screen.getByRole('link', { name: 'Email' })).toHaveAttribute('aria-current', 'location');
  });
});
