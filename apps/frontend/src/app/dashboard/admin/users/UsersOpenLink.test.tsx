// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import UsersTab from './index';
import { userDetailHref } from './lib/userLink';
import {
  getUserDetail,
  listModelVisibility,
  listUsers,
  type AdminUser,
  type UserDetail,
} from '@/lib/api/admin';

/** The Users tab's address, as a test sets it, and the router that rewrites it. */
const address = vi.hoisted(() => ({ search: new URLSearchParams(), router: { replace: vi.fn() } }));
vi.mock('next/navigation', () => ({
  useSearchParams: () => address.search,
  useRouter: () => address.router,
  usePathname: () => '/dashboard/admin/users',
}));
vi.mock('@/lib/api/admin', () => ({
  listUsers: vi.fn(),
  getBulkUsageTotals: vi.fn(),
  getUserDetail: vi.fn(),
  // The projects panel loads on its own; these tests are about the rest.
  getUserProjects: vi.fn(() => new Promise(() => {})),
  listModelVisibility: vi.fn(),
}));
vi.mock('./SummaryCards', () => ({ SummaryCards: () => null }));
vi.mock('./SavedViews', () => ({ SavedViews: () => null }));
vi.mock('./FilterBar', () => ({ FilterBar: () => null }));
vi.mock('./UserTagsPanel', () => ({ UserTagsPanel: () => null }));
vi.mock('./hooks/useUserCostHistory', () => ({ useBulkCostHistory: () => ({ data: {} }) }));
vi.mock('./hooks/useUserTurnAverages', () => ({ useBulkTurnAverages: () => ({ data: {} }) }));
vi.mock('./hooks/useUserAskQuestionFractions', () => ({
  useBulkAskQuestionFractions: () => ({ data: {} }),
}));
vi.mock('./hooks/useUserAutomationScores', () => ({
  useBulkAutomationScores: () => ({ data: {} }),
}));

const user = {
  id: 'fixture-user',
  email: 'fixture@example.test',
  user_name: 'Fixture user',
  role: 'free',
  status: 'active',
  usage_today_usd: 0,
  usage_month_usd: 0,
  usage_alltime_usd: 0,
  usage_alltime_requests: 0,
  usage_alltime_tokens: 0,
} as AdminUser;

const detail: UserDetail = {
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
};

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  vi.mocked(listUsers).mockResolvedValue({
    total: 1,
    users: [user],
    status_counts: {
      all: 1,
      active: 1,
      pending_approval: 0,
      suspended: 0,
      rejected: 0,
      deleted: 0,
    },
  });
  vi.mocked(getUserDetail).mockResolvedValue(detail);
  vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });
  // As the real router does, a rewrite changes the address the tab reads next.
  address.router.replace.mockImplementation((url: string) => {
    address.search = new URL(url, 'https://console.test').searchParams;
  });
});
afterEach(cleanup);

function renderUsers() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = () => (
    <QueryClientProvider client={client}>
      <UsersTab />
    </QueryClientProvider>
  );
  const view = render(tree());
  return {
    ...view,
    /** A navigation while the tab is mounted: the address changes and the tab draws again. */
    navigate(url: string) {
      address.search = new URL(url, 'https://console.test').searchParams;
      view.rerender(tree());
    },
  };
}

const noUsers = {
  total: 0,
  users: [],
  status_counts: { all: 0, active: 0, pending_approval: 0, suspended: 0, rejected: 0, deleted: 0 },
};

it('lists the account a request links to and opens its details', async () => {
  address.search = new URL(userDetailHref('fixture-user'), 'https://console.test').searchParams;
  renderUsers();

  expect(await screen.findByRole('textbox', { name: 'Admin note' })).toBeInTheDocument();
  expect(listUsers).toHaveBeenCalledWith(expect.objectContaining({ search: 'fixture-user' }));
  expect(getUserDetail).toHaveBeenCalledTimes(1);
  expect(getUserDetail).toHaveBeenCalledWith('fixture-user');
  // The link has done its work: the address keeps the search and loses `open`.
  await waitFor(() =>
    expect(address.router.replace).toHaveBeenCalledWith('/dashboard/admin/users?q=fixture-user', {
      scroll: false,
    }),
  );
  expect(address.router.replace).toHaveBeenCalledTimes(1);
});

it('opens no account on an ordinary visit', async () => {
  address.search = new URLSearchParams();
  renderUsers();

  expect(await screen.findByText('Fixture user')).toBeInTheDocument();
  expect(getUserDetail).not.toHaveBeenCalled();
  expect(address.router.replace).not.toHaveBeenCalled();
});

it('says when the account a link names is not listed, and drops `open`', async () => {
  vi.mocked(listUsers).mockResolvedValue(noUsers);
  address.search = new URL(userDetailHref('gone-user'), 'https://console.test').searchParams;
  renderUsers();

  expect(await screen.findByText(/No account with the ID/)).toHaveTextContent(
    'No account with the ID gone-user is listed. It may have been permanently deleted.',
  );
  expect(address.router.replace).toHaveBeenCalledWith('/dashboard/admin/users?q=gone-user', {
    scroll: false,
  });
  expect(getUserDetail).not.toHaveBeenCalled();

  // Any filter change moves on from the link.
  fireEvent.click(screen.getByRole('button', { name: 'Today' }));
  await waitFor(() => expect(screen.queryByText(/No account with the ID/)).toBeNull());
});

it('ends the note when a step back or forward shows other filters', async () => {
  vi.mocked(listUsers).mockResolvedValue(noUsers);
  address.search = new URL(userDetailHref('gone-user'), 'https://console.test').searchParams;
  const { navigate } = renderUsers();
  await screen.findByText(/No account with the ID/);

  // The browser steps to an address with other filters; no control rewrote it.
  navigate('/dashboard/admin/users?q=someone-else');

  await waitFor(() => expect(screen.queryByText(/No account with the ID/)).toBeNull());
});

it('waits for the list a link names before calling its account missing', async () => {
  const other = {
    ...user,
    id: 'other-user',
    email: 'other@example.test',
    user_name: 'Other user',
  } as AdminUser;
  vi.mocked(listUsers).mockImplementation(async (params) => ({
    total: 1,
    users: [params?.search === 'other-user' ? other : user],
    status_counts: {
      all: 1,
      active: 1,
      pending_approval: 0,
      suspended: 0,
      rejected: 0,
      deleted: 0,
    },
  }));
  vi.mocked(getUserDetail).mockResolvedValue({
    ...detail,
    id: 'other-user',
    email: 'other@example.test',
  });
  address.search = new URLSearchParams();
  const { navigate } = renderUsers();
  expect(await screen.findByText('Fixture user')).toBeInTheDocument();

  // A link reaches the tab while it still shows the full list, which lacks the account.
  navigate(userDetailHref('other-user'));

  expect(await screen.findByRole('textbox', { name: 'Admin note' })).toBeInTheDocument();
  expect(getUserDetail).toHaveBeenCalledWith('other-user');
  expect(screen.queryByText(/No account with the ID/)).toBeNull();
});
