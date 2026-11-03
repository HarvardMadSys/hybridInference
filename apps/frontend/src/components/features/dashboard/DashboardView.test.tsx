// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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

vi.mock('@/components/features/dashboard/UsageStats', () => ({ UsageStats: () => null }));
vi.mock('@/components/ui/UpdatesBanner', () => ({ UpdatesBanner: () => null }));

import { DashboardView } from './DashboardView';

describe('DashboardView account identity', () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    role = 'admin';
  });

  it('greets an account without an email by its login name', () => {
    identity = { email: null, login_name: 'admin', user_name: null };

    render(<DashboardView />);

    expect(screen.getByText('Welcome back, admin')).toBeInTheDocument();
    expect(screen.getByText('Nickname:').nextElementSibling).toHaveTextContent('Not set');
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

describe('DashboardView overview', () => {
  afterEach(() => cleanup());

  it('leaves keys, requests, models and tools to their own pages', () => {
    role = 'admin';
    identity = { email: 'ops@example.test', user_name: 'Ops' };

    render(<DashboardView />);

    expect(screen.queryByRole('heading', { name: 'Tools' })).not.toBeInTheDocument();
    expect(screen.queryAllByRole('link')).toHaveLength(0);
  });
});
