// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

let pathname = '/dashboard';
let agentsEnabled = false;
let agentsUrl = '';
let role = 'internal';

vi.mock('next/navigation', () => ({
  usePathname: () => pathname,
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: 'internal-user', role } } }),
}));

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useSiteConfig: () => ({ features: { agents: agentsEnabled }, agentsUrl }),
}));

let batchEntry: boolean | null = false;
let batchError: string | null = null;
vi.mock('@/lib/hooks/queries/useBatchCapabilities', () => ({
  useBatchCapabilities: () => ({
    capabilities: batchEntry === null ? null : { show_entry: batchEntry },
    loading: false,
    error: batchError,
  }),
}));

vi.mock('@/components/ui/revealHorizontally', () => ({ revealHorizontally: vi.fn() }));

import { revealHorizontally } from '@/components/ui/revealHorizontally';
import { ConsoleNav, isCurrent } from './ConsoleNav';

function reset() {
  pathname = '/dashboard';
  agentsEnabled = false;
  agentsUrl = '';
  role = 'internal';
  batchEntry = false;
  batchError = null;
}

function adminLinks(): string[] {
  const list = screen.queryByRole('list', { name: 'Admin' });
  if (!list) return [];
  return within(list)
    .getAllByRole('link')
    .map((link) => link.textContent?.trim() ?? '');
}

