// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/admin', () => ({ getUserProjects: vi.fn() }));

import { getUserProjects } from '@/lib/api/admin';
import { UserProjects } from './UserProjects';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const base = {
  description: null,
  budget_monthly_usd: null,
  allowed_models: null,
  max_concurrent_requests: null,
  spent_month_usd: 0,
  created_at: '2026-11-01T00:00:00Z',
};
const key = {
  key_masked: 'hyi-aaaa****',
  created_at: '2026-11-01T00:00:00Z',
  status: 'active',
  spent_today_usd: 0.5,
};

it("lists the account's projects with their keys, and keys of archived projects apart", async () => {
  vi.mocked(getUserProjects).mockResolvedValue({
    projects: [
      {
        ...base,
        id: 'proj_d',
        name: 'Default',
        is_default: true,
        budget_daily_usd: null,
        spent_today_usd: 0,
        key_count: 0,
      },
      {
        ...base,
        id: 'proj_ci',
        name: 'CI',
        is_default: false,
        budget_daily_usd: 5,
        max_concurrent_requests: 2,
        spent_today_usd: 1,
        key_count: 1,
      },
    ],
    keys: [
      { ...key, key_prefix: 'hyi-aaaa', name: 'runner', project_id: 'proj_ci' },
      { ...key, key_prefix: 'hyi-bbbb', name: 'old', status: 'revoked', project_id: 'proj_gone' },
    ],
  });

  render(<UserProjects userId="user-1" />);

  const ci = await screen.findByRole('region', { name: 'CI' });
  expect(within(ci).getByText(/\$1\.00 of \$5\.00 today/)).toBeInTheDocument();
  expect(within(ci).getByText(/up to 2 at once/)).toBeInTheDocument();
  expect(within(ci).getByText('runner')).toBeInTheDocument();
  expect(
    within(screen.getByRole('region', { name: 'Default' })).getByText('No keys.'),
  ).toBeInTheDocument();
  const archived = screen.getByRole('region', { name: 'Archived projects' });
  expect(within(archived).getByText('old')).toBeInTheDocument();
  expect(within(archived).getByText('revoked')).toBeInTheDocument();
  expect(getUserProjects).toHaveBeenCalledWith('user-1');
});

it('says why the projects could not be loaded', async () => {
  vi.mocked(getUserProjects).mockRejectedValue(new Error('Database not configured'));

  render(<UserProjects userId="user-1" />);

  expect(await screen.findByText('Database not configured')).toBeInTheDocument();
});
