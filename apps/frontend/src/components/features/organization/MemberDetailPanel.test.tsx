// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { MemberDetail, OrganizationMember } from '@/lib/api/organization';

// The chart is loaded with next/dynamic; jsdom cannot lay out recharts anyway.
vi.mock('next/dynamic', () => ({
  default: () => (props: { points: Array<{ label: string; value: number }> }) => (
    <div data-testid="chart">
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
  monthly_limit_usd: 130,
  monthly_limit_source: 'member',
};

const DETAIL: MemberDetail = {
  member: LEI,
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
    expect(memberActions(LEI)).toEqual({ limit: true, remove: true, restore: false });
  });

  it('restores only a member that the organization removed', () => {
    expect(memberActions({ ...LEI, status: 'suspended', removed: true })).toEqual({
      limit: true,
      remove: false,
      restore: true,
    });
    // Suspended by the platform administrator: not the organization's to undo.
    expect(memberActions({ ...LEI, status: 'suspended', removed: false })).toEqual({
      limit: true,
      remove: false,
      restore: false,
    });
  });

  it('removes no account that waits for approval', () => {
    expect(memberActions({ ...LEI, status: 'pending_approval' })).toEqual({
      limit: true,
      remove: false,
      restore: false,
    });
  });

  it('does nothing to an organization administrator', () => {
    expect(memberActions({ ...LEI, role: 'admin' })).toEqual({
      limit: false,
      remove: false,
      restore: false,
    });
  });
});

describe('one member, opened in place', () => {
  it('shows the figures of the period and the month against the member’s own limit', () => {
    answer(DETAIL);

    render(<MemberDetailPanel member={LEI} period="7d" onAction={vi.fn()} />);

    expect(state.member).toHaveBeenLastCalledWith('u-lei', '7d');
    expect(screen.getByText('Joined Oct 9, 2026 · Last sign-in Never')).toBeInTheDocument();
    expect(card('Spend')).toHaveTextContent('$24.46');
    expect(card('Spend')).toHaveTextContent('Last 7 days');
    expect(card('Requests')).toHaveTextContent('982');
    expect(card('Tokens')).toHaveTextContent('3.4M');
    expect(card('This month')).toHaveTextContent('$76.70/ $130.00');
    expect(card('This month')).toHaveTextContent('Monthly limit of this member');
  });

  it.each([
    ['default', 40, 'Default limit of the organization', '$76.70/ $40.00'],
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

    const models = screen.getByRole('region', { name: 'Spend by model' });
    expect(models).toHaveTextContent('Today (UTC)');
    expect(
      within(models)
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual(['glm-4.6$14.20', 'deepseek-v3.2$10.26']);
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
  });

  it('offers to restore a member that the organization removed', () => {
    const removed = { ...LEI, status: 'suspended', removed: true };
    answer({ ...DETAIL, member: removed });
    const onAction = vi.fn();

    render(<MemberDetailPanel member={removed} period="7d" onAction={onAction} />);
    fireEvent.click(screen.getByRole('button', { name: 'Restore member' }));

    expect(onAction).toHaveBeenCalledWith({ mode: 'restore', member: removed });
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

  it('shows the figures of the row at once while the details load', () => {
    answer(undefined, { isLoading: true });

    render(<MemberDetailPanel member={LEI} period="7d" onAction={vi.fn()} />);

    expect(card('Spend')).toHaveTextContent('$24.46');
    expect(screen.getByRole('button', { name: 'Set monthly limit' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Loading...');
    expect(screen.queryByRole('region', { name: 'Recent requests' })).not.toBeInTheDocument();
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
