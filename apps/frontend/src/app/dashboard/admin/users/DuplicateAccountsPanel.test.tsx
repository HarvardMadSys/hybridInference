// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import type { ReactNode } from 'react';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/admin', () => ({ scanDuplicateAccounts: vi.fn() }));

import { scanDuplicateAccounts, type DuplicateAccountScan } from '@/lib/api/admin';
import { chooseOption } from '@/test-utils/select';
import { DuplicateAccountsPanel } from './DuplicateAccountsPanel';
import { userDetailHref } from './lib/userLink';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const scan: DuplicateAccountScan = {
  days: 14,
  since: '2026-11-14T12:00:00Z',
  accounts_scanned: 40,
  shared_addresses: 2,
  shared_browsers: 0,
  total_groups: 1,
  groups: [
    {
      score: 11,
      user_ids: ['u-ann', 'u-ben'],
      signals: ['signup_burst', 'signup_ip', 'request_ip'],
      members: [
        {
          user_id: 'u-ann',
          email: 'ann@example.org',
          login_name: null,
          phone: null,
          user_name: 'Ann',
          role: 'free',
          status: 'active',
          created_at: '2026-11-20T10:00:00Z',
          requests: 1200,
        },
        {
          user_id: 'u-ben',
          email: 'ben@example.org',
          login_name: null,
          phone: null,
          user_name: null,
          role: 'pro',
          status: 'suspended',
          created_at: '2026-11-20T10:04:00Z',
          requests: 1,
        },
      ],
      links: [
        {
          user_ids: ['u-ann', 'u-ben'],
          weight: 8,
          evidence: [
            {
              kind: 'signup_burst',
              weight: 3,
              addresses: ['198.51.100.7'],
              detail: 'Registered 4 minutes apart from 198.51.100.7',
            },
            {
              kind: 'signup_ip',
              weight: 3,
              addresses: ['198.51.100.7'],
              detail: 'Both registered from 198.51.100.7',
            },
            {
              kind: 'request_ip',
              weight: 2,
              addresses: ['198.51.100.7'],
              detail: 'Both sent API requests from 198.51.100.7',
            },
          ],
        },
      ],
    },
  ],
  oversized: [{ user_ids: Array.from({ length: 12 }, (_, i) => `x${i}`) }],
};

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  render(<DuplicateAccountsPanel />, { wrapper });
}

describe('DuplicateAccountsPanel', () => {
  it('scans nothing until asked', () => {
    renderPanel();

    expect(screen.getByRole('region', { name: 'Duplicate accounts' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Scan' })).toBeEnabled();
    expect(scanDuplicateAccounts).not.toHaveBeenCalled();
  });

  it('shows each group with its members, signals and evidence', async () => {
    vi.mocked(scanDuplicateAccounts).mockResolvedValue(scan);
    renderPanel();

    fireEvent.click(screen.getByRole('button', { name: 'Scan' }));

    const group = await screen.findByTestId('duplicate-account-group');
    expect(scanDuplicateAccounts).toHaveBeenCalledWith(14);
    expect(screen.getByRole('status')).toHaveTextContent(
      '1 group among the 40 accounts active since Nov 14, 2026, highest score first. ' +
        'Set aside as shared: 2 addresses shared by more than 5 accounts.',
    );
    expect(within(group).getByText('Score 11')).toBeInTheDocument();
    for (const label of ['Registered together', 'Signup address', 'Request address']) {
      expect(within(group).getByText(label)).toBeInTheDocument();
    }
    const ann = within(group).getByRole('link', { name: 'ann@example.org' });
    expect(ann).toHaveAttribute('href', userDetailHref('u-ann'));
    expect(within(group).getByText('1,200 requests · joined Nov 20, 2026')).toBeInTheDocument();
    expect(within(group).getByText('Suspended')).toBeInTheDocument();
    expect(within(group).getByText('pro')).toBeInTheDocument();
    expect(within(group).getByText('Why these accounts (1 link)')).toBeInTheDocument();
    expect(within(group).getByText('ann@example.org ↔ ben@example.org')).toBeInTheDocument();
    expect(
      within(group).getByText('Registered 4 minutes apart from 198.51.100.7'),
    ).toBeInTheDocument();
    expect(screen.getByText(/1 larger component \(12 accounts\)/)).toBeInTheDocument();
  });

  it('scans the window chosen', async () => {
    vi.mocked(scanDuplicateAccounts).mockResolvedValue({ ...scan, days: 90 });
    renderPanel();

    chooseOption('Scan window', 'Last 90 days');
    fireEvent.click(screen.getByRole('button', { name: 'Scan' }));

    await screen.findByTestId('duplicate-account-group');
    expect(scanDuplicateAccounts).toHaveBeenCalledWith(90);
  });

  it('says so when no accounts look alike', async () => {
    vi.mocked(scanDuplicateAccounts).mockResolvedValue({
      ...scan,
      shared_addresses: 0,
      total_groups: 0,
      groups: [],
      oversized: [],
    });
    renderPanel();

    fireEvent.click(screen.getByRole('button', { name: 'Scan' }));

    expect(await screen.findByRole('status')).toHaveTextContent(
      "No accounts look like one person's among the 40 accounts active since Nov 14, 2026.",
    );
    expect(screen.queryByTestId('duplicate-account-group')).not.toBeInTheDocument();
  });

  it('shows a failed scan and hides the results on request', async () => {
    vi.mocked(scanDuplicateAccounts).mockRejectedValue(
      new Error('The scan did not finish in time. Try a shorter window.'),
    );
    renderPanel();

    fireEvent.click(screen.getByRole('button', { name: 'Scan' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The scan did not finish in time. Try a shorter window.',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Hide results' }));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  });
});
