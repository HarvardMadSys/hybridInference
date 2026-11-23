// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { MemberDetail, OrganizationMember } from '@/lib/api/organization';

// The chart is loaded with next/dynamic; jsdom cannot lay out recharts anyway.
// `data-values` is each value as the chart writes it in its tooltip, and
// `data-ticks` as it writes it on its axis.
vi.mock('next/dynamic', () => ({
  default:
    () =>
    (props: {
      points: Array<{ label: string; value: number }>;
      formatValue: (value: number) => string;
      formatTick?: (value: number) => string;
    }) => (
      <div
        data-testid="chart"
        data-values={props.points.map((point) => props.formatValue(point.value)).join(' ')}
        data-ticks={props.points
          .map((point) => (props.formatTick ?? props.formatValue)(point.value))
          .join(' ')}
      >
        {props.points.map((point) => `${point.label}=${point.value}`).join(' ')}
      </div>
    ),
}));

const state = vi.hoisted(() => ({ member: vi.fn(), refetch: vi.fn() }));
vi.mock('@/lib/hooks', () => ({
  useOrganizationMember: (userId: string, period: string) => state.member(userId, period),
}));

import { MemberDetailPanel, memberActions } from './MemberDetailPanel';

const LEI: OrganizationMember = {
  user_id: 'u-lei',
  email: 'lei.wang@example.edu',
  user_name: 'Lei Wang',
  role: 'member',
  status: 'active',
  removed: false,
  joined_at: '2026-10-09T12:00:00Z',
  last_login_at: null,
  last_request_at: '2026-11-18T11:33:00Z',
  requests: 982,
  total_tokens: 3_420_000,
  cost_usd: 24.46,
  spent_month_usd: 76.7,
  spent_today_usd: 3.2,
  spent_7d_usd: 24.46,
  spent_alltime_usd: 310.05,
  monthly_limit_usd: 130,
  monthly_limit_source: 'member',
};

const DETAIL: MemberDetail = {
  member: LEI,
  account: { has_key: true, two_factor_enabled: false, email_verified: true },
  projects: [
    {
      id: 'p-default',
      name: 'Default',
      is_default: true,
      created_at: '2026-10-09T12:00:00Z',
      spent_today_usd: 0,
      spent_month_usd: 0,
    },
    {
      id: 'p-thesis',
      name: 'Thesis',
      is_default: false,
      created_at: '2026-10-10T12:00:00Z',
      spent_today_usd: 3.2,
      spent_month_usd: 76.7,
    },
  ],
  keys: [
    {
      id: 41,
      name: 'laptop',
      status: 'active',
      project_id: 'p-thesis',
      project_name: 'Thesis',
      created_at: '2026-10-10T12:00:00Z',
      last_used_at: '2026-11-18T11:33:00Z',
      spent_today_usd: 3.2,
      spent_month_usd: 70,
    },
    {
      id: 42,
      name: null,
      status: 'revoked',
      project_id: 'p-thesis',
      project_name: 'Thesis',
      created_at: '2026-10-11T12:00:00Z',
      last_used_at: null,
      spent_today_usd: 0,
      spent_month_usd: 6.7,
    },
  ],
  disabled_models: ['glm-4.6'],
  platform_disabled_models: ['big-model'],
  days: [
    {
      bucket: '2026-11-16T00:00:00Z',
      cost_usd: 3.5,
      requests: 120,
      total_tokens: 400_000,
      members_with_requests: 1,
    },
    {
      bucket: '2026-11-17T00:00:00Z',
      cost_usd: 4.25,
      requests: 150,
      total_tokens: 500_000,
      members_with_requests: 1,
    },
  ],
  models: [
    {
      model_id: 'glm-4.6',
      requests: 600,
      errors: 4,
      prompt_tokens: 1,
      completion_tokens: 1,
      cost_usd: 14.2,
    },
    {
      model_id: 'deepseek-v3.2',
      requests: 382,
      errors: 1,
      prompt_tokens: 1,
      completion_tokens: 1,
      cost_usd: 10.26,
    },
  ],
  requests: [
    {
      id: 7,
      timestamp: '2026-11-18T11:33:00Z',
      user_id: 'u-lei',
      email: 'lei.wang@example.edu',
      project_name: 'Thesis',
      key_name: 'laptop',
      model_id: 'glm-4.6',
      status_code: 200,
      prompt_tokens: 1820,
      completion_tokens: 412,
      cost_usd: 0.0123,
    },
    {
      id: 6,
      timestamp: '2026-11-18T11:30:00Z',
      user_id: 'u-lei',
      email: 'lei.wang@example.edu',
      project_name: null,
      key_name: null,
      model_id: 'deepseek-v3.2',
      status_code: 429,
      prompt_tokens: 0,
      completion_tokens: 0,
      cost_usd: 0,
    },
  ],
};

