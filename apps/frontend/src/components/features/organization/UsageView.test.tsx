// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  MemberList,
  ModelUsage,
  OrganizationMember,
  OrganizationRequest,
  RequestPage,
} from '@/lib/api/organization';
import { chooseOption } from '@/test-utils/select';

const state = vi.hoisted(() => ({
  view: null as string | null,
  replace: vi.fn(),
  requests: vi.fn(),
  models: vi.fn(),
  members: vi.fn(),
  refetch: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: state.replace }),
  usePathname: () => '/dashboard/organization/usage',
  useSearchParams: () => new URLSearchParams(state.view ? { view: state.view } : {}),
}));
vi.mock('@/lib/hooks', () => ({
  useOrganizationRequests: (query: unknown) => state.requests(query),
  useOrganizationModelUsage: (period: string, enabled?: boolean) => state.models(period, enabled),
  useOrganizationMemberUsage: (query: unknown) => state.members(query),
  useOrganizationOverview: () => ({ data: { cost_usd: 121.76 } }),
}));

import { UsageView } from './UsageView';

function request(over: Partial<OrganizationRequest>): OrganizationRequest {
  return {
    id: 1,
    timestamp: '2026-11-18T11:33:00Z',
    user_id: 'u-lei',
    email: 'lei.wang@example.edu',
    user_name: 'Lei Wang',
    project_name: 'Thesis',
    key_name: 'laptop',
    model_id: 'glm-4.6',
    status_code: 200,
    prompt_tokens: 1820,
    completion_tokens: 412,
    cost_usd: 0.0123,
    ...over,
  };
}

const MODELS: ModelUsage[] = [
  {
    model_id: 'deepseek-v3.2',
    requests: 900,
    errors: 9,
    prompt_tokens: 2_400_000,
    completion_tokens: 310_000,
    cost_usd: 32.87,
  },
  {
    model_id: 'free-model',
    requests: 40,
    errors: 0,
    prompt_tokens: 9_000,
    completion_tokens: 800,
    cost_usd: 0,
  },
  {
    model_id: 'glm-4.6',
    requests: 1600,
    errors: 20,
    prompt_tokens: 5_100_000,
    completion_tokens: 640_000,
    cost_usd: 49.92,
  },
];

function usage(over: Partial<OrganizationMember>): OrganizationMember {
  return {
    user_id: 'u-lei',
    email: 'lei.wang@example.edu',
    user_name: 'Lei Wang',
    role: 'member',
    status: 'active',
    removed: false,
    joined_at: '2026-10-09T12:00:00Z',
    requests: 982,
    total_tokens: 3_420_000,
    cost_usd: 24.46,
    spent_month_usd: 76.7,
    monthly_limit_source: 'none',
    ...over,
  };
}

function hook<T>(data: T | undefined, more: Record<string, unknown> = {}) {
  return {
    data,
    error: null,
    isLoading: false,
    isPlaceholderData: false,
    refetch: state.refetch,
    ...more,
  };
}

function answerRequests(page: RequestPage | undefined, more: Record<string, unknown> = {}) {
  state.requests.mockReturnValue(hook(page, more));
}

/** The query the requests were last asked with. */
function asked(): Record<string, unknown> {
  return state.requests.mock.lastCall?.[0] as Record<string, unknown>;
}

function memberQuery(): Record<string, unknown> {
  return state.members.mock.lastCall?.[0] as Record<string, unknown>;
}

beforeEach(() => {
  state.view = null;
  state.replace.mockReset();
  state.requests.mockReset();
  state.models.mockReset().mockReturnValue(hook(MODELS));
  state.members.mockReset().mockReturnValue(hook<MemberList>({ total: 0, members: [] }));
  state.refetch.mockReset();
  answerRequests({ requests: [request({})], next_before: null });
});
afterEach(cleanup);

