// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

let agentsEnabled = false;
let agentsUrl = '';
let role = 'internal';
let identity: { email: string | null; login_name?: string | null; user_name: string | null } = {
  email: 'internal@example.test',
  user_name: 'Internal User',
};

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: {
      user: {
        id: 'internal-user',
        ...identity,
        role,
      },
    },
  }),
}));

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useSiteConfig: () => ({
    branding: { docsUrl: '' },
    features: { agents: agentsEnabled },
    agentsUrl,
  }),
}));

vi.mock('@/components/features/dashboard/ApiKeyManager', () => ({ ApiKeyManager: () => null }));
vi.mock('@/components/features/dashboard/ModelsSection', () => ({ ModelsSection: () => null }));
vi.mock('@/components/features/dashboard/RecentRequests', () => ({ RecentRequests: () => null }));
vi.mock('@/components/features/dashboard/UsageStats', () => ({ UsageStats: () => null }));
vi.mock('@/components/ui/UpdatesBanner', () => ({ UpdatesBanner: () => null }));

import { DashboardView } from './DashboardView';

describe('DashboardView runtime agents gate', () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    agentsEnabled = false;
    agentsUrl = '';
    role = 'internal';
    identity = { email: 'internal@example.test', user_name: 'Internal User' };
  });

  it('hides the Agents link when the server-only proxy pair is unavailable', () => {
    render(<DashboardView />);

    expect(screen.queryByRole('link', { name: 'Agents' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /api playground/i })).toBeInTheDocument();
  });

  it('shows the Agents link when the runtime feature is enabled', () => {
    agentsEnabled = true;
    agentsUrl = '/agents';

    render(<DashboardView />);

    expect(screen.getByRole('link', { name: 'Agents' })).toHaveAttribute('href', '/agents');
  });

  it('links the tile directly to a standalone agent site', () => {
    agentsEnabled = true;
    agentsUrl = 'https://agents.example.test/';

    render(<DashboardView />);

    expect(screen.getByRole('link', { name: 'Agents' })).toHaveAttribute('href', agentsUrl);
  });

  it('keeps the standalone agent tile restricted to internal users', () => {
    agentsEnabled = true;
    agentsUrl = 'https://agents.example.test/';
    role = 'free';

    render(<DashboardView />);

    expect(screen.queryByRole('link', { name: 'Agents' })).not.toBeInTheDocument();
  });
});

describe('DashboardView account identity', () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    agentsEnabled = false;
    agentsUrl = '';
    role = 'admin';
  });

  it('greets an account without an email by its login name', () => {
    identity = { email: null, login_name: 'admin', user_name: null };

    render(<DashboardView />);

    expect(screen.getByText('Welcome back, admin')).toBeInTheDocument();
    expect(screen.getByText('Email:').nextElementSibling).toHaveTextContent('Not set');
    expect(screen.getByText('Login name:').nextElementSibling).toHaveTextContent('admin');
  });

  it('prefers the display name, and shows no login-name row for an email account', () => {
    identity = { email: 'ops@example.test', login_name: null, user_name: 'Ops' };

    render(<DashboardView />);

    expect(screen.getByText('Welcome back, Ops')).toBeInTheDocument();
    expect(screen.getByText('Email:').nextElementSibling).toHaveTextContent('ops@example.test');
    expect(screen.queryByText('Login name:')).not.toBeInTheDocument();
  });
});
