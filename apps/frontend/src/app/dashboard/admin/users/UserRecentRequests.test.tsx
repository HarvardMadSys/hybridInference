// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AdminRecentRequestItem, AdminRecentRequestsResponse } from '@/lib/api/admin';
import { UserRecentRequests } from './UserRecentRequests';

vi.mock('react-hot-toast', () => ({
  default: { error: vi.fn(), success: vi.fn() },
}));

vi.mock('@/lib/api/admin', () => ({
  listRecentRequests: vi.fn(),
  getRecentRequestContent: vi.fn(),
}));

import { listRecentRequests } from '@/lib/api/admin';

function makeRequest(index: number): AdminRecentRequestItem {
  return {
    request_id: `req_${index}`,
    user_id: 'user_1',
    user_name: 'Ada',
    user_email: 'ada@example.com',
    model_id: 'gpt-4o-mini',
    provider: 'openai',
    timestamp: '2026-06-30T12:00:00.000Z',
    status_code: 200,
    latency_ms: 1200,
    prompt_tokens: 100,
    completion_tokens: 50,
  };
}

function mockPage(page: Partial<AdminRecentRequestsResponse>) {
  vi.mocked(listRecentRequests).mockResolvedValue({
    requests: Array.from({ length: 25 }, (_, i) => makeRequest(i)),
    total: 25,
    limit: 25,
    offset: 0,
    ...page,
  });
}

describe('UserRecentRequests paging', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(cleanup);

  it('shows a total the backend could not finish counting as a lower bound', async () => {
    mockPage({ total: 25, total_exact: false, has_more: true });

    render(<UserRecentRequests userId="user_1" />);

    expect(await screen.findByText('25+ total')).toBeInTheDocument();
    // No "of N": the total is only the rows loaded so far.
    expect(screen.getByText(/^1\D25$/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled();
  });

  it('stops at the page where has_more runs out', async () => {
    mockPage({ total: 40, total_exact: true, has_more: true });

    render(<UserRecentRequests userId="user_1" />);

    expect(await screen.findByText('40 total')).toBeInTheDocument();
    mockPage({
      requests: Array.from({ length: 15 }, (_, i) => makeRequest(25 + i)),
      total: 40,
      total_exact: true,
      has_more: false,
      offset: 25,
    });
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await screen.findByText(/^26\D40 of 40$/);
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
  });

  it('asks for a fresh total on Refresh only', async () => {
    mockPage({ total: 3, has_more: false });

    render(<UserRecentRequests userId="user_1" />);

    await screen.findByText('3 total');
    // refresh is the 9th positional argument of listRecentRequests.
    expect(vi.mocked(listRecentRequests).mock.calls.at(-1)?.[8]).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(vi.mocked(listRecentRequests).mock.calls.at(-1)?.[8]).toBe(true));
  });
});
