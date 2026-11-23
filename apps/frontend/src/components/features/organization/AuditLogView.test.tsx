// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuditEntry, AuditPage } from '@/lib/api/organization';
import { chooseOption } from '@/test-utils/select';

const state = vi.hoisted(() => ({ audit: vi.fn(), refetch: vi.fn() }));
vi.mock('@/lib/hooks', () => ({
  useOrganizationAudit: (query: unknown) => state.audit(query),
}));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: { user: { id: 'u-yu', organization: { id: 'o-1', name: 'Example University' } } },
  }),
}));

import { AuditLogView } from './AuditLogView';

function entry(over: Partial<AuditEntry>): AuditEntry {
  return {
    id: 1,
    timestamp: '2026-11-18T12:00:00Z',
    action: 'organization.member_limit.set',
    category: 'limits',
    actor: 'member',
    actor_user_id: 'u-yu',
    actor_email: 'yu.chen@example.edu',
    target_type: 'user',
    target_user_id: 'u-lei',
    target_email: 'lei.wang@example.edu',
    details: { email: 'lei.wang@example.edu', old: '80.0000', new: '90.0000', currency: 'USD' },
    ...over,
  };
}

const ENTRIES: AuditEntry[] = [
  entry({ id: 4 }),
  entry({
    id: 3,
    action: 'organization.member.remove',
    category: 'membership',
    target_user_id: 'u-mei',
    target_email: 'mei.song@example.edu',
    details: { email: 'mei.song@example.edu' },
  }),
  entry({
    id: 2,
    action: 'organization.limit.set',
    actor: 'platform_administrator',
    actor_user_id: null,
    actor_email: null,
    target_type: 'organization',
    target_user_id: null,
    target_email: null,
    details: { old: null, new: '1000.0000', currency: 'USD' },
  }),
  entry({
    id: 1,
    action: 'organization.member.join',
    category: 'membership',
    actor: 'system',
    actor_user_id: null,
    actor_email: null,
    details: { sso_provider: 'example-univ' },
  }),
];

function answer(page: AuditPage | undefined, more: Record<string, unknown> = {}) {
  state.audit.mockReturnValue({
    data: page,
    error: null,
    isLoading: false,
    isPlaceholderData: false,
    refetch: state.refetch,
    ...more,
  });
}

/** The query the log was last asked with. */
function asked(): Record<string, unknown> {
  return state.audit.mock.lastCall?.[0] as Record<string, unknown>;
}

/** The cells of each row after the time, which is in the reader's time zone. */
function rows(): string[][] {
  return screen
    .getAllByRole('row')
    .slice(1)
    .map((row) =>
      [...row.querySelectorAll('td')].slice(1, 5).map((cell) => cell.textContent ?? ''),
    );
}

beforeEach(() => {
  state.audit.mockReset();
  state.refetch.mockReset();
});
afterEach(cleanup);

