// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import toast from 'react-hot-toast';
import UsersTab from './index';
import {
  approveUser,
  getUserDetail,
  listModelVisibility,
  listUsers,
  updateUser,
  type AdminUser,
  type UserDetail,
} from '@/lib/api/admin';

vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));
vi.mock('next/navigation', () => {
  const search = new URLSearchParams();
  const router = { replace: vi.fn() };
  return {
    useSearchParams: () => search,
    useRouter: () => router,
    usePathname: () => '/dashboard/admin/users',
  };
});
vi.mock('@/lib/api/admin', () => ({
  listUsers: vi.fn(),
  getBulkUsageTotals: vi.fn(),
  getUserDetail: vi.fn(),
  listModelVisibility: vi.fn(),
  updateUser: vi.fn(),
  approveUser: vi.fn(),
  rejectUser: vi.fn(),
  deleteUser: vi.fn(),
  resumeUser: vi.fn(),
  hardDeleteUser: vi.fn(),
}));
vi.mock('./SummaryCards', () => ({ SummaryCards: () => null }));
vi.mock('./SavedViews', () => ({ SavedViews: () => null }));
vi.mock('./FilterBar', () => ({ FilterBar: () => null }));
vi.mock('./hooks/useUserCostHistory', () => ({ useBulkCostHistory: () => ({ data: {} }) }));
vi.mock('./hooks/useUserTurnAverages', () => ({ useBulkTurnAverages: () => ({ data: {} }) }));
vi.mock('./hooks/useUserAskQuestionFractions', () => ({
  useBulkAskQuestionFractions: () => ({ data: {} }),
}));
vi.mock('./hooks/useUserAutomationScores', () => ({
  useBulkAutomationScores: () => ({ data: {} }),
}));

function listedUser(status: string): AdminUser {
  return {
    id: 'fixture-user',
    email: 'fixture@example.test',
    user_name: 'Fixture user',
    role: 'free',
    status,
    usage_today_usd: 0,
    usage_month_usd: 0,
    usage_alltime_usd: 0,
    usage_alltime_requests: 0,
    usage_alltime_tokens: 0,
  } as AdminUser;
}

function detail(overrides: Partial<UserDetail> = {}): UserDetail {
  return {
    id: 'fixture-user',
    email: 'fixture@example.test',
    user_name: 'Fixture user',
    role: 'free',
    status: 'active',
    email_verified: true,
    created_at: '2024-01-01T00:00:00Z',
    last_login_at: null,
    has_key: true,
    key_prefix: 'hyi_123',
    quota_daily_usd: 100,
    quota_monthly_usd: null,
    usage_today_usd: 0,
    usage_today_requests: 0,
    usage_month_usd: 0,
    usage_month_requests: 0,
    models_used: [],
    disabled_models: [],
    last_request_at: null,
    max_concurrent_requests: null,
    admin_note: null,
    suspension_message: null,
    avg_turns: null,
    avg_user_turns: null,
    ask_question_fraction: null,
    ...overrides,
  };
}

function mockList(status: string) {
  vi.mocked(listUsers).mockResolvedValue({
    total: 1,
    users: [listedUser(status)],
    status_counts: {
      all: 1,
      active: status === 'active' ? 1 : 0,
      pending_approval: status === 'pending_approval' ? 1 : 0,
      suspended: 0,
      rejected: 0,
      deleted: 0,
    },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });
});
afterEach(cleanup);

function renderUsers() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <UsersTab />
    </QueryClientProvider>,
  );
}

it('sends exactly one PATCH for a confirmed account-settings save', async () => {
  mockList('active');
  vi.mocked(getUserDetail)
    .mockResolvedValueOnce(detail())
    .mockResolvedValueOnce(detail({ role: 'pro' }));
  vi.mocked(updateUser).mockResolvedValue({
    user_id: 'fixture-user',
    updated_fields: ['role'],
    message: 'ok',
  });
  renderUsers();

  fireEvent.click(await screen.findByText('fixture@example.test'));
  fireEvent.click(await screen.findByRole('button', { name: 'Edit account settings' }));
  fireEvent.change(screen.getByRole('combobox', { name: 'Role' }), { target: { value: 'pro' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save changes to account settings' }));
  expect(updateUser).not.toHaveBeenCalled();

  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm save' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(vi.mocked(updateUser).mock.calls).toEqual([['fixture-user', { role: 'pro' }]]);
  expect(toast.success).toHaveBeenCalledWith('Saved');
});

it('shows a failed write inside the confirmation instead of a toast', async () => {
  mockList('pending_approval');
  vi.mocked(approveUser)
    .mockRejectedValueOnce(new Error('User is not pending approval'))
    .mockResolvedValueOnce({
      user_id: 'fixture-user',
      email: 'fixture@example.test',
      status: 'active',
      message: 'ok',
    });
  renderUsers();

  fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));
  const dialog = screen.getByRole('dialog', { name: 'Approve fixture@example.test?' });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Approve' }));

  expect(await within(dialog).findByRole('alert')).toHaveTextContent(
    'User is not pending approval',
  );
  expect(toast.error).not.toHaveBeenCalled();
  expect(toast.success).not.toHaveBeenCalled();

  fireEvent.click(within(dialog).getByRole('button', { name: 'Approve' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(approveUser).toHaveBeenCalledTimes(2);
  expect(approveUser).toHaveBeenLastCalledWith('fixture-user');
  expect(toast.success).toHaveBeenCalledWith('Approved fixture@example.test');
});