describe('the requests of the organization', () => {
  it('opens on the newest requests of the last 7 days, each with its member', () => {
    render(<UsageView />);

    expect(asked()).toEqual({
      period: '7d',
      search: undefined,
      model: undefined,
      status: undefined,
      limit: 50,
      before: undefined,
    });
    expect(screen.getByRole('tab', { name: 'Requests' })).toHaveAttribute('aria-selected', 'true');
    const panel = screen.getByRole('tabpanel', { name: 'Requests' });
    expect(
      within(panel)
        .getAllByRole('columnheader')
        .map((header) => header.textContent),
    ).toEqual([
      'Time',
      'Member',
      'Project',
      'API key',
      'Model',
      'Status',
      'Input tokens',
      'Output tokens',
      'Cost',
    ]);
    const [, row] = within(panel).getAllByRole('row');
    for (const text of [
      'lei.wang@example.edu',
      'Thesis',
      'laptop',
      'glm-4.6',
      'OK',
      '1,820',
      '412',
    ]) {
      expect(row).toHaveTextContent(text);
    }
    // One page: nothing to page through.
    expect(screen.queryByRole('button', { name: 'Older' })).not.toBeInTheDocument();
  });

  it('pages back through older requests by the cursor of each page, and forward again', () => {
    const pages: Record<string, RequestPage> = {
      first: { requests: [request({ id: 3 })], next_before: 'c1' },
      c1: { requests: [request({ id: 2 })], next_before: 'c2' },
      c2: { requests: [request({ id: 1 })], next_before: null },
    };
    state.requests.mockImplementation((query: { before?: string }) =>
      hook(pages[query.before ?? 'first']),
    );

    render(<UsageView />);
    expect(screen.getByRole('button', { name: 'Newer' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Older' }));
    expect(asked()).toMatchObject({ before: 'c1' });
    fireEvent.click(screen.getByRole('button', { name: 'Older' }));
    expect(asked()).toMatchObject({ before: 'c2' });
    // The last page has nothing older.
    expect(screen.getByRole('button', { name: 'Older' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Newer' }));
    expect(asked()).toMatchObject({ before: 'c1' });
    fireEvent.click(screen.getByRole('button', { name: 'Newer' }));
    expect(asked()).toMatchObject({ before: undefined });
  });

  it('offers no paging while the page asked for is still on its way', () => {
    let arrived = false;
    state.requests.mockImplementation((query: { before?: string }) =>
      query.before && !arrived
        ? // The rows shown are still those of the page before, with its cursor.
          hook({ requests: [request({ id: 3 })], next_before: 'c1' }, { isPlaceholderData: true })
        : hook({ requests: [request({ id: 2 })], next_before: query.before ? 'c2' : 'c1' }),
    );

    const { rerender } = render(<UsageView />);
    fireEvent.click(screen.getByRole('button', { name: 'Older' }));

    expect(asked()).toMatchObject({ before: 'c1' });
    expect(screen.getByRole('button', { name: 'Older' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Newer' })).toBeDisabled();
    // A second click must not put the same cursor on the way back twice.
    fireEvent.click(screen.getByRole('button', { name: 'Older' }));

    arrived = true;
    rerender(<UsageView />);
    expect(screen.getByRole('button', { name: 'Older' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Newer' }));
    // One step back is the first page.
    expect(asked()).toMatchObject({ before: undefined });
  });

  it('starts again from the newest request when a filter or the period changes', () => {
    state.requests.mockImplementation(() => hook({ requests: [request({})], next_before: 'c1' }));

    render(<UsageView />);
    fireEvent.click(screen.getByRole('button', { name: 'Older' }));
    expect(asked()).toMatchObject({ before: 'c1' });

    chooseOption('Status', 'Errors');
    expect(asked()).toMatchObject({ status: 'errors', before: undefined });

    fireEvent.click(screen.getByRole('button', { name: 'Older' }));
    fireEvent.click(screen.getByRole('button', { name: 'Last 30 days' }));
    expect(asked()).toMatchObject({ period: '30d', status: 'errors', before: undefined });

    fireEvent.click(screen.getByRole('button', { name: 'Older' }));
    chooseOption('Status', 'OK');
    expect(asked()).toMatchObject({ status: 'ok', before: undefined });
  });

  it('narrows to one of the models the period has', () => {
    render(<UsageView />);

    expect(state.models).toHaveBeenLastCalledWith('7d', true);
    chooseOption('Model', 'glm-4.6');
    expect(asked()).toMatchObject({ model: 'glm-4.6' });

    chooseOption('Model', 'All models');
    expect(asked()).toMatchObject({ model: undefined });
  });

  it('searches by member once the typing pauses', async () => {
    render(<UsageView />);
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search by member' }), {
      target: { value: ' wang ' },
    });
    expect(asked()).toMatchObject({ search: undefined });

    await waitFor(() => expect(asked()).toMatchObject({ search: 'wang' }));
  });

  it('says a quiet period is quiet, and a filter that matches nothing matches nothing', () => {
    answerRequests({ requests: [], next_before: null });

    render(<UsageView />);
    expect(screen.getByText('No requests in this period.')).toBeInTheDocument();

    chooseOption('Status', 'Errors');
    expect(screen.getByText('No request matches these filters.')).toBeInTheDocument();
  });

  it('offers to try again when the requests cannot be read', () => {
    answerRequests(undefined, { error: new Error('The gateway is not reachable.') });

    render(<UsageView />);

    expect(screen.getByRole('alert')).toHaveTextContent('The gateway is not reachable.');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(state.refetch).toHaveBeenCalledTimes(1);
  });

  it('keeps the view in the address, so a view can be linked to', () => {
    render(<UsageView />);

    fireEvent.click(screen.getByRole('tab', { name: 'By model' }));
    expect(state.replace).toHaveBeenLastCalledWith('/dashboard/organization/usage?view=models', {
      scroll: false,
    });
    fireEvent.click(screen.getByRole('tab', { name: 'By member' }));
    expect(state.replace).toHaveBeenLastCalledWith('/dashboard/organization/usage?view=members', {
      scroll: false,
    });
    fireEvent.click(screen.getByRole('tab', { name: 'Requests' }));
    expect(state.replace).toHaveBeenLastCalledWith('/dashboard/organization/usage', {
      scroll: false,
    });
  });

  it('shows the requests for a view it does not know', () => {
    state.view = 'everything';

    render(<UsageView />);

    expect(screen.getByRole('tab', { name: 'Requests' })).toHaveAttribute('aria-selected', 'true');
    expect(state.requests).toHaveBeenCalled();
  });
});

describe('the usage by model', () => {
  beforeEach(() => {
    state.view = 'models';
  });

  it('lists each model of the period, the largest spend first, with its share', () => {
    render(<UsageView />);

    expect(screen.getByRole('tab', { name: 'By model' })).toHaveAttribute('aria-selected', 'true');
    const panel = screen.getByRole('tabpanel', { name: 'By model' });
    expect(
      within(panel)
        .getAllByRole('row')
        .map((row) => [...row.querySelectorAll('th,td')].map((cell) => cell.textContent)),
    ).toEqual([
      ['Model', 'Requests', 'Errors', 'Input tokens', 'Output tokens', 'Spend', 'Share of spend'],
      ['glm-4.6', '1,600', '20', '5.1M', '640K', '$49.92', '60.3%'],
      ['deepseek-v3.2', '900', '9', '2.4M', '310K', '$32.87', '39.7%'],
      ['free-model', '40', '0', '9,000', '800', '$0.00', '0.0%'],
    ]);
  });

  it('has no filter that does not apply to it, and asks for no requests', () => {
    render(<UsageView />);

    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(state.requests).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Today (UTC)' }));
    expect(state.models).toHaveBeenLastCalledWith('1d', undefined);
  });

  it('gives no share when nothing was spent', () => {
    state.models.mockReturnValue(hook([{ ...MODELS[1] }]));

    render(<UsageView />);

    const [, row] = within(screen.getByRole('tabpanel', { name: 'By model' })).getAllByRole('row');
    expect(row).toHaveTextContent('$0.00');
    expect(row).toHaveTextContent('—');
  });

  it('says a quiet period is quiet', () => {
    state.models.mockReturnValue(hook([]));

    render(<UsageView />);

    expect(screen.getByText('No requests in this period.')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });
});

describe('the usage by member', () => {
  beforeEach(() => {
    state.view = 'members';
    state.members.mockReturnValue(
      hook<MemberList>({
        total: 120,
        members: [
          usage({}),
          usage({
            user_id: 'u-bo',
            email: 'bo.huang@example.edu',
            user_name: null,
            requests: 12,
            total_tokens: 40_000,
            cost_usd: 0,
          }),
        ],
      }),
    );
  });

  it('lists each member with its share of what the organization spent in the period', () => {
    render(<UsageView />);

    expect(memberQuery()).toEqual({ period: '7d', search: undefined, limit: 50, offset: 0 });
    const panel = screen.getByRole('tabpanel', { name: 'By member' });
    expect(
      within(panel)
        .getAllByRole('row')
        .map((row) => [...row.querySelectorAll('th,td')].map((cell) => cell.textContent)),
    ).toEqual([
      ['Member', 'Requests', 'Tokens', 'Spend', 'Share of spend'],
      // 24.46 of the organization's 121.76, not of this page's rows.
      ['Lei Wanglei.wang@example.edu', '982', '3.4M', '$24.46', '20.1%'],
      ['bo.huang@example.edu', '12', '40K', '$0.00', '0.0%'],
    ]);
    // The model filter is the requests view's; its list is not asked for here.
    expect(state.models).toHaveBeenLastCalledWith('7d', false);
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('pages through the members, and starts from the first page for another search', async () => {
    render(<UsageView />);

    expect(screen.getByText('Showing 1–2 of 120')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(memberQuery()).toMatchObject({ offset: 50 });

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search by member' }), {
      target: { value: 'bo' },
    });
    await waitFor(() => expect(memberQuery()).toMatchObject({ search: 'bo', offset: 0 }));
  });

  it('says so when no member matches, and when there is none at all', async () => {
    state.members.mockReturnValue(hook<MemberList>({ total: 0, members: [] }));

    render(<UsageView />);
    expect(screen.getByText('The organization has no members yet.')).toBeInTheDocument();

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search by member' }), {
      target: { value: 'nobody' },
    });
    expect(await screen.findByText('No member matches these filters.')).toBeInTheDocument();
  });
});
