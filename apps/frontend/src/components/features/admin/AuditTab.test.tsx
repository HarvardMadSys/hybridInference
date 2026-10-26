// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const replace = vi.fn();
let search = new URLSearchParams();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace }),
  usePathname: () => '/dashboard/admin/log',
  useSearchParams: () => search,
}));

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('@/lib/api/admin', () => ({
  listAuditLog: vi.fn(),
}));

import { listAuditLog, type AuditLogEntry, type ListAuditLogResponse } from '@/lib/api/admin';
import { AuditTab } from './AuditTab';
import { chooseOption } from '@/test-utils/select';
import { escapingScreenReaderText } from './__tests__/layoutContainment';

const USER_ID = '0123456789abcdef0123';
const ADMIN_ID = 'fedcba9876543210fedc';

function entry(over: Partial<AuditLogEntry> & Pick<AuditLogEntry, 'id' | 'action'>): AuditLogEntry {
  return {
    timestamp: '2026-10-22T10:00:00Z',
    admin_ip: '10.0.0.1',
    target_user_id: null,
    details: null,
    success: true,
    ...over,
  };
}

function page(...entries: AuditLogEntry[]): ListAuditLogResponse {
  return { total: entries.length, entries };
}

/** The table row of the entry whose action reads `action`. */
async function rowOf(action: string): Promise<HTMLElement> {
  const badge = await screen.findByText(action, { selector: 'span[title]' });
  return badge.closest('tr')!;
}

/** The row's Actor, Target and Source cells, by their column. */
function cellsOf(row: HTMLElement) {
  const headers = screen.getAllByRole('columnheader').map((h) => h.textContent);
  const cells = within(row).getAllByRole('cell');
  return {
    actor: cells[headers.indexOf('Actor')],
    target: cells[headers.indexOf('Target')],
    source: cells[headers.indexOf('Source')],
  };
}

/** Opens a row's details and returns them. */
function openDetails(action: string): HTMLElement {
  const toggle = screen.getByRole('button', { name: new RegExp(`Show details of ${action}`) });
  fireEvent.click(toggle);
  return document.getElementById(toggle.getAttribute('aria-controls')!)!;
}

/** The value a details panel records under `label`. */
function detail(details: HTMLElement, label: string): HTMLElement | null {
  const term = within(details).queryByText(label, { selector: 'dt' });
  return (term?.nextElementSibling as HTMLElement | null) ?? null;
}

beforeEach(() => {
  vi.clearAllMocks();
  search = new URLSearchParams();
  vi.mocked(listAuditLog).mockResolvedValue({
    total: 2,
    entries: [
      {
        id: 1,
        timestamp: '2026-10-22T10:00:00Z',
        admin_ip: '10.0.0.1',
        action: 'update_user',
        target_user_id: USER_ID,
        details: { role: 'pro' },
        success: true,
      },
      {
        id: 2,
        timestamp: '2026-10-22T11:00:00Z',
        admin_ip: '10.0.0.2',
        action: 'delete_key',
        target_user_id: null,
        details: null,
        success: false,
      },
    ],
  });
});

afterEach(cleanup);

