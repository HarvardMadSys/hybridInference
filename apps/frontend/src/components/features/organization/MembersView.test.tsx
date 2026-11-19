// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { MemberDetail, MemberList, OrganizationMember } from '@/lib/api/organization';
import { chooseOption } from '@/test-utils/select';

// The chart is loaded with next/dynamic; jsdom cannot lay out recharts anyway.
vi.mock('next/dynamic', () => ({
  default: () => () => <div data-testid="chart" />,
}));

const state = vi.hoisted(() => ({
  members: vi.fn(),
  member: vi.fn(),
  refetch: vi.fn(),
}));
vi.mock('@/lib/hooks', () => ({
  useOrganization: () => ({
    data: { id: 'o-1', name: 'Example University', monthly_limit_usd: 1000 },
  }),
  useOrganizationMembers: (query: unknown) => state.members(query),
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
  monthly_limit_usd: 40,
  monthly_limit_source: 'default',
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
  monthly_limit_usd: null,
  monthly_limit_source: 'none',
});

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
  state.members.mockReset();
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
  it('asks for the largest spend of the last 7 days first', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);

    expect(asked()).toEqual({
      period: '7d',
      search: undefined,
      status: undefined,
      sortBy: 'spend',
      order: 'desc',
      limit: 50,
      offset: 0,
    });
    expect(screen.getByRole('columnheader', { name: 'Spend' })).toHaveAttribute(
      'aria-sort',
      'descending',
    );
    expect(screen.getByRole('columnheader', { name: 'Member' })).not.toHaveAttribute('aria-sort');
  });

  it('shows each member with its usage and where its month stands against its limit', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);

    const lei = rowOf('Lei Wang');
    for (const text of [
      'lei.wang@example.edu',
      'Active',
      '27 min ago',
      '982',
      '3.4M',
      '$24.46',
      '59% of $130.00',
    ]) {
      expect(lei).toHaveTextContent(text);
    }
    expect(lei).not.toHaveTextContent('Admin');
    expect(lei).not.toHaveTextContent('You');

    const yu = rowOf('Yu Chen');
    expect(yu).toHaveTextContent('Admin');
    expect(yu).toHaveTextContent('You');
    expect(yu).toHaveTextContent('16% of $40.00');

    // Removed by the organization, against suspended by the platform.
    expect(rowOf('Mei Song')).toHaveTextContent('Removed');
    const bo = rowOf('bo.huang@example.edu');
    expect(bo).toHaveTextContent('Suspended');
    expect(bo).toHaveTextContent('Never');
    expect(bo).toHaveTextContent('No limit');
    // A member with no name is shown by its email once, not twice.
    expect(within(bo).getAllByText('bo.huang@example.edu')).toHaveLength(1);
  });

  it('sorts by a column, a figure from its largest and a name from A, and turns on a second click', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);
    fireEvent.click(screen.getByRole('button', { name: 'Requests' }));

    expect(asked()).toMatchObject({ sortBy: 'requests', order: 'desc', offset: 0 });
    expect(screen.getByRole('columnheader', { name: 'Requests' })).toHaveAttribute(
      'aria-sort',
      'descending',
    );
    expect(screen.getByRole('columnheader', { name: 'Spend' })).not.toHaveAttribute('aria-sort');

    fireEvent.click(screen.getByRole('button', { name: 'Requests' }));
    expect(asked()).toMatchObject({ sortBy: 'requests', order: 'asc' });

    fireEvent.click(screen.getByRole('button', { name: 'Member' }));
    expect(asked()).toMatchObject({ sortBy: 'name', order: 'asc' });

    fireEvent.click(screen.getByRole('button', { name: 'Monthly limit' }));
    expect(asked()).toMatchObject({ sortBy: 'month', order: 'desc' });
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

  it('narrows to a status and to a period, each from the first page', () => {
    answer({ total: 120, members: [LEI] });

    render(<MembersView />);
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    chooseOption('Status', 'Removed');

    expect(asked()).toMatchObject({ status: 'removed', offset: 0 });

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('button', { name: 'Last 30 days' }));

    expect(asked()).toMatchObject({ period: '30d', status: 'removed', offset: 0 });

    chooseOption('Status', 'All statuses');
    expect(asked()).toMatchObject({ status: undefined });
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

  it('opens a member in place with the actions an administrator has on it', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);
    expect(state.member).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Lei Wang' }));

    expect(state.member).toHaveBeenLastCalledWith('u-lei', '7d');
    expect(rowOf('Lei Wang')).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Joined Oct 9, 2026 · Last sign-in 3 h ago')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Set monthly limit' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Remove from organization' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Restore member' })).not.toBeInTheDocument();

    // A second click closes it.
    fireEvent.click(screen.getByRole('button', { name: 'Lei Wang' }));
    expect(screen.queryByRole('button', { name: 'Set monthly limit' })).not.toBeInTheDocument();
    expect(rowOf('Lei Wang')).toHaveAttribute('aria-expanded', 'false');
  });

  it('opens one member at a time, by a click anywhere on its row', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);
    fireEvent.click(rowOf('Lei Wang'));
    fireEvent.click(rowOf('Mei Song'));

    expect(rowOf('Lei Wang')).toHaveAttribute('aria-expanded', 'false');
    expect(rowOf('Mei Song')).toHaveAttribute('aria-expanded', 'true');
    // Removed by the organization: it can come back, and cannot be removed twice.
    expect(screen.getByRole('button', { name: 'Restore member' })).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Remove from organization' }),
    ).not.toBeInTheDocument();
  });

  it('opens the dialog of the action that was chosen', () => {
    answer({ total: 4, members: [LEI, YU, MEI, BO] });

    render(<MembersView />);
    fireEvent.click(screen.getByRole('button', { name: 'Lei Wang' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Remove from organization' }));

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent(
      'The account of Lei Wang (lei.wang@example.edu) is suspended:',
    );
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Set monthly limit' }));
    expect(
      within(screen.getByRole('dialog')).getByRole('heading', { name: 'Set monthly limit' }),
    ).toBeInTheDocument();
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
          spent_month: 515.5,
          monthly_limit: 900,
        } as OrganizationMember,
      ],
    });

    render(<MembersView />);

    const lei = rowOf('Lei Wang');
    expect(lei).toHaveTextContent('¥164.40');
    expect(lei).toHaveTextContent('57% of ¥900.00');
    // Never the USD equivalents under a yuan sign.
    expect(lei).not.toHaveTextContent('24.46');
    expect(lei).not.toHaveTextContent('130');
  });
});
