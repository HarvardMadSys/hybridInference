// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AdminNotice, AdminSection, AdminSubTabs } from './AdminPrimitives';

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
        title="Send History"
        description="Every broadcast."
        actions={<button>Go</button>}
      >
        <p>Body</p>
      </AdminSection>,
    );

    const region = screen.getByRole('region', { name: 'Send History' });
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