function answer(data: MemberDetail | undefined, more: Record<string, unknown> = {}) {
  state.member.mockReturnValue({
    data,
    error: null,
    isLoading: false,
    refetch: state.refetch,
    ...more,
  });
}

function card(label: string): HTMLElement {
  return screen.getByText(label).closest('div') as HTMLElement;
}

beforeEach(() => {
  state.member.mockReset();
  state.refetch.mockReset();
});
afterEach(cleanup);

describe('what an organization administrator may do to a member', () => {
  it('sets the limit of a member and removes one that is active', () => {
    expect(memberActions(LEI)).toEqual({
      limit: true,
      access: true,
      remove: true,
      restore: false,
    });
  });

  it('restores only a member that the organization removed', () => {
    expect(memberActions({ ...LEI, status: 'suspended', removed: true })).toEqual({
      limit: true,
      access: true,
      remove: false,
      restore: true,
    });
    // Suspended by the platform administrator: not the organization's to undo.
    expect(memberActions({ ...LEI, status: 'suspended', removed: false })).toEqual({
      limit: true,
      access: true,
      remove: false,
      restore: false,
    });
  });

  it('removes no account that waits for approval', () => {
    expect(memberActions({ ...LEI, status: 'pending_approval' })).toEqual({
      limit: true,
      access: true,
      remove: false,
      restore: false,
    });
  });

  it('does nothing to an organization administrator', () => {
    expect(memberActions({ ...LEI, role: 'admin' })).toEqual({
      limit: false,
      access: false,
      remove: false,
      restore: false,
    });
  });

  it('does nothing to a member that the gateway marks as the platform administrator’s', () => {
    // An administrator of the platform: the role in the organization is "member".
    expect(memberActions({ ...LEI, protected: true })).toEqual({
      limit: false,
      access: false,
      remove: false,
      restore: false,
    });
    expect(memberActions({ ...LEI, protected: false })).toEqual({
      limit: true,
      access: true,
      remove: true,
      restore: false,
    });
  });
});