describe('the audit log of the organization', () => {
  it('opens on every row, the newest first', () => {
    answer({ total: 4, entries: ENTRIES });

    render(<AuditLogView />);

    // No period: the log is short, and its rows are rare.
    expect(asked()).toEqual({
      period: undefined,
      category: undefined,
      member: undefined,
      search: undefined,
      limit: 50,
      offset: 0,
    });
    expect(screen.getByRole('button', { name: 'All' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
      'Time',
      'Actor',
      'Event',
      'Target',
      'Details',
      // The last column opens a row; its name is for a screen reader.
      'Every field',
    ]);
  });

  it('says who did what to whom, and what changed', () => {
    answer({ total: 4, entries: ENTRIES });

    render(<AuditLogView />);

    expect(rows()).toEqual([
      [
        'yu.chen@example.edu',
        'Member monthly limit changed',
        'lei.wang@example.edu',
        '$80.00 to $90.00',
      ],
      [
        'yu.chen@example.edu',
        'Member removed',
        'mei.song@example.edu',
        'Account suspended; sessions ended, API keys revoked',
      ],
      // A platform administrator is named as that, without its account.
      [
        'Platform administrator',
        'Organization monthly limit changed',
        'Example University',
        'No limit to $1,000.00',
      ],
      ['System', 'Member joined', 'lei.wang@example.edu', 'Signed in through example-univ'],
    ]);
  });

  it('shows a dash for a row with nothing to add, and an action it does not know as it is', () => {
    answer({
      total: 1,
      entries: [entry({ action: 'organization.something.new', details: null })],
    });

    render(<AuditLogView />);

    expect(rows()).toEqual([
      ['yu.chen@example.edu', 'organization.something.new', 'lei.wang@example.edu', '—'],
    ]);
  });

  it('says which models an action turned off and which it turned on again, and which key was revoked', () => {
    answer({
      total: 4,
      entries: [
        entry({
          id: 7,
          action: 'organization.member_models.set',
          category: 'access',
          details: {
            email: 'lei.wang@example.edu',
            // glm-4.6 was off before and stays off: it is not what this action did.
            disabled_models: { old: ['glm-4.6'], new: ['glm-4.6', 'qwen-max'] },
          },
        }),
        entry({
          id: 6,
          action: 'organization.member_models.set',
          category: 'access',
          details: {
            email: 'lei.wang@example.edu',
            disabled_models: { old: ['glm-4.6'], new: [] },
          },
        }),
        entry({
          id: 8,
          action: 'organization.member_models.set',
          category: 'access',
          details: {
            email: 'lei.wang@example.edu',
            disabled_models: { old: ['glm-4.6', 'big'], new: ['big', 'qwen-max', 'small'] },
          },
        }),
        entry({
          id: 5,
          action: 'organization.member_key.revoke',
          category: 'access',
          details: { email: 'lei.wang@example.edu', key_name: 'laptop', project_name: 'Thesis' },
        }),
      ],
    });

    render(<AuditLogView />);

    expect(rows().map((row) => [row[1], row[3]])).toEqual([
      ['Member models changed', 'Turned off: qwen-max'],
      ['Member models changed', 'Turned on again: glm-4.6'],
      ['Member models changed', 'Turned off: qwen-max, small; turned on again: glm-4.6'],
      ['API key revoked', 'laptop, in project Thesis'],
    ]);
  });

  it('opens a row on every field that the action recorded, and on no source address', () => {
    answer({
      total: 1,
      entries: [
        entry({
          id: 7,
          action: 'organization.member_models.set',
          category: 'access',
          details: {
            email: 'lei.wang@example.edu',
            disabled_models: { old: ['glm-4.6'], new: ['glm-4.6', 'qwen-max'] },
          },
        }),
      ],
    });

    render(<AuditLogView />);
    const toggle = screen.getByRole('button', {
      name: /^Show every field of Member models changed/,
    });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const opened = screen.getAllByRole('row')[2];
    // Each field of the details, in full: the old list and the new one.
    expect(within(opened).getByText('disabled_models').nextElementSibling?.textContent).toBe(
      JSON.stringify({ old: ['glm-4.6'], new: ['glm-4.6', 'qwen-max'] }, null, 2),
    );
    expect(within(opened).getByText('email').nextElementSibling).toHaveTextContent(
      'lei.wang@example.edu',
    );
    for (const label of ['Event', 'Time', 'Actor', 'Target']) {
      expect(within(opened).getByText(label)).toBeInTheDocument();
    }
    expect(opened).not.toHaveTextContent(/source/i);
    expect(within(opened).queryByText('Raw JSON')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /^Hide every field of/ }));
    expect(screen.getAllByRole('row')).toHaveLength(2);
  });

  it('narrows to the member of a row, as target or as actor, and back', () => {
    answer({ total: 120, entries: ENTRIES });

    render(<AuditLogView />);
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('button', { name: 'mei.song@example.edu' }));

    expect(asked()).toMatchObject({ member: 'u-mei', offset: 0 });
    expect(screen.getByText('Only the events of mei.song@example.edu')).toBeInTheDocument();
    // The member the log is narrowed to is plain text in its rows.
    expect(screen.queryByRole('button', { name: 'mei.song@example.edu' })).not.toBeInTheDocument();

    // An actor that is a member narrows too.
    fireEvent.click(screen.getAllByRole('button', { name: 'yu.chen@example.edu' })[0]);
    expect(asked()).toMatchObject({ member: 'u-yu' });

    fireEvent.click(screen.getByRole('button', { name: 'Show every member' }));
    expect(asked()).toMatchObject({ member: undefined });
    expect(screen.queryByText(/^Only the events of/)).not.toBeInTheDocument();
  });

  it('gives no such control for the platform administrator, the system or the organization', () => {
    answer({ total: 4, entries: ENTRIES });

    render(<AuditLogView />);

    for (const name of ['Platform administrator', 'System', 'Example University']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
    }
  });

  it('narrows to a kind of event, from the first page', () => {
    answer({ total: 120, entries: ENTRIES });

    render(<AuditLogView />);
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(asked()).toMatchObject({ offset: 50 });

    chooseOption('Event', 'Limits');
    expect(asked()).toMatchObject({ category: 'limits', offset: 0 });

    chooseOption('Event', 'Membership');
    expect(asked()).toMatchObject({ category: 'membership' });

    chooseOption('Event', 'Access');
    expect(asked()).toMatchObject({ category: 'access' });

    chooseOption('Event', 'All events');
    expect(asked()).toMatchObject({ category: undefined });
  });

  it('narrows to a period and back to every row, from the first page', () => {
    answer({ total: 120, entries: ENTRIES });

    render(<AuditLogView />);
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('button', { name: 'Last 7 days' }));

    expect(asked()).toMatchObject({ period: '7d', offset: 0 });
    expect(screen.getByRole('button', { name: 'All' })).toHaveAttribute('aria-pressed', 'false');

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('button', { name: 'All' }));
    expect(asked()).toMatchObject({ period: undefined, offset: 0 });
  });

  it('searches once the typing pauses, from the first page', async () => {
    answer({ total: 120, entries: ENTRIES });

    render(<AuditLogView />);
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search the log' }), {
      target: { value: ' lei ' },
    });
    expect(asked()).toMatchObject({ search: undefined, offset: 50 });

    await waitFor(() => expect(asked()).toMatchObject({ search: 'lei', offset: 0 }));
  });

  it('keeps the page when the text changes and the search does not', async () => {
    answer({ total: 120, entries: [entry({})] });

    render(<AuditLogView />);
    // At once: the page must also stay when the log has only just opened.
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(asked()).toMatchObject({ search: undefined, offset: 50 });

    // Spaces are not a search.
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search the log' }), {
      target: { value: '  ' },
    });
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(asked()).toMatchObject({ search: undefined, offset: 50 });
  });

  it('goes back to the last page when the log gets shorter than the page it is on', async () => {
    answer({ total: 120, entries: [entry({})] });
    const { rerender } = render(<AuditLogView />);
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(asked()).toMatchObject({ offset: 100 });

    // Rows left the period that is shown: 60 are left, so the third page is gone.
    answer({ total: 60, entries: [] });
    rerender(<AuditLogView />);

    // Not "nothing has been recorded", with no way back to the rows.
    await waitFor(() => expect(asked()).toMatchObject({ offset: 50 }));
  });

  it('pages through a long log and says where the page is', () => {
    answer({ total: 120, entries: ENTRIES });

    render(<AuditLogView />);

    expect(screen.getByText('Showing 1–4 of 120')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(asked()).toMatchObject({ offset: 50 });
    expect(screen.getByText('Showing 51–54 of 120')).toBeInTheDocument();
  });

  it('says an empty log is empty, and a filter that matches nothing matches nothing', () => {
    answer({ total: 0, entries: [] });

    render(<AuditLogView />);
    expect(screen.getByText('Nothing has been recorded yet.')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Today (UTC)' }));
    expect(screen.getByText('No event matches these filters.')).toBeInTheDocument();
  });

  it('offers to try again when the log cannot be read', () => {
    answer(undefined, { error: new Error('The gateway is not reachable.') });

    render(<AuditLogView />);

    expect(screen.getByRole('alert')).toHaveTextContent('The gateway is not reachable.');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(state.refetch).toHaveBeenCalledTimes(1);
  });

  it('shows that it is loading, with the filters already there', () => {
    answer(undefined, { isLoading: true });

    render(<AuditLogView />);

    expect(screen.getByRole('status')).toHaveTextContent('Loading...');
    expect(screen.getByRole('searchbox', { name: 'Search the log' })).toBeInTheDocument();
  });
});
