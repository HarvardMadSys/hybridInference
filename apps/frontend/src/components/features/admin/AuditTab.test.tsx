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

import { listAuditLog, type ListAuditLogResponse } from '@/lib/api/admin';
import { AuditTab } from './AuditTab';
import { chooseOption } from '@/test-utils/select';
import { escapingScreenReaderText } from './__tests__/layoutContainment';

const USER_ID = '0123456789abcdef0123';

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
    expect(screen.getByLabelText('Filter by user ID')).toHaveValue('');
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
    render(<AuditTab />);

    fireEvent.click(await screen.findByRole('button', { name: `Filter by user ${USER_ID}` }));
    expect(replace).toHaveBeenLastCalledWith(`/dashboard/admin/log?user_id=${USER_ID}`, {
      scroll: false,
    });

    chooseOption('Filter by action', 'Delete key');
    expect(replace).toHaveBeenLastCalledWith('/dashboard/admin/log?action=delete_key', {
      scroll: false,
    });
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
