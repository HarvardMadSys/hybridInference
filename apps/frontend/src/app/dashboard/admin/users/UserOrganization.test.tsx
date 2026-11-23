// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { chooseOption } from '@/test-utils/select';

vi.mock('@/lib/api/admin', () => ({
  getUserDetail: vi.fn(),
  getUserProjects: vi.fn(() => new Promise(() => {})),
  getUserFilterProviders: vi.fn(async () => ({ providers: [] })),
  listModelVisibility: vi.fn(async () => ({ models: [] })),
  listAdminOrganizations: vi.fn(),
  listUsers: vi.fn(),
  updateUser: vi.fn(),
}));
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));

import { getUserDetail, listAdminOrganizations, listUsers } from '@/lib/api/admin';
import type { UserDetail } from '@/lib/api/admin';
import { FilterBar } from './FilterBar';
import { OrganizationFilter } from './filters/OrganizationFilter';
import { useOrganizations } from './hooks/useOrganizations';
import { useUsers } from './hooks/useUsers';
import { DEFAULT_FILTER_STATE, filterStateFromUrl, filterStateToUrl } from './lib/filterTypes';
import type { UserRow } from './types';
import { UserTable } from './UserTable';

const ORGANIZATIONS = [
  { id: 'o-1', name: 'Example University' },
  { id: 'o-2', name: 'Example Labs' },
];

const USER: UserRow = {
  id: 'user-1',
  email: 'lei.wang@example.edu',
  user_name: 'Lei Wang',
  role: 'free',
  status: 'active',
  email_verified: true,
  approval_note: null,
  reviewed_at: null,
  reviewed_by: null,
  signup_reason: null,
  admin_note: null,
  created_at: '2026-01-01T00:00:00Z',
  last_login_at: null,
  has_key: true,
  key_prefix: 'hyi_123',
  key_status: 'active',
  usage_today_usd: '0',
  usage_month_usd: '0',
  usage_alltime_usd: '0',
  usage_alltime_requests: 0,
  usage_alltime_tokens: 0,
};

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function table(users: UserRow[], showOrganization: boolean) {
  const nothing = vi.fn(async () => undefined);
  return render(
    <UserTable
      users={users}
      costHistories={{}}
      turnAverages={{}}
      askQuestionFractions={{}}
      automationScores={{}}
      scoreState="idle"
      scoreSortDir={null}
      onScoreHeader={vi.fn()}
      density="comfortable"
      filterState={DEFAULT_FILTER_STATE}
      onSortChange={vi.fn()}
      showOrganization={showOrganization}
      onApprove={nothing}
      onApproveMany={nothing}
      onReject={nothing}
      onUpdate={nothing}
      onSuspend={nothing}
      onResume={nothing}
      onDelete={nothing}
      onHardDelete={nothing}
    />,
  );
}

beforeEach(() => {
  vi.mocked(listAdminOrganizations).mockReset().mockResolvedValue(ORGANIZATIONS);
  vi.mocked(listUsers)
    .mockReset()
    .mockResolvedValue({ total: 0, users: [] } as never);
  vi.mocked(getUserDetail).mockReset();
});
afterEach(cleanup);

