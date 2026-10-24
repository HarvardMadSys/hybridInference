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
import { escapingScreenReaderText, uncontainedGrids } from './__tests__/layoutContainment';

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
  it('shows the history first and opens the composer on request', async () => {
    render(<BroadcastsTab />);
    await screen.findByText('Maintenance tonight');

    expect(screen.queryByLabelText('Template')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Compose email' }));
    expect(screen.getByRole('group', { name: 'New broadcast' })).toBeInTheDocument();
    expect(screen.getByLabelText('Subject')).toHaveFocus();
  });

  it('keeps what was typed while the composer is hidden', async () => {
    render(<BroadcastsTab />);
    await screen.findByText('Maintenance tonight');
    fireEvent.click(screen.getByRole('button', { name: 'Compose email' }));
    fireEvent.change(screen.getByLabelText('Subject'), { target: { value: 'Heads up' } });

    fireEvent.click(screen.getByRole('button', { name: 'Hide' }));
    expect(screen.queryByLabelText('Subject')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resume draft' })).toHaveFocus();

    fireEvent.click(screen.getByRole('button', { name: 'Resume draft' }));
    expect(screen.getByLabelText('Subject')).toHaveValue('Heads up');
    expect(screen.getByLabelText('Subject')).toHaveFocus();
  });

  it('labels the composer fields', async () => {
    render(<BroadcastsTab />);
    await screen.findByText('Maintenance tonight');
    fireEvent.click(screen.getByRole('button', { name: 'Compose email' }));

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
    fireEvent.click(screen.getByRole('button', { name: 'Compose email' }));

    const spend = screen.getByLabelText('Minimum spend today (USD)');
    fireEvent.change(spend, { target: { value: '-1' } });

    expect(spend).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('button', { name: 'Send Now' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Preview & Count' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Send Test to Me' })).toBeDisabled();
  });

  it('opens a broadcast’s recipients from a button, not only a row click', async () => {
    render(<BroadcastsTab />);
    const history = await screen.findByRole('region', { name: 'Email broadcasts' });
    const toggle = within(history).getByRole('button', {
      name: 'Show recipients of Maintenance tonight',
    });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(toggle);

    // Opens at once, with a loading line, then the list.
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(await within(history).findByText('Recipients (1)')).toBeInTheDocument();
    expect(getBroadcastDetail).toHaveBeenCalledTimes(1);
    expect(getBroadcastDetail).toHaveBeenCalledWith('b1');
    expect(escapingScreenReaderText(history)).toEqual([]);

    fireEvent.click(
      within(history).getByRole('button', { name: 'Hide recipients of Maintenance tonight' }),
    );
    await waitFor(() => expect(within(history).queryByText('Recipients (1)')).toBeNull());
    expect(getBroadcastDetail).toHaveBeenCalledTimes(1);
  });

  it('shows a failed recipients load in place and retries it', async () => {
    vi.mocked(getBroadcastDetail).mockRejectedValueOnce(new Error('timeout'));
    render(<BroadcastsTab />);
    const history = await screen.findByRole('region', { name: 'Email broadcasts' });

    fireEvent.click(
      within(history).getByRole('button', { name: 'Show recipients of Maintenance tonight' }),
    );
    const alert = await within(history).findByRole('alert');
    expect(alert).toHaveTextContent('Recipients could not be loaded: timeout');

    fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await within(history).findByText('Recipients (1)')).toBeInTheDocument();
    expect(getBroadcastDetail).toHaveBeenCalledTimes(2);
  });

  it('keeps a wide Markdown preview inside the composer on a narrow screen', async () => {
    render(<BroadcastsTab />);
    await screen.findByText('Maintenance tonight');
    fireEvent.click(screen.getByRole('button', { name: 'Compose email' }));
    const cells = Array.from({ length: 6 }, () => 'X'.repeat(80));
    fireEvent.change(screen.getByLabelText('Body (Markdown)'), {
      target: { value: `| ${cells.join(' | ')} |\n|${'---|'.repeat(6)}\n| ${cells.join(' | ')} |` },
    });

    const composer = screen.getByRole('group', { name: 'New broadcast' });
    const rendered = within(composer).getByRole('table');
    expect(uncontainedGrids(rendered, composer)).toEqual([]);
  });
});