describe('one member, opened in place', () => {
  it('shows the four spend figures, the month against the member’s own limit, and the period’s requests', () => {
    answer(DETAIL);

    render(<MemberDetailPanel member={LEI} period="30d" onAction={vi.fn()} />);

    expect(state.member).toHaveBeenLastCalledWith('u-lei', '30d');
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('Joined Oct 9, 2026 · Last sign-in Never')).toBeInTheDocument();
    expect(card('Today')).toHaveTextContent('$3.20');
    expect(card('Last 7 days')).toHaveTextContent('$24.46');
    expect(card('All-time')).toHaveTextContent('$310.05');
    expect(card('All-time')).toHaveTextContent('Since the member joined');
    expect(card('Requests')).toHaveTextContent('982');
    expect(card('Requests')).toHaveTextContent('Last 30 days');
    expect(card('Tokens')).toHaveTextContent('3.4M');
    expect(card('This month')).toHaveTextContent('$76.70/ $130.00');
    expect(card('This month')).toHaveTextContent('Monthly limit of this member');
  });

  it.each([
    ['default', 40, 'Default limit of the organization', '$76.70/ $40.00'],
    ['platform', 10, "Platform administrator's limit", '$76.70/ $10.00'],
    ['none', null, 'No monthly limit', '$76.70'],
  ] as const)('says whose limit it is: %s', (source, limit, note, figure) => {
    const member = { ...LEI, monthly_limit_source: source, monthly_limit_usd: limit };
    answer({ ...DETAIL, member });

    render(<MemberDetailPanel member={member} period="7d" onAction={vi.fn()} />);

    expect(card('This month')).toHaveTextContent(note);
    expect(card('This month')).toHaveTextContent(figure);
    if (limit === null) expect(card('This month')).not.toHaveTextContent('/');
  });

  it('draws the spend of the last 30 days whatever the period is, and the models of the period', () => {
    answer(DETAIL);

    render(<MemberDetailPanel member={LEI} period="1d" onAction={vi.fn()} />);

    const spend = screen.getByRole('region', { name: 'Spend per day (USD)' });
    expect(spend).toHaveTextContent('Last 30 days');
    expect(within(spend).getByTestId('chart')).toHaveTextContent('Nov 16=3.5 Nov 17=4.25');
    // Short on the axis, in full in the tooltip.
    expect(within(spend).getByTestId('chart')).toHaveAttribute('data-ticks', '$3.5 $4.25');
    expect(within(spend).getByTestId('chart')).toHaveAttribute('data-values', '$3.50 $4.25');

    const models = screen.getByRole('region', { name: 'Spend by model' });
    expect(models).toHaveTextContent('Today (UTC)');
    expect(
      within(models)
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual(['glm-4.6$14.20', 'deepseek-v3.2$10.26']);
  });

  it('names and writes the spend of each day in the currency the gateway sent with it', () => {
    // The page stayed open while the ledger changed: its own configuration
    // still says USD, and the gateway now answers in yuan.
    const member = {
      ...LEI,
      currency: 'CNY' as const,
      cost: 171.2,
      spent_today: 22.4,
      spent_month: 536.9,
      monthly_limit: 910,
    };
    const [first, second] = DETAIL.days;
    answer({
      ...DETAIL,
      member,
      days: [
        { ...first, currency: 'CNY', cost: 24.5 },
        { ...second, currency: 'CNY', cost: 29.75 },
      ],
    });

    render(<MemberDetailPanel member={member} period="7d" onAction={vi.fn()} />);

    // The card of the same answer is in yuan, and so is the figure.
    expect(card('Today')).toHaveTextContent('¥22.40');
    const spend = screen.getByRole('region', { name: 'Spend per day (CNY)' });
    expect(within(spend).getByTestId('chart')).toHaveAttribute('data-values', '¥24.50 ¥29.75');
    fireEvent.click(within(spend).getByRole('button', { name: 'Table' }));
    expect(
      within(spend)
        .getAllByRole('row')
        .map((row) => row.textContent),
    ).toEqual(['Day (UTC)Spend', 'Nov 16¥24.50', 'Nov 17¥29.75']);
  });

  it('lists the last requests without a column for the member, and with no content', () => {
    answer(DETAIL);

    render(<MemberDetailPanel member={LEI} period="7d" onAction={vi.fn()} />);

    const requests = screen.getByRole('region', { name: 'Recent requests' });
    expect(
      within(requests)
        .getAllByRole('columnheader')
        .map((header) => header.textContent),
    ).toEqual([
      'Time',
      'Project',
      'API key',
      'Model',
      'Status',
      'Input tokens',
      'Output tokens',
      'Cost',
    ]);
    const [, first, second] = within(requests).getAllByRole('row');
    for (const text of ['Thesis', 'laptop', 'glm-4.6', 'OK', '1,820', '412']) {
      expect(first).toHaveTextContent(text);
    }
    // A request with no project or key shows a dash, and a refusal its code.
    expect(second).toHaveTextContent('429 Rate limited');
    expect(within(second).getAllByText('—')).toHaveLength(2);
    expect(requests).not.toHaveTextContent('lei.wang@example.edu');
  });

  it('hands the chosen action to the page, with the member as it was just read', () => {
    const fresh = { ...LEI, monthly_limit_usd: 150 };
    answer({ ...DETAIL, member: fresh });
    const onAction = vi.fn();

    render(<MemberDetailPanel member={LEI} period="7d" onAction={onAction} />);
    fireEvent.click(screen.getByRole('button', { name: 'Set monthly limit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Remove from organization' }));

    expect(onAction).toHaveBeenNthCalledWith(1, { mode: 'limit', member: fresh });
    expect(onAction).toHaveBeenNthCalledWith(2, { mode: 'remove', member: fresh });
    expect(screen.queryByRole('button', { name: 'Restore member' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Models' }));
    expect(onAction).toHaveBeenLastCalledWith({ mode: 'models', member: fresh });
  });

  it('says what the account has: a key, a second factor, a verified email', () => {
    answer(DETAIL);

    render(<MemberDetailPanel member={LEI} period="30d" onAction={vi.fn()} />);

    expect(
      screen.getByText('Has an active API key · Two-factor off · Email verified'),
    ).toBeInTheDocument();
  });

  it('lists the projects and the keys by name, with what each spent, and no key value', () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-11-18T12:00:00Z'));
    answer(DETAIL);

    const { container } = render(
      <MemberDetailPanel member={LEI} period="30d" onAction={vi.fn()} />,
    );

    const thesis = screen.getByRole('region', { name: 'Thesis' });
    expect(thesis).toHaveTextContent('$76.70 this month');
    expect(thesis).toHaveTextContent('laptop$70.00 this month · last used 27 min ago');
    // A key without a name, revoked and never used.
    expect(thesis).toHaveTextContent('Unnamed keyrevoked$6.70 this month · never used');
    expect(
      within(screen.getByRole('region', { name: 'Default' })).getByText('No keys.'),
    ).toBeInTheDocument();
    // The gateway sends no value and no prefix of a key, and the list has no place for one.
    expect(container.querySelector('code')).toBeNull();
    vi.useRealTimers();
  });

  it('offers to revoke an active key, and hands the key to the page', () => {
    answer(DETAIL);
    const onAction = vi.fn();

    render(<MemberDetailPanel member={LEI} period="30d" onAction={onAction} />);

    // Only the active key: a revoked key stays revoked.
    expect(screen.getAllByRole('button', { name: /^Revoke / })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Revoke laptop' }));

    expect(onAction).toHaveBeenCalledWith({
      mode: 'revoke-key',
      member: LEI,
      apiKey: DETAIL.keys?.[0],
    });
  });

  it('shows the models that are off, and whose each list is', () => {
    answer(DETAIL);

    render(<MemberDetailPanel member={LEI} period="30d" onAction={vi.fn()} />);

    const models = screen.getByRole('region', { name: 'Models turned off' });
    expect(models).toHaveTextContent('By the organization: glm-4.6');
    expect(models).toHaveTextContent('By the platform administrator: big-model');
  });

  it('says so when no model is off, and reads a gateway that sends none of the new lists', () => {
    const older = { ...DETAIL };
    delete older.account;
    delete older.projects;
    delete older.keys;
    delete older.disabled_models;
    delete older.platform_disabled_models;
    answer(older);

    render(<MemberDetailPanel member={LEI} period="30d" onAction={vi.fn()} />);

    expect(screen.getByText('No model is turned off for this member.')).toBeInTheDocument();
    expect(screen.getByText('Projects and keys')).toBeInTheDocument();
    expect(screen.queryByText(/API key ·/)).not.toBeInTheDocument();
  });

  it('offers to restore a member that the organization removed', () => {
    const removed = { ...LEI, status: 'suspended', removed: true };
    answer({ ...DETAIL, member: removed });
    const onAction = vi.fn();

    render(<MemberDetailPanel member={removed} period="7d" onAction={onAction} />);
    fireEvent.click(screen.getByRole('button', { name: 'Restore member' }));

    expect(onAction).toHaveBeenCalledWith({ mode: 'restore', member: removed });
    // Removed by the organization, which is not the same as suspended by the platform.
    expect(screen.getByText('Removed')).toBeInTheDocument();
    expect(screen.queryByText('Suspended')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Remove from organization' }),
    ).not.toBeInTheDocument();
  });

  it('says who can make an account active again when the platform suspended it', () => {
    const suspended = { ...LEI, status: 'suspended', removed: false };
    answer({ ...DETAIL, member: suspended });

    render(<MemberDetailPanel member={suspended} period="7d" onAction={vi.fn()} />);

    expect(
      screen.getByText(
        'The platform administrator suspended this account, and only the platform administrator can make it active again.',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Restore member' })).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Remove from organization' }),
    ).not.toBeInTheDocument();
  });

  it('gives no control over the keys or the models of an administrator', () => {
    const admin = { ...LEI, role: 'admin' as const, protected: true };
    answer({ ...DETAIL, member: admin });

    render(<MemberDetailPanel member={admin} period="30d" onAction={vi.fn()} />);

    expect(screen.queryByRole('button', { name: 'Models' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Revoke / })).not.toBeInTheDocument();
    // The keys are still listed, by name.
    expect(screen.getByRole('region', { name: 'Thesis' })).toHaveTextContent('laptop');
  });

  it('gives no action on an organization administrator, and says who has them', () => {
    const administrator = { ...LEI, role: 'admin' as const };
    answer({ ...DETAIL, member: administrator });

    render(<MemberDetailPanel member={administrator} period="7d" onAction={vi.fn()} />);

    for (const name of ['Set monthly limit', 'Remove from organization', 'Restore member']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
    }
    expect(
      screen.getByText(
        'The platform administrator sets the limit of an organization administrator and removes one.',
      ),
    ).toBeInTheDocument();
  });

  it('gives no action on a member that is the platform administrator’s, and says who has them', () => {
    const member = { ...LEI, protected: true };
    answer({ ...DETAIL, member });

    render(<MemberDetailPanel member={member} period="7d" onAction={vi.fn()} />);

    // The gateway refuses each of them for this account, so none is offered.
    expect(screen.queryByRole('button', { name: 'Set monthly limit' })).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Remove from organization' }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText('The platform administrator sets the limit of this account and removes it.'),
    ).toBeInTheDocument();
  });

  it('shows the figures of the row at once while the details load', () => {
    answer(undefined, { isLoading: true });

    render(<MemberDetailPanel member={LEI} period="7d" onAction={vi.fn()} />);

    expect(card('Today')).toHaveTextContent('$3.20');
    expect(screen.getByRole('button', { name: 'Set monthly limit' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Loading...');
    expect(screen.queryByRole('region', { name: 'Recent requests' })).not.toBeInTheDocument();
    // What the account has comes with the details, not with the row.
    expect(screen.queryByText(/API key/)).not.toBeInTheDocument();
  });

  it('offers to try again when the details cannot be read', () => {
    answer(undefined, { error: new Error('The gateway is not reachable.') });

    render(<MemberDetailPanel member={LEI} period="7d" onAction={vi.fn()} />);

    expect(screen.getByRole('alert')).toHaveTextContent('The gateway is not reachable.');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(state.refetch).toHaveBeenCalledTimes(1);
  });

  it('says a member with no requests has none', () => {
    answer({
      ...DETAIL,
      days: DETAIL.days.map((day) => ({ ...day, cost_usd: 0 })),
      models: [],
      requests: [],
    });

    render(<MemberDetailPanel member={LEI} period="7d" onAction={vi.fn()} />);

    expect(screen.queryByTestId('chart')).not.toBeInTheDocument();
    expect(screen.getAllByText('Nothing in this period.')).toHaveLength(2);
    expect(screen.getByText('No requests in this period.')).toBeInTheDocument();
  });
});