describe('the organization filter of the Users tab', () => {
  it('offers every organization, and the accounts that have none', () => {
    const onChange = vi.fn();
    render(<OrganizationFilter organizations={ORGANIZATIONS} value={null} onChange={onChange} />);

    chooseOption('Filter by organization', 'Example Labs');
    expect(onChange).toHaveBeenLastCalledWith('o-2');
    chooseOption('Filter by organization', 'No organization');
    expect(onChange).toHaveBeenLastCalledWith('none');
  });

  it('goes back to every account', () => {
    const onChange = vi.fn();
    render(<OrganizationFilter organizations={ORGANIZATIONS} value="o-1" onChange={onChange} />);

    chooseOption('Filter by organization', 'Any organization');
    expect(onChange).toHaveBeenLastCalledWith(null);
  });

  it('shows a filter from a link that names an organization the list does not have', () => {
    render(<OrganizationFilter organizations={[]} value="o-gone" onChange={vi.fn()} />);

    expect(screen.getByRole('combobox', { name: 'Filter by organization' })).toHaveTextContent(
      'o-gone',
    );
  });

  it('is in the filter bar only on a deployment that has organizations', () => {
    const bar = (organizations: typeof ORGANIZATIONS) => (
      <FilterBar
        state={DEFAULT_FILTER_STATE}
        onChange={vi.fn()}
        density="comfortable"
        onDensityChange={vi.fn()}
        hiddenColumns={[]}
        onHiddenColumnsChange={vi.fn()}
        organizations={organizations}
      />
    );
    const { rerender } = render(bar([]));
    expect(
      screen.queryByRole('combobox', { name: 'Filter by organization' }),
    ).not.toBeInTheDocument();

    rerender(bar(ORGANIZATIONS));
    expect(screen.getByRole('combobox', { name: 'Filter by organization' })).toBeInTheDocument();
  });

  it('keeps the organization in the address, and reads it back', () => {
    const url = filterStateToUrl({ ...DEFAULT_FILTER_STATE, organization: 'none' });

    expect(url).toBe('organization_id=none');
    expect(filterStateFromUrl(new URLSearchParams(url)).organization).toBe('none');
    expect(filterStateFromUrl(new URLSearchParams('')).organization).toBeNull();
  });

  it('asks the gateway for the members of the organization', async () => {
    const { result } = renderHookWith(() =>
      useUsers({ ...DEFAULT_FILTER_STATE, organization: 'o-1' }),
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(vi.mocked(listUsers).mock.lastCall?.[0]).toMatchObject({ organizationId: 'o-1' });
  });

  it('reads the organizations once, and has none when the read fails', async () => {
    const named = renderHookWith(() => useOrganizations());
    await waitFor(() => expect(named.result.current).toEqual(ORGANIZATIONS));

    vi.mocked(listAdminOrganizations).mockRejectedValue(new Error('not reachable'));
    const failed = renderHookWith(() => useOrganizations());
    await waitFor(() => expect(listAdminOrganizations).toHaveBeenCalledTimes(2));
    expect(failed.result.current).toEqual([]);
  });
});

describe('the organization of an account in the Users table', () => {
  const member = {
    ...USER,
    organization: { id: 'o-1', name: 'Example University', role: 'member' as const },
  };
  const administrator = {
    ...USER,
    id: 'user-2',
    email: 'yu.chen@example.edu',
    organization: { id: 'o-1', name: 'Example University', role: 'admin' as const },
  };
  const personal = { ...USER, id: 'user-3', email: 'personal@mail.example.org' };

  it('has a column for it after the status, with the role of an administrator', () => {
    table([member, administrator, personal], true);

    const headers = screen.getAllByRole('columnheader').map((cell) => cell.textContent);
    expect(headers.indexOf('Organization')).toBe(headers.indexOf('Status') + 1);
    const cell = (email: string) =>
      (screen.getByText(email).closest('tr') as HTMLElement).children[2] as HTMLElement;
    expect(cell('lei.wang@example.edu')).toHaveTextContent('Example University');
    expect(cell('lei.wang@example.edu')).not.toHaveTextContent('Administrator');
    expect(cell('yu.chen@example.edu')).toHaveTextContent('Example UniversityAdministrator');
    // An account of no organization.
    expect(cell('personal@mail.example.org')).toHaveTextContent('—');
  });

  it('has no such column on a deployment without organizations', () => {
    table([personal], false);

    expect(screen.queryByRole('columnheader', { name: 'Organization' })).not.toBeInTheDocument();
  });

  it('names the organization and the role in the details of an account', async () => {
    vi.mocked(getUserDetail).mockResolvedValue({
      ...administrator,
      models_used: [],
      disabled_models: [],
      usage_today_usd: 0,
      usage_today_requests: 0,
      usage_month_usd: 0,
      usage_month_requests: 0,
      last_request_at: null,
    } as unknown as UserDetail);
    table([administrator], true);

    fireEvent.click(screen.getByText('yu.chen@example.edu'));

    const label = await screen.findByText('Organization', { selector: 'span' });
    const line = label.parentElement as HTMLElement;
    expect(within(line).getByText('Example University')).toBeInTheDocument();
    expect(within(line).getByText('Organization administrator')).toBeInTheDocument();
  });
});

function renderHookWith<T>(hook: () => T) {
  const result = { current: undefined as T };
  function Probe() {
    result.current = hook();
    return null;
  }
  render(<Probe />, { wrapper });
  return { result };
}
