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

import { ConsoleNav, isCurrent } from './ConsoleNav';

function reset() {
  pathname = '/dashboard';
  agentsEnabled = false;
  agentsUrl = '';
  role = 'internal';
  batchEntry = false;
  batchError = null;
}

function tools(): string[] {
  const list = screen.queryByRole('list', { name: 'Tools' });
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
      ['Overview', '/dashboard'],
      ['API Keys', '/dashboard/keys'],
      ['Requests', '/dashboard/requests'],
      ['Models', '/dashboard/models'],
      ['Settings', '/dashboard/settings'],
    ]) {
      expect(screen.getByRole('link', { name })).toHaveAttribute('href', href);
    }
    expect(screen.getByRole('link', { name: 'API Keys' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Overview' })).not.toHaveAttribute('aria-current');
  });

  it('matches the overview exactly and other pages by prefix', () => {
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
    expect(screen.getByRole('link', { name: /api playground/i })).toBeInTheDocument();
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

describe('ConsoleNav tools and Batches', () => {
  afterEach(() => cleanup());
  beforeEach(() => {
    reset();
    agentsEnabled = true;
    agentsUrl = '/agents';
  });

  it.each([
    ['free', false, [], false],
    ['free', true, [], true],
    ['pro', true, [], true],
    ['internal', false, ['Agents', 'API Playground'], false],
    ['internal', true, ['Agents', 'API Playground'], true],
    ['admin', true, ['Agents', 'API Playground', 'Admin Console', 'pgAdmin'], true],
    ['admin', false, ['Agents', 'API Playground', 'Admin Console', 'pgAdmin'], false],
  ])(
    'a %s user with the Batches entry %s sees only the tools each permits',
    (userRole, entry, visibleTools, batches) => {
      role = userRole;
      batchEntry = entry;

      render(<ConsoleNav />);

      expect(tools()).toEqual(visibleTools);
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

    expect(tools()).toEqual(['API Playground']);
  });

  it('shows no tools group for an account with no tools', () => {
    role = 'free';

    render(<ConsoleNav />);

    expect(screen.queryByRole('list', { name: 'Tools' })).not.toBeInTheDocument();
  });
});
