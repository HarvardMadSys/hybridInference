// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import type { Project } from '@/lib/api/user';
import { ProjectDialogs, projectHasLimits, projectSummary } from './ProjectDialogs';

// A CNY ledger: the project budgets and spend come native, beside USD
// equivalents that must never be shown as yuan.
const state = vi.hoisted(() => ({ create: vi.fn(), update: vi.fn(), archive: vi.fn() }));
vi.mock('@/lib/hooks', () => ({
  useModels: () => ({ data: { object: 'list', data: [] }, isLoading: false }),
  useCreateProject: () => ({ mutateAsync: state.create, isPending: false }),
  useUpdateProject: () => ({ mutateAsync: state.update, isPending: false }),
  useArchiveProject: () => ({ mutateAsync: state.archive, isPending: false }),
}));
vi.mock('@/lib/hooks/useAccountToast', () => ({
  useAccountToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
vi.mock('@/lib/api/admin', () => ({ getUserProjects: vi.fn() }));

import { getUserProjects } from '@/lib/api/admin';
import { UserProjects } from '@/app/dashboard/admin/users/UserProjects';

const RESEARCH = {
  id: 'proj_research',
  name: 'Research',
  description: null,
  is_default: false,
  currency: 'CNY',
  budget_daily_usd: 2.9771,
  budget_daily: 20,
  budget_monthly_usd: null,
  budget_monthly: null,
  spent_today_usd: 1.5,
  spent_today: 10.077,
  spent_month_usd: 1.5,
  spent_month: 10.077,
  allowed_models: null,
  max_concurrent_requests: null,
  key_count: 1,
  created_at: '2026-11-01T00:00:00Z',
} as unknown as Project;

const cnyConfig = { ...buildTimeSiteConfig, accounting: { currency: 'CNY' as const } };

function renderCny(node: JSX.Element): void {
  render(<SiteConfigProvider initialConfig={cnyConfig}>{node}</SiteConfigProvider>);
}

beforeEach(() => {
  state.create.mockReset().mockResolvedValue({});
  state.update.mockReset().mockResolvedValue({});
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('projects on a CNY ledger', () => {
  it('summarizes the native budget and spend, not the USD equivalents', () => {
    const t = (_slot: string, english: string) => english;
    const summary = projectSummary(RESEARCH, t);
    expect(summary).toBe('¥10.077 of ¥20.00 today · ¥10.077 this month');
    expect(summary).not.toMatch(/\$|2\.97/);
    expect(projectHasLimits({ ...RESEARCH, budget_daily: null } as Project)).toBe(false);
  });

  it('edits the native budget and sends it with its currency', async () => {
    renderCny(<ProjectDialogs dialog={{ mode: 'edit', project: RESEARCH }} onClose={vi.fn()} />);
    const dialog = screen.getByRole('dialog');
    const daily = within(dialog).getByLabelText('Daily budget (CNY)');
    expect(daily).toHaveValue('20');
    fireEvent.change(daily, { target: { value: '35.5' } });
    fireEvent.click(within(dialog).getByRole('button', { name: /Save/ }));
    await waitFor(() => expect(state.update).toHaveBeenCalled());
    const { changes } = state.update.mock.calls[0][0];
    expect(changes).toMatchObject({ budget_daily: 35.5, budget_monthly: null, currency: 'CNY' });
    expect(changes).not.toHaveProperty('budget_daily_usd');
    expect(changes).not.toHaveProperty('budget_monthly_usd');
  });

  it('creates a project with a native budget', async () => {
    renderCny(<ProjectDialogs dialog={{ mode: 'create' }} onClose={vi.fn()} />);
    const dialog = screen.getByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Evals' } });
    fireEvent.change(within(dialog).getByLabelText('Monthly budget (CNY)'), {
      target: { value: '300' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create Project' }));
    await waitFor(() =>
      expect(state.create).toHaveBeenCalledWith({
        name: 'Evals',
        budget_daily: null,
        budget_monthly: 300,
        currency: 'CNY',
        max_concurrent_requests: null,
        allowed_models: null,
      }),
    );
  });

  it("shows an administrator the account's native project and key amounts", async () => {
    vi.mocked(getUserProjects).mockResolvedValue({
      projects: [RESEARCH],
      keys: [
        {
          key_prefix: 'hyi-aaaa',
          key_masked: 'hyi-aaaa****',
          name: 'runner',
          created_at: '2026-11-01T00:00:00Z',
          status: 'active',
          project_id: 'proj_research',
          currency: 'CNY',
          spent_today_usd: 0.5,
          spent_today: 3.359,
          budget_daily_usd: 1.4886,
          budget_daily: 10,
        },
      ],
    } as never);
    renderCny(<UserProjects userId="u-1" />);
    const research = await screen.findByRole('region', { name: 'Research' });
    expect(within(research).getByText(/¥10\.08 of ¥20\.00 today/)).toBeInTheDocument();
    expect(within(research).getByText(/¥3\.36 today · daily ¥10\.00/)).toBeInTheDocument();
    expect(research).not.toHaveTextContent(/\$|2\.97|1\.48/);
  });
});
