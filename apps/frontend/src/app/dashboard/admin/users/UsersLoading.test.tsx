// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import UsersTab from './index';
import { getBulkUsageTotals, listUsers, type AdminUser } from '@/lib/api/admin';

vi.mock('next/navigation', () => {
  const search = new URLSearchParams();
  const router = { replace: vi.fn() };
  return {
    useSearchParams: () => search,
    useRouter: () => router,
    usePathname: () => '/dashboard/admin/users',
  };
});
vi.mock('@/lib/api/admin', () => ({ listUsers: vi.fn(), getBulkUsageTotals: vi.fn() }));
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

const user = {
  id: 'fixture-user',
  email: 'fixture@example.test',
  user_name: 'Fixture user',
  role: 'free',
  status: 'active',
  usage_today_usd: 1,
  usage_month_usd: 2,
  usage_alltime_usd: 3,
  usage_alltime_requests: null,
  usage_alltime_tokens: null,
} as AdminUser;

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

it('shows the real table while lifetime totals are pending, then fills them in', async () => {
  let resolve!: (value: Awaited<ReturnType<typeof getBulkUsageTotals>>) => void;
  vi.mocked(getBulkUsageTotals).mockImplementation(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  renderUsers();
  expect(await screen.findByText('Fixture user')).toBeInTheDocument();
  expect(screen.getByRole('table')).toBeInTheDocument();
  expect(listUsers).toHaveBeenCalledWith(expect.objectContaining({ includeUsageTotals: false }));
  expect(screen.getByTitle('All-time requests not loaded yet')).toHaveTextContent('—');
  await waitFor(() =>
    expect(getBulkUsageTotals).toHaveBeenCalledWith(['fixture-user'], expect.any(AbortSignal)),
  );
  await act(async () => resolve({ totals: { 'fixture-user': { requests: 0, tokens: 12345 } } }));
  expect(await screen.findByTitle('0 requests (all-time)')).toHaveTextContent('0');
  expect(screen.getByTitle('12,345 tokens (all-time)')).toBeInTheDocument();
});

it('keeps management usable on statistics failure and retries only when asked', async () => {
  vi.mocked(getBulkUsageTotals)
    .mockRejectedValueOnce(new Error('statistics timeout'))
    .mockResolvedValueOnce({ totals: { 'fixture-user': { requests: 7, tokens: 8 } } });
  renderUsers();
  expect(await screen.findByText('Fixture user')).toBeInTheDocument();
  const retry = await screen.findByRole('button', { name: 'Retry statistics' });
  expect(getBulkUsageTotals).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('table')).toBeInTheDocument();
  fireEvent.click(retry);
  expect(await screen.findByTitle('7 requests (all-time)')).toBeInTheDocument();
  expect(listUsers).toHaveBeenCalledTimes(1);
});
