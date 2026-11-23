// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  MemberDetail,
  MemberList,
  MembersSummary,
  OrganizationMember,
} from '@/lib/api/organization';
import { chooseOption } from '@/test-utils/select';

// The chart is loaded with next/dynamic; jsdom cannot lay out recharts anyway.
vi.mock('next/dynamic', () => ({
  default: () => () => <div data-testid="chart" />,
}));

const state = vi.hoisted(() => ({
  organization: vi.fn(),
  members: vi.fn(),
  summary: vi.fn(),
  member: vi.fn(),
  refetch: vi.fn(),
}));
vi.mock('@/lib/hooks', () => ({
  useOrganization: () => state.organization(),
  useOrganizationMembers: (query: unknown) => state.members(query),
  useOrganizationMembersSummary: () => state.summary(),
  useOrganizationMemberModels: () => ({ data: undefined, isLoading: true, refetch: vi.fn() }),
  useSetMemberModels: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRevokeMemberKey: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useOrganizationMember: (userId: string, period: string) => state.member(userId, period),
  useSetMemberMonthlyLimit: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRemoveOrganizationMember: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRestoreOrganizationMember: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock('@/lib/hooks/useAccountToast', () => ({
  useAccountToast: () => ({ success: vi.fn(), error: vi.fn() }),
}));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: 'u-yu' } } }),
}));

import { MembersView } from './MembersView';

function member(over: Partial<OrganizationMember>): OrganizationMember {
  return {
    user_id: 'u-lei',
    email: 'lei.wang@example.edu',
    user_name: 'Lei Wang',
    role: 'member',
    status: 'active',
    removed: false,
    joined_at: '2026-10-09T12:00:00Z',
    last_login_at: '2026-11-18T09:00:00Z',
    last_request_at: '2026-11-18T11:33:00Z',
    requests: 982,
    total_tokens: 3_420_000,
    cost_usd: 24.46,
    spent_month_usd: 76.7,
    spent_today_usd: 3.2,
    spent_7d_usd: 24.46,
    spent_alltime_usd: 310.05,
    spend_days: [1, 2, 3, 4, 5, 6, 3.2].map((cost_usd, index) => ({
      day: `2026-11-${12 + index}`,
      cost_usd,
    })),
    monthly_limit_usd: 130,
    monthly_limit_source: 'member',
    ...over,
  };
}

const LEI = member({});
const YU = member({
  user_id: 'u-yu',
  email: 'yu.chen@example.edu',
  user_name: 'Yu Chen',
  role: 'admin',
  requests: 86,
  total_tokens: 493_000,
  cost_usd: 2.78,
  spent_month_usd: 6.4,
  // An administrator has no limit of the organization: only the quota that
  // the platform administrator set for the account.
  monthly_limit_usd: 40,
  monthly_limit_source: 'platform',
  platform_monthly_limit_usd: 40,
});
const MEI = member({
  user_id: 'u-mei',
  email: 'mei.song@example.edu',
  user_name: 'Mei Song',
  status: 'suspended',
  removed: true,
  requests: 36,
  total_tokens: 185_400,
  cost_usd: 1,
  spent_month_usd: 3.9,
  monthly_limit_usd: 40,
  monthly_limit_source: 'default',
});
const BO = member({
  user_id: 'u-bo',
  email: 'bo.huang@example.edu',
  user_name: null,
  status: 'suspended',
  removed: false,
  last_request_at: null,
  requests: 0,
  total_tokens: 0,
  cost_usd: 0,
  spent_month_usd: 0,
  spent_today_usd: 0,
  spent_7d_usd: 0,
  spent_alltime_usd: 0,
  spend_days: [],
  monthly_limit_usd: null,
  monthly_limit_source: 'none',
});

const SUMMARY: MembersSummary = {
  near_limit: { count: 1, top: [member({ spent_month_usd: 117, monthly_limit_usd: 130 })] },
  top_spenders_today: { count: 2, top: [LEI, member({ user_id: 'u-an', user_name: 'An Li' })] },
};

function answer(list: MemberList | undefined, more: Record<string, unknown> = {}) {
  state.members.mockReturnValue({
    data: list,
    error: null,
    isLoading: false,
    isPlaceholderData: false,
    refetch: state.refetch,
    ...more,
  });
}

function detail(of: OrganizationMember): MemberDetail {
  return { member: of, days: [], models: [], requests: [] };
}