describe('ConsoleNav pages', () => {
  afterEach(() => cleanup());
  beforeEach(reset);

  it('links every console page and marks the one being shown', () => {
    pathname = '/dashboard/keys';

    render(<ConsoleNav />);

    for (const [name, href] of [
      ['Home', '/dashboard'],
      ['Chat', '/dashboard/playground'],
      ['API Keys', '/dashboard/keys'],
      ['Requests', '/dashboard/requests'],
      ['Models', '/dashboard/models'],
      ['Settings', '/dashboard/settings'],
    ]) {
      expect(screen.getByRole('link', { name })).toHaveAttribute('href', href);
    }
    expect(screen.getByRole('link', { name: 'API Keys' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Home' })).not.toHaveAttribute('aria-current');
  });

  it('matches Home exactly and other pages by prefix', () => {
    expect(isCurrent('/dashboard', '/dashboard')).toBe(true);
    expect(isCurrent('/dashboard/keys', '/dashboard')).toBe(false);
    expect(isCurrent('/dashboard/batches/batch_1', '/dashboard/batches')).toBe(true);
    expect(isCurrent('/dashboard/batchesx', '/dashboard/batches')).toBe(false);
  });
});

describe('ConsoleNav runtime agents gate', () => {
  afterEach(() => cleanup());
  beforeEach(reset);

  it('hides the Agents link when the server-only proxy pair is unavailable', () => {
    render(<ConsoleNav />);

    expect(screen.queryByRole('link', { name: 'Agents' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Chat' })).toBeInTheDocument();
  });

  it('shows the Agents link, naming this console account, when the feature is enabled', () => {
    agentsEnabled = true;
    agentsUrl = '/agents';

    render(<ConsoleNav />);

    expect(screen.getByRole('link', { name: 'Agents' })).toHaveAttribute(
      'href',
      '/agents?gateway_account=internal-user',
    );
  });

  it('links directly to a standalone agent site', () => {
    agentsEnabled = true;
    agentsUrl = 'https://agents.example.test/';

    render(<ConsoleNav />);

    expect(screen.getByRole('link', { name: 'Agents' })).toHaveAttribute(
      'href',
      'https://agents.example.test/?gateway_account=internal-user',
    );
  });

  it('keeps the standalone agent link restricted to internal users', () => {
    agentsEnabled = true;
    agentsUrl = 'https://agents.example.test/';
    role = 'free';

    render(<ConsoleNav />);

    expect(screen.queryByRole('link', { name: 'Agents' })).not.toBeInTheDocument();
  });
});

describe('ConsoleNav administration and feature access', () => {
  afterEach(() => cleanup());
  beforeEach(() => {
    reset();
    agentsEnabled = true;
    agentsUrl = '/agents';
  });

  it.each([
    ['free', false, [], [], false],
    ['free', true, [], [], true],
    ['pro', true, [], [], true],
    ['internal', false, ['Chat', 'Agents'], [], false],
    ['internal', true, ['Chat', 'Agents'], [], true],
    ['admin', true, ['Chat', 'Agents'], ['Admin Console', 'pgAdmin'], true],
    ['admin', false, ['Chat', 'Agents'], ['Admin Console', 'pgAdmin'], false],
  ])(
    'a %s user with the Batches entry %s sees only the entries each permits',
    (userRole, entry, visibleFeatures, visibleAdmin, batches) => {
      role = userRole;
      batchEntry = entry;

      render(<ConsoleNav />);

      expect(adminLinks()).toEqual(visibleAdmin);
      for (const name of ['Chat', 'Agents']) {
        expect(screen.queryAllByRole('link', { name })).toHaveLength(
          visibleFeatures.includes(name) ? 1 : 0,
        );
      }
      expect(screen.queryAllByRole('link', { name: 'Batches' })).toHaveLength(batches ? 1 : 0);
    },
  );

  it('opens pgAdmin in a new tab', () => {
    role = 'admin';

    render(<ConsoleNav />);

    const link = screen.getByRole('link', { name: 'pgAdmin' });
    expect(link).toHaveAttribute('href', '/pgadmin/');
    expect(link).toHaveAttribute('target', '_blank');
  });

  it('brings the current page back into view when an entry loads ahead of it', () => {
    // On a phone the row scrolls sideways. Batches arrives after its
    // capabilities do and pushes Settings along, so Settings is revealed again.
    pathname = '/dashboard/settings';
    batchEntry = null;
    const reveal = vi.mocked(revealHorizontally);
    reveal.mockClear();

    const { rerender } = render(<ConsoleNav />);
    const nav = screen.getByRole('navigation', { name: 'Console' });
    expect(reveal).toHaveBeenLastCalledWith(nav, screen.getByRole('link', { name: 'Settings' }));

    reveal.mockClear();
    batchEntry = true;
    rerender(<ConsoleNav />);

    expect(screen.getByRole('link', { name: 'Batches' })).toBeInTheDocument();
    expect(reveal).toHaveBeenCalledTimes(1);
    expect(reveal).toHaveBeenCalledWith(nav, screen.getByRole('link', { name: 'Settings' }));

    // A render that changes nothing leaves the reader's own scrolling alone.
    reveal.mockClear();
    rerender(<ConsoleNav />);
    expect(reveal).not.toHaveBeenCalled();
  });

  it('shows no Batches entry while the capabilities are unknown', () => {
    batchEntry = null;

    render(<ConsoleNav />);

    expect(screen.queryByRole('link', { name: /batches/i })).not.toBeInTheDocument();
  });

  it.each(['internal', 'admin'])(
    'keeps the legacy entry for %s when the first lookup fails',
    (userRole) => {
      role = userRole;
      batchEntry = null;
      batchError = 'offline';

      render(<ConsoleNav />);

      expect(screen.getByRole('link', { name: 'Batches' })).toBeInTheDocument();
    },
  );

  it('does not infer a free account has history when its first lookup fails', () => {
    role = 'free';
    batchEntry = null;
    batchError = 'offline';

    render(<ConsoleNav />);

    expect(screen.queryByRole('link', { name: 'Batches' })).not.toBeInTheDocument();
  });

  it('keeps a known disabled entry hidden through a failed refresh', () => {
    batchEntry = false;
    batchError = 'offline';

    render(<ConsoleNav />);

    expect(screen.queryByRole('link', { name: 'Batches' })).not.toBeInTheDocument();
  });

  it('keeps Agents behind its feature flag for internal users', () => {
    agentsEnabled = false;
    batchEntry = true;

    render(<ConsoleNav />);

    expect(screen.queryByRole('link', { name: 'Agents' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Chat' })).toBeInTheDocument();
    expect(adminLinks()).toEqual([]);
  });

  it.each(['free', 'pro', 'internal'])('shows no Admin group for a %s account', (userRole) => {
    role = userRole;

    render(<ConsoleNav />);

    expect(screen.queryByRole('list', { name: 'Admin' })).not.toBeInTheDocument();
  });
});
