// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
    await waitFor(() => expect(snoozeAlerts).toHaveBeenCalledWith(1800));
    expect(onToast).toHaveBeenCalledWith('Alert notifications snoozed for 30m');

    fireEvent.click(await screen.findByRole('button', { name: 'Resume now' }));
    await waitFor(() => expect(clearAlertSnooze).toHaveBeenCalledTimes(1));
    expect(onToast).toHaveBeenCalledWith('Alert notifications resumed');
  });
});