/** The query the list was last asked with. */
function asked(): Record<string, unknown> {
  return state.members.mock.lastCall?.[0] as Record<string, unknown>;
}

function rowOf(name: string): HTMLElement {
  return screen.getByRole('button', { name }).closest('tr') as HTMLElement;
}

beforeEach(() => {
  // Only the clock is fixed, for "27 min ago"; the timers stay real.
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-11-18T12:00:00Z'));
  state.organization.mockReset().mockReturnValue({
    data: { id: 'o-1', name: 'Example University', monthly_limit_usd: 1000 },
    isError: false,
    refetch: vi.fn(),
  });
  state.members.mockReset();
  state.summary.mockReset().mockReturnValue({ data: SUMMARY, isLoading: false });
  state.member.mockReset();
  state.refetch.mockReset();
  state.member.mockImplementation((userId: string) => ({
    data: detail([LEI, YU, MEI, BO].find((one) => one.user_id === userId) ?? LEI),
    error: null,
    isLoading: false,
    refetch: vi.fn(),
  }));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('the members of the organization', () => {
  it('asks for the largest spend of this month first, with no period of its own', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);

    expect(asked()).toEqual({
      search: undefined,
      status: undefined,
      limitState: undefined,
      minCostToday: undefined,
      minCostMonth: undefined,
      currency: 'USD',
      sortBy: 'month',
      order: 'desc',
      limit: 50,
      offset: 0,
    });
    // The spend columns are fixed, so the page has no 1d / 7d / 30d switch.
    expect(screen.queryByRole('group', { name: 'Period' })).not.toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'This month' })).toHaveAttribute(
      'aria-sort',
      'descending',
    );
    expect(screen.getByRole('columnheader', { name: 'Member' })).not.toHaveAttribute('aria-sort');
  });

  it('shows each member with its spend and where its month stands against its limit', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);

    const lei = rowOf('Lei Wang');
    for (const text of [
      'lei.wang@example.edu',
      'Active',
      '27 min ago',
      '$3.20',
      '$76.70',
      '$310.05',
      '59% of $130.00',
    ]) {
      expect(lei).toHaveTextContent(text);
    }
    // In the order of the Admin list: today, the trend, the month, all time.
    expect(screen.getAllByRole('columnheader').map((cell) => cell.textContent)).toEqual([
      'Member',
      'Status',
      'Last request',
      'Today',
      '7 days',
      'This month↓',
      'All-time',
      'Monthly limit',
    ]);
    // The trend is a line of the last 7 days; the pointer shows their total.
    const trend = within(lei).getByRole('img', { name: 'Spend of the last 7 days' });
    expect(trend.closest('td')).toHaveAttribute('title', 'Last 7 days: $24.46');
    expect(lei).not.toHaveTextContent('Admin');
    expect(lei).not.toHaveTextContent('You');
    // The limit is the member's own: a change of the default does not move it.
    expect(lei).not.toHaveTextContent('default');

    const yu = rowOf('Yu Chen');
    expect(yu).toHaveTextContent('Admin');
    expect(yu).toHaveTextContent('You');
    expect(yu).toHaveTextContent('16% of $40.00');
    // The limit is the platform administrator's, and the row says so.
    expect(within(yu).getByText('platform')).toHaveAttribute(
      'title',
      "Platform administrator's limit",
    );
    expect(yu).not.toHaveTextContent('default');
    // The limit is the default of the organization, and the row says so.
    expect(within(rowOf('Mei Song')).getByText('default')).toHaveAttribute(
      'title',
      'Default limit of the organization',
    );

    // Removed by the organization, against suspended by the platform.
    expect(rowOf('Mei Song')).toHaveTextContent('Removed');
    const bo = rowOf('bo.huang@example.edu');
    expect(bo).toHaveTextContent('Suspended');
    expect(bo).toHaveTextContent('Never');
    expect(bo).toHaveTextContent('No limit');
    expect(bo).not.toHaveTextContent('default');
    // A member with no name is shown by its email once, not twice.
    expect(within(bo).getAllByText('bo.huang@example.edu')).toHaveLength(1);
  });

  it('sorts by a column, a figure from its largest and a name from A, and turns on a second click', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);
    fireEvent.click(screen.getByRole('button', { name: 'Today' }));

    expect(asked()).toMatchObject({ sortBy: 'today', order: 'desc', offset: 0 });
    expect(screen.getByRole('columnheader', { name: 'Today' })).toHaveAttribute(
      'aria-sort',
      'descending',
    );
    expect(screen.getByRole('columnheader', { name: 'This month' })).not.toHaveAttribute(
      'aria-sort',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Today' }));
    expect(asked()).toMatchObject({ sortBy: 'today', order: 'asc' });

    fireEvent.click(screen.getByRole('button', { name: '7 days' }));
    expect(asked()).toMatchObject({ sortBy: 'week', order: 'desc' });
    fireEvent.click(screen.getByRole('button', { name: 'All-time' }));
    expect(asked()).toMatchObject({ sortBy: 'alltime', order: 'desc' });

    fireEvent.click(screen.getByRole('button', { name: 'Member' }));
    expect(asked()).toMatchObject({ sortBy: 'name', order: 'asc' });

    // The column shows the share of the limit that the month used, so that is
    // its order: the member nearest its limit first, not the largest spend.
    fireEvent.click(screen.getByRole('button', { name: 'Monthly limit' }));
    expect(asked()).toMatchObject({ sortBy: 'limit_use', order: 'desc' });
  });

  it('searches once the typing pauses, from the first page', async () => {
    answer({ total: 120, members: [LEI] });

    render(<MembersView />);
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(asked()).toMatchObject({ offset: 50 });

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search by name or email' }), {
      target: { value: '  wang ' },
    });
    // Not on each key.
    expect(asked()).toMatchObject({ search: undefined });

    await waitFor(() => expect(asked()).toMatchObject({ search: 'wang', offset: 0 }));
  });

  it('keeps the page when the text changes and the search does not', async () => {
    answer({ total: 120, members: [LEI] });

    render(<MembersView />);
    // At once: the page must also stay when the list has only just opened.
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(asked()).toMatchObject({ offset: 50 });
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(asked()).toMatchObject({ search: undefined, offset: 50 });

    // Spaces are not a search.
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search by name or email' }), {
      target: { value: '   ' },
    });
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(asked()).toMatchObject({ search: undefined, offset: 50 });
  });

  it('narrows to a status and to a use of the limit, each from the first page', () => {
    answer({ total: 120, members: [LEI] });

    render(<MembersView />);
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    chooseOption('Status', 'Removed');

    expect(asked()).toMatchObject({ status: 'removed', offset: 0 });

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    chooseOption('Limit use', 'Over the limit');

    expect(asked()).toMatchObject({ limitState: 'over', status: 'removed', offset: 0 });

    chooseOption('Status', 'All statuses');
    chooseOption('Limit use', 'Any limit use');
    expect(asked()).toMatchObject({ status: undefined, limitState: undefined });
  });

  it('narrows to a least spend of today and of the month, in the ledger currency', () => {
    answer({ total: 120, members: [LEI] });

    render(<MembersView />);
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.change(screen.getByLabelText('Today ≥ $'), { target: { value: '2.5' } });

    expect(asked()).toMatchObject({ minCostToday: 2.5, currency: 'USD', offset: 0 });

    fireEvent.change(screen.getByLabelText('Month ≥ $'), { target: { value: '0' } });
    // Zero is a threshold, not the lack of one.
    expect(asked()).toMatchObject({ minCostToday: 2.5, minCostMonth: 0 });

    fireEvent.change(screen.getByLabelText('Today ≥ $'), { target: { value: '' } });
    expect(asked()).toMatchObject({ minCostToday: undefined, minCostMonth: 0 });
  });

  it('shows the two cards, each with its count and the members that lead it', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);

    const near = screen.getByRole('button', { name: /Near or over the limit/ });
    expect(near).toHaveTextContent('1');
    // The share of the limit that the month used.
    expect(near).toHaveTextContent('Lei Wang90%');
    const spenders = screen.getByRole('button', { name: /Top spenders today/ });
    expect(spenders).toHaveTextContent('2');
    expect(spenders).toHaveTextContent('Lei Wang$3.20');
    expect(spenders).toHaveTextContent('An Li');
    expect(near).toHaveAttribute('aria-pressed', 'false');
  });

  it('opens the list of a card: its filter and its order, and nothing else', async () => {
    answer({ total: 120, members: [LEI] });

    render(<MembersView />);
    chooseOption('Status', 'Removed');
    fireEvent.change(screen.getByLabelText('Today ≥ $'), { target: { value: '5' } });
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('button', { name: /Near or over the limit/ }));

    expect(asked()).toMatchObject({
      limitState: 'near',
      sortBy: 'limit_use',
      order: 'desc',
      status: undefined,
      minCostToday: undefined,
      offset: 0,
    });
    expect(screen.getByRole('button', { name: /Near or over the limit/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    );

    fireEvent.click(screen.getByRole('button', { name: /Top spenders today/ }));

    expect(asked()).toMatchObject({ limitState: undefined, sortBy: 'today', order: 'desc' });
    expect(screen.getByRole('button', { name: /Top spenders today/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(screen.getByRole('button', { name: /Near or over the limit/ })).toHaveAttribute(
      'aria-pressed',
      'false',
    );

    // Another order is no longer the list of the card.
    fireEvent.click(screen.getByRole('button', { name: 'Member' }));
    expect(screen.getByRole('button', { name: /Top spenders today/ })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });

  it('shows a dash on the cards while they are read, and the list all the same', () => {
    state.summary.mockReturnValue({ data: undefined, isLoading: true });
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);

    expect(screen.getByRole('button', { name: /Near or over the limit/ })).toHaveTextContent('—');
    expect(rowOf('Lei Wang')).toBeInTheDocument();
  });

  it('pages through a long list and says where the page is', () => {
    answer({ total: 120, members: [LEI, YU] });

    render(<MembersView />);

    expect(screen.getByText('Showing 1–2 of 120')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));

    expect(asked()).toMatchObject({ offset: 50 });
    expect(screen.getByText('Showing 51–52 of 120')).toBeInTheDocument();
  });

  it('goes back to the last page when the list gets shorter than the page it is on', async () => {
    answer({ total: 120, members: [LEI] });
    const { rerender } = render(<MembersView />);
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(asked()).toMatchObject({ offset: 100 });

    // Members were removed elsewhere: 60 are left, so the third page is gone.
    answer({ total: 60, members: [] });
    rerender(<MembersView />);

    await waitFor(() => expect(asked()).toMatchObject({ offset: 50 }));
  });

  it('opens a member in a panel beside the list, with the actions an administrator has on it', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);
    expect(state.member).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Lei Wang' }));

    const panel = screen.getByRole('dialog', { name: 'Lei Wang' });
    expect(state.member).toHaveBeenLastCalledWith('u-lei', '30d');
    expect(panel).toHaveTextContent('lei.wang@example.edu');
    expect(
      within(panel).getByText('Joined Oct 9, 2026 · Last sign-in 3 h ago'),
    ).toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: 'Set monthly limit' })).toBeInTheDocument();
    expect(
      within(panel).getByRole('button', { name: 'Remove from organization' }),
    ).toBeInTheDocument();
    expect(within(panel).queryByRole('button', { name: 'Restore member' })).not.toBeInTheDocument();
    // The list stays behind the panel, with the member's row marked.
    expect(screen.getByRole('button', { name: 'Lei Wang' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(rowOf('Yu Chen')).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole('button', { name: 'Close' }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Lei Wang' })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
  });

  it('opens the member of a row by a click anywhere on the row, one member at a time', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);
    fireEvent.click(rowOf('Mei Song'));

    const mei = screen.getByRole('dialog', { name: 'Mei Song' });
    // Removed by the organization: it can come back, and cannot be removed twice.
    expect(within(mei).getByRole('button', { name: 'Restore member' })).toBeInTheDocument();
    expect(
      within(mei).queryByRole('button', { name: 'Remove from organization' }),
    ).not.toBeInTheDocument();

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    fireEvent.click(rowOf('Lei Wang'));
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(screen.getByRole('dialog', { name: 'Lei Wang' })).toBeInTheDocument();
    expect(state.member).toHaveBeenLastCalledWith('u-lei', '30d');
  });

  it('opens the confirmation of the chosen action over the panel', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);
    fireEvent.click(screen.getByRole('button', { name: 'Lei Wang' }));
    const panel = screen.getByRole('dialog', { name: 'Lei Wang' });
    fireEvent.click(within(panel).getByRole('button', { name: 'Remove from organization' }));

    const confirmation = screen.getByRole('dialog', { name: 'Remove from organization' });
    expect(confirmation).toHaveTextContent(
      'The account of Lei Wang (lei.wang@example.edu) is suspended:',
    );
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Cancel' }));
    expect(
      screen.queryByRole('dialog', { name: 'Remove from organization' }),
    ).not.toBeInTheDocument();

    fireEvent.click(within(panel).getByRole('button', { name: 'Set monthly limit' }));
    expect(screen.getByRole('dialog', { name: 'Set monthly limit' })).toBeInTheDocument();
  });

  it('says in the limit dialog that the organization could not be read, and reads it again', () => {
    const again = vi.fn();
    state.organization.mockReturnValue({ data: undefined, isError: true, refetch: again });
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);
    fireEvent.click(screen.getByRole('button', { name: 'Lei Wang' }));
    const panel = screen.getByRole('dialog', { name: 'Lei Wang' });
    fireEvent.click(within(panel).getByRole('button', { name: 'Set monthly limit' }));

    const confirmation = screen.getByRole('dialog', { name: 'Set monthly limit' });
    expect(within(confirmation).getByRole('alert')).toHaveTextContent(
      'The organization could not be read, so the limit cannot be saved.',
    );
    expect(within(confirmation).getByRole('button', { name: 'Save' })).toBeDisabled();
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Retry' }));
    expect(again).toHaveBeenCalledTimes(1);
  });

  it('closes only the confirmation on Escape, and the panel on the next one', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);
    fireEvent.click(screen.getByRole('button', { name: 'Lei Wang' }));
    fireEvent.click(screen.getByRole('button', { name: 'Remove from organization' }));
    expect(screen.getAllByRole('dialog')).toHaveLength(2);

    fireEvent.keyDown(window, { key: 'Escape' });

    expect(
      screen.queryByRole('dialog', { name: 'Remove from organization' }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Lei Wang' })).toBeInTheDocument();

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('keeps the panel of a member that the list no longer shows', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });
    const { rerender } = render(<MembersView />);
    fireEvent.click(screen.getByRole('button', { name: 'Lei Wang' }));

    // Removed under a filter for active members: the row goes, the panel stays
    // and shows the member as it now is.
    state.member.mockReturnValue({
      data: detail({ ...LEI, status: 'suspended', removed: true }),
      error: null,
      isLoading: false,
      refetch: vi.fn(),
    });
    answer({ total: 3, members: [YU, MEI, BO] });
    rerender(<MembersView />);

    const panel = screen.getByRole('dialog', { name: 'Lei Wang' });
    expect(panel).toHaveTextContent('Removed');
    expect(within(panel).getByRole('button', { name: 'Restore member' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Lei Wang' })).not.toBeInTheDocument();
  });

  it('gives no action on an organization administrator, and says who has them', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);
    fireEvent.click(screen.getByRole('button', { name: 'Yu Chen' }));

    expect(screen.queryByRole('button', { name: 'Set monthly limit' })).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Remove from organization' }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(
        'The platform administrator sets the limit of an organization administrator and removes one.',
      ),
    ).toBeInTheDocument();
  });

  it('says so when no member matches, and when there is none at all', () => {
    answer({ total: 0, members: [] });

    render(<MembersView />);
    expect(screen.getByText('The organization has no members yet.')).toBeInTheDocument();

    chooseOption('Status', 'Suspended');
    expect(screen.getByText('No member matches these filters.')).toBeInTheDocument();
  });

  it('offers to try again when the list cannot be read', () => {
    answer(undefined, { error: new Error('The gateway is not reachable.') });

    render(<MembersView />);

    expect(screen.getByRole('alert')).toHaveTextContent('The gateway is not reachable.');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(state.refetch).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('shows that it is loading, with the filters already there', () => {
    answer(undefined, { isLoading: true });

    render(<MembersView />);

    expect(screen.getByRole('status')).toHaveTextContent('Loading...');
    expect(screen.getByRole('searchbox', { name: 'Search by name or email' })).toBeInTheDocument();
  });

  it('reads the native amounts on a ledger that is not in USD', () => {
    answer({
      total: 1,
      members: [
        {
          ...LEI,
          currency: 'CNY',
          cost: 164.4,
          spent_today: 21.5,
          spent_7d: 164.4,
          spent_month: 515.5,
          spent_alltime: 2083.7,
          monthly_limit: 900,
        } as OrganizationMember,
      ],
    });

    render(<MembersView />);

    const lei = rowOf('Lei Wang');
    expect(lei).toHaveTextContent('¥21.50');
    expect(lei).toHaveTextContent('¥515.50');
    expect(lei).toHaveTextContent('¥2,083.70');
    expect(lei).toHaveTextContent('57% of ¥900.00');
    // Never the USD equivalents under a yuan sign.
    expect(lei).not.toHaveTextContent('3.20');
    expect(lei).not.toHaveTextContent('310.05');
    expect(lei).not.toHaveTextContent('130');
  });
});