describe('AuditTab', () => {
  it('labels its filters and shows each entry with its outcome and origin', async () => {
    render(<AuditTab />);

    expect(await screen.findByText('Update user')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Filter by action' })).toHaveTextContent(
      'All actions',
    );
    expect(screen.getByLabelText('Filter by affected user ID')).toHaveValue('');
    expect(screen.getByText('2 entries')).toBeInTheDocument();
    expect(screen.getByText('FAILED')).toBeInTheDocument();
    expect(screen.getByText('10.0.0.1')).toBeInTheDocument();
    expect(screen.getByText('10.0.0.2')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Audit log' })).toBeInTheDocument();
    // The hidden "Details" header and full user IDs stay inside the table's
    // own scroller on a narrow screen instead of widening the page.
    expect(escapingScreenReaderText(screen.getByRole('region', { name: 'Audit log' }))).toEqual([]);
  });

  it('filters by the target user and by action through the URL', async () => {
    vi.mocked(listAuditLog).mockResolvedValue(
      page(
        entry({
          id: 1,
          action: 'update_user',
          actor_type: 'admin',
          actor_id: ADMIN_ID,
          target_type: 'user',
          target_id: USER_ID,
          target_user_id: USER_ID,
          source_ip: '10.0.0.1',
        }),
      ),
    );
    render(<AuditTab />);

    fireEvent.click(
      await screen.findByRole('button', { name: `Show entries affecting user ${USER_ID}` }),
    );
    expect(replace).toHaveBeenLastCalledWith(`/dashboard/admin/log?user_id=${USER_ID}`, {
      scroll: false,
    });

    chooseOption('Filter by action', 'Delete key');
    expect(replace).toHaveBeenLastCalledWith('/dashboard/admin/log?action=delete_key', {
      scroll: false,
    });
  });

  it('separates a signed-in actor from the affected user and source address', async () => {
    vi.mocked(listAuditLog).mockResolvedValue(
      page(
        entry({
          id: 1,
          action: 'update_user',
          actor_type: 'admin',
          actor_id: ADMIN_ID,
          target_type: 'user',
          target_id: USER_ID,
          target_user_id: USER_ID,
          source_ip: '192.0.2.10',
          admin_ip: '192.0.2.10',
        }),
      ),
    );
    render(<AuditTab />);
    const { actor, target, source } = cellsOf(await rowOf('Update user'));
    expect(actor).toHaveTextContent('Administrator');
    expect(within(actor).getByTitle(ADMIN_ID)).toHaveTextContent(ADMIN_ID);
    expect(within(actor).queryByRole('button')).not.toBeInTheDocument();
    expect(target).toHaveTextContent(USER_ID);
    expect(target).not.toHaveTextContent(ADMIN_ID);
    expect(source).toHaveTextContent('192.0.2.10');
    const details = openDetails('Update user');
    expect(detail(details, 'Actor ID')).toHaveTextContent(ADMIN_ID);
    expect(detail(details, 'Target ID')).toHaveTextContent(USER_ID);
    expect(detail(details, 'Source address')).toHaveTextContent('192.0.2.10');
    expect(detail(details, 'Legacy source')).toBeNull();
  });

  it.each([
    ['admin_token', 'Shared admin token', null],
    ['user', 'User (self-service)', USER_ID],
    ['anonymous', 'Anonymous registration', null],
    ['bootstrap', 'Initial setup', null],
    ['system', 'System', null],
  ] as const)(
    'renders the recorded %s actor without inventing an account',
    async (type, label, id) => {
      vi.mocked(listAuditLog).mockResolvedValue(
        page(
          entry({
            id: 1,
            action: 'create_user',
            actor_type: type,
            actor_id: id,
            source_ip: null,
            admin_ip: 'unknown',
          }),
        ),
      );
      render(<AuditTab />);
      const { actor, source } = cellsOf(await rowOf('Create user'));
      expect(actor).toHaveTextContent(label);
      if (id) expect(actor).toHaveTextContent(id);
      if (type === 'admin_token') expect(actor).toHaveTextContent('No account');
      expect(source).toHaveTextContent('Not recorded');
      expect(source).not.toHaveTextContent('Legacy record');
      const details = openDetails('Create user');
      expect(detail(details, 'Actor')).toHaveTextContent(label);
      if (id) expect(detail(details, 'Actor ID')).toHaveTextContent(id);
      else expect(detail(details, 'Actor ID')).toBeNull();
      expect(detail(details, 'Source address')).toHaveTextContent('Not recorded');
      expect(detail(details, 'Legacy source')).toBeNull();
    },
  );

  it.each(['ops@example.test', '192.0.2.11', 'legacy-login', 'unknown'])(
    'shows raw legacy source %s without inferring an actor or target type',
    async (raw) => {
      vi.mocked(listAuditLog).mockResolvedValue(
        page(
          entry({
            id: 1,
            action: 'providers.disabled.update',
            admin_ip: raw,
            actor_type: null,
            actor_id: null,
            source_ip: null,
            target_type: null,
            target_id: null,
            target_user_id: 'old-provider',
            details: { admin_id: 'old-display-name', actor: 'admin' },
          }),
        ),
      );
      render(<AuditTab />);
      const { actor, target, source } = cellsOf(await rowOf('Providers disabled update'));
      expect(actor).toHaveTextContent('Not recorded');
      expect(actor).not.toHaveTextContent('old-display-name');
      expect(target).toHaveTextContent('Type not recorded');
      expect(target).toHaveTextContent('old-provider');
      expect(within(target).queryByRole('button')).not.toBeInTheDocument();
      expect(source).toHaveTextContent(raw);
      expect(source).toHaveTextContent('Legacy record');
      const details = openDetails('Providers disabled update');
      expect(detail(details, 'Actor ID')).toBeNull();
      expect(detail(details, 'Legacy source')).toHaveTextContent(raw);
      expect(detail(details, 'Recorded target ID')).toHaveTextContent('old-provider');
      expect(detail(details, 'Affected user')).toBeNull();
    },
  );

  it('renders model and provider targets without offering a user filter', async () => {
    vi.mocked(listAuditLog).mockResolvedValue(
      page(
        entry({
          id: 1,
          action: 'model.update',
          actor_type: 'admin',
          actor_id: ADMIN_ID,
          target_type: 'model',
          target_id: 'model-a',
        }),
        entry({
          id: 2,
          action: 'provider.update',
          actor_type: 'admin_token',
          target_type: 'provider',
          target_id: 'provider-a',
        }),
      ),
    );
    render(<AuditTab />);
    for (const [action, label, id] of [
      ['Model update', 'Model', 'model-a'],
      ['Provider update', 'Provider', 'provider-a'],
    ]) {
      const { target } = cellsOf(await rowOf(action));
      expect(target).toHaveTextContent(label);
      expect(target).toHaveTextContent(id);
      expect(within(target).queryByRole('button')).not.toBeInTheDocument();
    }
    expect(
      screen.queryByRole('button', { name: /Show entries affecting user/ }),
    ).not.toBeInTheDocument();
    const details = openDetails('Model update');
    expect(detail(details, 'Target ID')).toHaveTextContent('model-a');
    expect(detail(details, 'Affected user')).toBeNull();
  });

  it('sums each change up in one line and discloses the whole record on request', async () => {
    vi.mocked(listAuditLog).mockResolvedValue({
      total: 1,
      entries: [
        {
          id: 7,
          timestamp: '2026-10-22T10:00:00Z',
          admin_ip: 'unknown',
          action: 'settings.update',
          target_user_id: null,
          details: {
            key: 'user_concurrency_free',
            old_value: 4,
            new_value: 3,
            note: 'x'.repeat(200),
          },
          success: true,
        },
      ],
    });
    render(<AuditTab />);

    expect(await screen.findByText('Settings update')).toHaveAttribute('title', 'settings.update');
    expect(screen.getByText('user_concurrency_free')).toBeInTheDocument();
    expect(screen.queryByText('Raw JSON')).not.toBeInTheDocument();

    const toggle = screen.getByRole('button', { name: /Show details of Settings update/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const details = document.getElementById(toggle.getAttribute('aria-controls')!)!;
    expect(within(details).getByText('settings.update')).toBeInTheDocument();
    expect(within(details).getByText('Succeeded')).toBeInTheDocument();
    // Long values are shown in full in the details, not cut short.
    expect(within(details).getByText('x'.repeat(200))).toBeInTheDocument();
    expect(within(details).getByText('Raw JSON')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Hide details of Settings update/ }));
    expect(screen.queryByText('Raw JSON')).not.toBeInTheDocument();
  });

  it('shows a filter set by a link even when the list does not offer it', async () => {
    search = new URLSearchParams('action=settings.update');
    render(<AuditTab />);
    await screen.findByText('Update user');

    expect(listAuditLog).toHaveBeenCalledWith('settings.update', undefined, 50, 0);
    const filter = screen.getByRole('combobox', { name: 'Filter by action' });
    expect(filter).toHaveTextContent('Settings update');
    expect(screen.getByText('2 matching entries')).toBeInTheDocument();

    // The list keeps its groups, and offers the link's filter as selected.
    fireEvent.click(filter);
    const known = screen.getByRole('group', { name: 'Users and API keys' });
    expect(within(known).getAllByRole('option')).toHaveLength(13);
    const current = screen.getByRole('group', { name: 'Current filter' });
    expect(within(current).getByRole('option', { name: 'Settings update' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    // Choosing it again changes nothing.
    fireEvent.click(within(current).getByRole('option', { name: 'Settings update' }));
    expect(replace).not.toHaveBeenCalled();
  });

  it('says when filters match nothing and clears them', async () => {
    search = new URLSearchParams('user_id=nobody&offset=50');
    vi.mocked(listAuditLog).mockResolvedValue({ total: 0, entries: [] });
    render(<AuditTab />);

    expect(await screen.findByText('No entries match these filters.')).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole('button', { name: 'Clear filters' })[0]);
    expect(replace).toHaveBeenLastCalledWith('/dashboard/admin/log', { scroll: false });
  });

  it('reports a failed load in place and retries it', async () => {
    vi.mocked(listAuditLog).mockRejectedValueOnce(new Error('gateway down'));
    render(<AuditTab />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('gateway down');
    fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText('Update user')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(listAuditLog).toHaveBeenCalledTimes(2);
  });

  it('withdraws a failed load’s alert as soon as the next request starts', async () => {
    vi.mocked(listAuditLog).mockRejectedValueOnce(new Error('gateway down'));
    const { rerender } = render(<AuditTab />);
    expect(await screen.findByRole('alert')).toHaveTextContent('gateway down');

    // A new filter whose request is still in flight: the failure was not its.
    let resolve!: (page: ListAuditLogResponse) => void;
    vi.mocked(listAuditLog).mockImplementationOnce(() => new Promise((r) => (resolve = r)));
    search = new URLSearchParams('action=delete_key');
    rerender(<AuditTab />);
    await waitFor(() =>
      expect(listAuditLog).toHaveBeenLastCalledWith('delete_key', undefined, 50, 0),
    );
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByText('Loading audit log…')).toBeInTheDocument();

    resolve({ total: 0, entries: [] });
    expect(await screen.findByText('No entries match these filters.')).toBeInTheDocument();
  });

  it('withdraws the old rows, count and range when a filter change fails', async () => {
    vi.mocked(listAuditLog).mockResolvedValueOnce({
      total: 120,
      entries: [
        {
          id: 1,
          timestamp: '2026-10-22T10:00:00Z',
          admin_ip: '10.0.0.1',
          action: 'update_user',
          target_user_id: USER_ID,
          details: { role: 'pro' },
          success: true,
        },
      ],
    });
    const { rerender } = render(<AuditTab />);
    expect(await screen.findByText('Update user', { selector: 'span[title]' })).toBeInTheDocument();
    expect(screen.getByText('120 entries')).toBeInTheDocument();
    expect(screen.getByText(/1–50 of/)).toBeInTheDocument();

    // A new filter whose request is still in flight: the old rows stay, dimmed
    // and busy, but without a count or page range that would claim them.
    let reject!: (e: Error) => void;
    vi.mocked(listAuditLog).mockImplementationOnce(() => new Promise((_, r) => (reject = r)));
    search = new URLSearchParams('action=delete_key&offset=50');
    rerender(<AuditTab />);
    await waitFor(() =>
      expect(listAuditLog).toHaveBeenLastCalledWith('delete_key', undefined, 50, 50),
    );
    expect(
      screen.getByText('Update user', { selector: 'span[title]' }).closest('[aria-busy="true"]'),
    ).not.toBeNull();
    expect(screen.queryByText(/entries$/)).not.toBeInTheDocument();
    expect(screen.queryByText(/of 120/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument();

    // It fails: nothing from the previous filter is left on screen.
    reject(new Error('gateway down'));
    expect(await screen.findByRole('alert')).toHaveTextContent('gateway down');
    expect(screen.queryByText('Update user', { selector: 'span[title]' })).not.toBeInTheDocument();
    expect(screen.queryByText(/entries$/)).not.toBeInTheDocument();
    expect(screen.queryByText(/of 120/)).not.toBeInTheDocument();
  });
});
