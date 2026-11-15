// @vitest-environment jsdom
import { chooseOption } from '@/test-utils/select';
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import toast from 'react-hot-toast';
import UsersTab from './index';
import {
  approveUser,
  batchApproveUsers,
  getUserDetail,
  listModelVisibility,
  listUsers,
  rejectUser,
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
  // The projects panel loads on its own; these tests are about the rest.
  getUserProjects: vi.fn(() => new Promise(() => {})),
  listModelVisibility: vi.fn(),
  updateUser: vi.fn(),
  approveUser: vi.fn(),
  batchApproveUsers: vi.fn(),
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

it('sends exactly one PATCH for an account-settings save', async () => {
  mockList('active');
  vi.mocked(getUserDetail)
    .mockResolvedValueOnce(detail({ role: 'pro' }))
    .mockResolvedValueOnce(detail({ role: 'free' }));
  vi.mocked(updateUser).mockResolvedValue({
    user_id: 'fixture-user',
    updated_fields: ['role'],
    message: 'ok',
  });
  renderUsers();

  fireEvent.click(await screen.findByText('fixture@example.test'));
  await screen.findByRole('combobox', { name: 'Role' });
  chooseOption('Role', 'free');
  expect(updateUser).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Save changes to account settings' }));

  // A lower role is the administrator's to set: nothing to confirm.
  expect(screen.queryByRole('dialog')).toBeNull();
  await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Saved'));
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: 'Save changes to account settings' })).toBeNull(),
  );
  expect(vi.mocked(updateUser).mock.calls).toEqual([['fixture-user', { role: 'free' }]]);
});

it('asks for the password before it raises a role, and sends it with the one PATCH', async () => {
  mockList('active');
  vi.mocked(getUserDetail)
    .mockResolvedValueOnce(detail())
    .mockResolvedValueOnce(detail({ role: 'admin' }));
  vi.mocked(updateUser)
    .mockRejectedValueOnce(new Error('Current password is incorrect.'))
    .mockResolvedValueOnce({ user_id: 'fixture-user', updated_fields: ['role'], message: 'ok' });
  renderUsers();

  fireEvent.click(await screen.findByText('fixture@example.test'));
  await screen.findByRole('combobox', { name: 'Role' });
  chooseOption('Role', 'admin');
  fireEvent.click(screen.getByRole('button', { name: 'Save changes to account settings' }));

  const dialog = await screen.findByRole('dialog');
  expect(dialog).toHaveTextContent('Give fixture@example.test the admin role?');
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));
  expect(await within(dialog).findByRole('alert')).toHaveTextContent(
    'Enter your password to raise this role.',
  );
  expect(updateUser).not.toHaveBeenCalled();

  fireEvent.change(within(dialog).getByLabelText('Your password'), {
    target: { value: 'wrong' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));
  // A refusal keeps the dialog open, with the reason, for another try.
  expect(await within(dialog).findByRole('alert')).toHaveTextContent(
    'Current password is incorrect.',
  );

  fireEvent.change(within(dialog).getByLabelText('Your password'), {
    target: { value: 'right' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(vi.mocked(updateUser).mock.calls).toEqual([
    ['fixture-user', { role: 'admin', password: 'wrong' }],
    ['fixture-user', { role: 'admin', password: 'right' }],
  ]);
});

it('approves on the click and toasts a failure, then approves on the next click', async () => {
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
  expect(screen.queryByRole('dialog')).toBeNull();
  await waitFor(() => expect(toast.error).toHaveBeenCalledWith('User is not pending approval'));
  expect(toast.success).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
  await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Approved fixture@example.test'));
  expect(approveUser).toHaveBeenCalledTimes(2);
  expect(approveUser).toHaveBeenLastCalledWith('fixture-user');
  expect(toast.error).toHaveBeenCalledTimes(1);
});

it('shows a failed rejection inside its confirmation instead of a toast', async () => {
  mockList('pending_approval');
  vi.mocked(rejectUser)
    .mockRejectedValueOnce(new Error('User is not pending approval'))
    .mockResolvedValueOnce({
      user_id: 'fixture-user',
      email: 'fixture@example.test',
      status: 'rejected',
      message: 'ok',
    });
  renderUsers();

  fireEvent.click(await screen.findByRole('button', { name: 'Reject' }));
  const dialog = screen.getByRole('dialog', { name: 'Reject fixture@example.test?' });
  fireEvent.change(within(dialog).getByLabelText('Reason for rejection (required)'), {
    target: { value: 'not a student' },
  });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Reject' }));

  expect(await within(dialog).findByRole('alert')).toHaveTextContent(
    'User is not pending approval',
  );
  expect(toast.error).not.toHaveBeenCalled();
  expect(toast.success).not.toHaveBeenCalled();

  fireEvent.click(within(dialog).getByRole('button', { name: 'Reject' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(rejectUser).toHaveBeenCalledTimes(2);
  expect(rejectUser).toHaveBeenLastCalledWith('fixture-user', 'not a student');
  expect(toast.success).toHaveBeenCalledWith('Rejected fixture@example.test');
});

function mockPendingList(...ids: string[]) {
  vi.mocked(listUsers).mockResolvedValue({
    total: ids.length,
    users: ids.map((id) => ({
      ...listedUser('pending_approval'),
      id,
      email: `${id}@example.test`,
    })),
    status_counts: {
      all: ids.length,
      active: 0,
      pending_approval: ids.length,
      suspended: 0,
      rejected: 0,
      deleted: 0,
    },
  });
}

it('approves the selected users in one request and names the ones left out', async () => {
  mockPendingList('ann', 'bob', 'cat');
  vi.mocked(batchApproveUsers).mockResolvedValue({
    approved: [
      { user_id: 'ann', email: 'ann@example.test' },
      { user_id: 'cat', email: 'cat@example.test' },
    ],
    skipped: [{ user_id: 'bob', reason: 'not_pending', status: 'active' }],
    message: 'Approved 2 users. 1 not approved: no longer pending or not found.',
  });
  renderUsers();

  fireEvent.click(
    await screen.findByRole('checkbox', { name: 'Select all pending users on this page' }),
  );
  fireEvent.click(screen.getByRole('button', { name: 'Approve selected' }));
  const dialog = screen.getByRole('dialog', { name: 'Approve 3 users?' });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Approve 3' }));

  await waitFor(() => expect(toast.success).toHaveBeenCalledWith('Approved 2 users'));
  expect(toast.error).toHaveBeenCalledWith('Not approved, no longer pending: bob@example.test');
  expect(vi.mocked(batchApproveUsers).mock.calls).toEqual([[['ann', 'bob', 'cat']]]);
  expect(approveUser).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
});

it('says in the confirmation when none of the selected users could be approved', async () => {
  mockPendingList('ann');
  vi.mocked(batchApproveUsers).mockResolvedValue({
    approved: [],
    skipped: [{ user_id: 'ann', reason: 'not_pending', status: 'rejected' }],
    message: 'Approved 0 users. 1 not approved: no longer pending or not found.',
  });
  renderUsers();

  fireEvent.click(await screen.findByRole('checkbox', { name: 'Select ann@example.test' }));
  fireEvent.click(screen.getByRole('button', { name: 'Approve selected' }));
  const dialog = screen.getByRole('dialog', { name: 'Approve ann@example.test?' });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Approve' }));

  expect(await within(dialog).findByRole('alert')).toHaveTextContent(
    'None of the selected users was approved: ann@example.test is no longer pending.',
  );
  expect(toast.success).not.toHaveBeenCalled();
  expect(toast.error).not.toHaveBeenCalled();
});
