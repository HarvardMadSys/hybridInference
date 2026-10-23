// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('@/lib/api/admin', () => ({
  cancelBroadcast: vi.fn(),
  createBroadcast: vi.fn(),
  getBroadcastDetail: vi.fn(),
  listBroadcasts: vi.fn(),
  previewBroadcast: vi.fn(),
  sendTestBroadcastEmail: vi.fn(),
}));

import { getBroadcastDetail, listBroadcasts } from '@/lib/api/admin';
import { BroadcastsTab } from './BroadcastsTab';

const BROADCAST = {
  id: 'b1',
  subject: 'Maintenance tonight',
  status: 'sent',
  recipient_count: 2,
  scheduled_at: null,
  sent_at: '2026-10-22T10:00:00Z',
  created_by: 'admin',
  created_at: '2026-10-22T09:00:00Z',
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listBroadcasts).mockResolvedValue({ total: 1, broadcasts: [BROADCAST] });
  vi.mocked(getBroadcastDetail).mockResolvedValue({
    broadcast: BROADCAST,
    total_recipients: 1,
    recipients: [
      { user_id: 'u1', email: 'a@example.com', status: 'sent', error: null, sent_at: null },
    ],
  });
});

afterEach(cleanup);

describe('BroadcastsTab', () => {
  it('labels the composer fields', async () => {
    render(<BroadcastsTab />);
    await screen.findByText('Maintenance tonight');

    expect(screen.getByLabelText('Template')).toHaveValue('custom');
    expect(screen.getByLabelText('Subject')).toBeInTheDocument();
    expect(screen.getByLabelText('Body (Markdown)')).toBeInTheDocument();
    expect(screen.getByLabelText('Minimum spend today (USD)')).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Roles' })).toBeInTheDocument();
    expect(screen.getByRole('group', { name: 'Statuses' })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Send now' })).toBeChecked();
  });

  it('blocks sending while the spend threshold is not a number', async () => {
    render(<BroadcastsTab />);
    await screen.findByText('Maintenance tonight');

    const spend = screen.getByLabelText('Minimum spend today (USD)');
    fireEvent.change(spend, { target: { value: '-1' } });

    expect(spend).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('button', { name: 'Send Now' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Preview & Count' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Send Test to Me' })).toBeDisabled();
  });

  it('opens a broadcast’s recipients from a button, not only a row click', async () => {
    render(<BroadcastsTab />);
    const history = await screen.findByRole('region', { name: 'Send History' });
    const toggle = within(history).getByRole('button', { name: 'Maintenance tonight' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(toggle);

    expect(await within(history).findByText('Recipients (1)')).toBeInTheDocument();
    expect(getBroadcastDetail).toHaveBeenCalledTimes(1);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    fireEvent.click(toggle);
    await waitFor(() => expect(within(history).queryByText('Recipients (1)')).toBeNull());
    expect(getBroadcastDetail).toHaveBeenCalledTimes(1);
  });
});
