// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { clearAlertSnooze, getAlertSnooze, snoozeAlerts } from '@/lib/api/admin';

import { AlertSnoozeSection } from './AlertSnoozeSection';

vi.mock('@/lib/api/admin', () => ({
  clearAlertSnooze: vi.fn(),
  getAlertSnooze: vi.fn(),
  snoozeAlerts: vi.fn(),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('AlertSnoozeSection', () => {
  it('snoozes and resumes backend notifications with channel-neutral wording', async () => {
    const active = { snoozed: false, snooze_until: null, seconds_remaining: 0 };
    vi.mocked(getAlertSnooze).mockResolvedValue(active);
    vi.mocked(snoozeAlerts).mockResolvedValue({
      snoozed: true,
      snooze_until: Date.now() / 1000 + 1800,
      seconds_remaining: 1800,
    });
    vi.mocked(clearAlertSnooze).mockResolvedValue(active);
    const onToast = vi.fn();
    render(<AlertSnoozeSection onToast={onToast} />);

    expect(screen.getByRole('heading', { name: 'Alert Notifications' })).toBeInTheDocument();
    expect(screen.queryByText(/Slack/)).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: '30m' }));
    // A discrete action: one confirmation naming the impact, and Cancel writes nothing.
    let dialog = screen.getByRole('dialog', { name: 'Snooze alert notifications for 30m?' });
    expect(dialog).toHaveTextContent('No backend alert is sent until');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(snoozeAlerts).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '30m' }));
    dialog = screen.getByRole('dialog', { name: 'Snooze alert notifications for 30m?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Snooze' }));
    await waitFor(() => expect(snoozeAlerts).toHaveBeenCalledWith(1800));
    expect(snoozeAlerts).toHaveBeenCalledTimes(1);
    expect(onToast).toHaveBeenCalledWith('Alert notifications snoozed for 30m');

    fireEvent.click(await screen.findByRole('button', { name: 'Resume now' }));
    dialog = screen.getByRole('dialog', { name: 'Resume alert notifications now?' });
    expect(clearAlertSnooze).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Resume now' }));
    await waitFor(() => expect(clearAlertSnooze).toHaveBeenCalledTimes(1));
    expect(onToast).toHaveBeenCalledWith('Alert notifications resumed');
  });

  it('keeps the confirmation open with the error when a snooze fails', async () => {
    vi.mocked(getAlertSnooze).mockResolvedValue({
      snoozed: false,
      snooze_until: null,
      seconds_remaining: 0,
    });
    vi.mocked(snoozeAlerts).mockRejectedValueOnce(new Error('Database not configured'));
    render(<AlertSnoozeSection onToast={vi.fn()} />);

    fireEvent.click(await screen.findByRole('button', { name: '1h' }));
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Snooze' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Database not configured');
    expect(screen.getByText('Active — alerts are being sent')).toBeInTheDocument();
  });
});
