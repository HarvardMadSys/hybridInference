// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { chooseOption } from '@/test-utils/select';

import { AlertMutesSection } from './AlertMutesSection';

vi.mock('@/lib/api/admin', () => ({
  listAlertMutes: vi.fn(),
  muteAlertType: vi.fn(),
  unmuteAlertType: vi.fn(),
}));

import { listAlertMutes, muteAlertType, unmuteAlertType } from '@/lib/api/admin';
import type { AlertTypeMute } from '@/lib/api/admin';

function alertType(overrides: Partial<AlertTypeMute> = {}): AlertTypeMute {
  return {
    alert_type: 'auth_ip_blocked',
    label: 'Auth-failure blocklist refusing a source',
    description: 'A source crossed the auth-failure threshold and is now refused.',
    group: 'Auth',
    key_pattern: 'auth_ip_blocked',
    muted: false,
    muted_until: null,
    muted_by: null,
    muted_at: null,
    ...overrides,
  };
}

const blocked = alertType();
const circuit = alertType({
  alert_type: 'circuit_open',
  label: 'Provider circuit opened',
  description: "A provider's circuit breaker tripped.",
  group: 'Providers',
  key_pattern: 'circuit_open:<provider>',
});

describe('AlertMutesSection', () => {
  beforeEach(() => {
    vi.mocked(listAlertMutes).mockResolvedValue({ types: [circuit, blocked] });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('lists every alert type under its group with its status', async () => {
    vi.mocked(listAlertMutes).mockResolvedValue({
      types: [{ ...circuit, muted: true, muted_by: 'admin@x.com' }, blocked],
    });

    render(<AlertMutesSection onToast={vi.fn()} />);

    expect(await screen.findByText('Provider circuit opened')).toBeInTheDocument();
    expect(screen.getByText(/Mute one kind of backend alert/)).toBeInTheDocument();
    expect(screen.getByText('Providers')).toBeInTheDocument();
    expect(screen.getByText('Auth')).toBeInTheDocument();
    expect(screen.getByText('circuit_open:<provider>')).toBeInTheDocument();
    expect(screen.getByText('Muted until unmuted')).toBeInTheDocument();
    expect(screen.getByText('by admin@x.com')).toBeInTheDocument();
    expect(screen.getByText('Sending')).toBeInTheDocument();
    expect(screen.getByText('1 muted')).toBeInTheDocument();
    // Only the muted row offers to lift its mute.
    expect(screen.getAllByRole('button', { name: 'Unmute' })).toHaveLength(1);
  });

  it('mutes a type for the chosen duration', async () => {
    const until = Date.now() / 1000 + 24 * 60 * 60;
    vi.mocked(muteAlertType).mockResolvedValue({
      ...blocked,
      muted: true,
      muted_until: until,
      muted_by: 'admin@x.com',
    });
    const onToast = vi.fn();
    render(<AlertMutesSection onToast={onToast} />);

    await screen.findByRole('combobox', { name: `Mute ${blocked.label}` });
    // Choosing a duration mutes at once: Unmute undoes it.
    chooseOption(`Mute ${blocked.label}`, '24 hours');
    expect(screen.queryByRole('dialog')).toBeNull();

    await waitFor(() => {
      expect(muteAlertType).toHaveBeenCalledWith('auth_ip_blocked', 24 * 60 * 60);
    });
    expect(await screen.findByText(/^Muted until /)).toBeInTheDocument();
    expect(onToast).toHaveBeenCalledWith(`Muted "${blocked.label}" for 24 hours`);
    // The other type is untouched.
    expect(screen.getByText('Sending')).toBeInTheDocument();
  });

  it('mutes a type until it is unmuted', async () => {
    vi.mocked(muteAlertType).mockResolvedValue({ ...blocked, muted: true });
    const onToast = vi.fn();
    render(<AlertMutesSection onToast={onToast} />);

    await screen.findByRole('combobox', { name: `Mute ${blocked.label}` });
    chooseOption(`Mute ${blocked.label}`, 'Until unmuted');

    await waitFor(() => {
      expect(muteAlertType).toHaveBeenCalledWith('auth_ip_blocked', null);
    });
    expect(await screen.findByText('Muted until unmuted')).toBeInTheDocument();
    expect(onToast).toHaveBeenCalledWith(`Muted "${blocked.label}" until unmuted`);
  });

  it('mutes again when the same duration is chosen twice', async () => {
    vi.mocked(muteAlertType).mockImplementation(async () => ({ ...blocked, muted: true }));
    const onToast = vi.fn();
    render(<AlertMutesSection onToast={onToast} />);

    const trigger = await screen.findByRole('combobox', { name: `Mute ${blocked.label}` });
    expect(trigger).toHaveTextContent('Mute for…');
    chooseOption(`Mute ${blocked.label}`, '1 hour');
    await waitFor(() => expect(muteAlertType).toHaveBeenCalledTimes(1));
    // The control stays a prompt rather than showing the duration last chosen.
    await waitFor(() => expect(trigger).toHaveTextContent('Change…'));
    await waitFor(() => expect(trigger).not.toBeDisabled());

    // The same duration again is a new command: it mutes again.
    chooseOption(`Mute ${blocked.label}`, '1 hour');
    await waitFor(() => expect(muteAlertType).toHaveBeenCalledTimes(2));
    expect(muteAlertType).toHaveBeenNthCalledWith(2, 'auth_ip_blocked', 60 * 60);
  });

  it('unmutes a muted type', async () => {
    vi.mocked(listAlertMutes).mockResolvedValue({ types: [{ ...circuit, muted: true }] });
    vi.mocked(unmuteAlertType).mockResolvedValue(circuit);
    const onToast = vi.fn();
    render(<AlertMutesSection onToast={onToast} />);

    fireEvent.click(await screen.findByRole('button', { name: 'Unmute' }));
    expect(screen.queryByRole('dialog')).toBeNull();

    await waitFor(() => {
      expect(unmuteAlertType).toHaveBeenCalledWith('circuit_open');
    });
    expect(await screen.findByText('Sending')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Unmute' })).not.toBeInTheDocument();
    expect(onToast).toHaveBeenCalledWith('Unmuted "Provider circuit opened"');
  });

  it('reports a failed mute above the table, leaves the row as it was, and mutes on a retry', async () => {
    vi.mocked(muteAlertType)
      .mockRejectedValueOnce(new Error('Database not configured'))
      .mockResolvedValueOnce({ ...blocked, muted: true });
    const onToast = vi.fn();
    render(<AlertMutesSection onToast={onToast} />);

    await screen.findByRole('combobox', { name: `Mute ${blocked.label}` });
    chooseOption(`Mute ${blocked.label}`, '1 hour');

    expect(await screen.findByRole('alert')).toHaveTextContent('Database not configured');
    expect(onToast).not.toHaveBeenCalled();
    expect(screen.getAllByText('Sending')).toHaveLength(2);
    // A write failure is not a load failure: it offers no reload, only dismissal.
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();

    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: `Mute ${blocked.label}` })).toBeEnabled(),
    );
    chooseOption(`Mute ${blocked.label}`, '1 hour');
    expect(await screen.findByText('Muted until unmuted')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(muteAlertType).toHaveBeenCalledTimes(2);
  });

  it('shows a load failure with a retry', async () => {
    vi.mocked(listAlertMutes)
      .mockRejectedValueOnce(new Error('Service unavailable'))
      .mockResolvedValueOnce({ types: [blocked] });
    render(<AlertMutesSection onToast={vi.fn()} />);

    expect(await screen.findByText('Service unavailable')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText(blocked.label)).toBeInTheDocument();
    expect(listAlertMutes).toHaveBeenCalledTimes(2);
  });

  it('reloads once a timed mute lapses', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const soon = Date.now() / 1000 + 60;
      vi.mocked(listAlertMutes)
        .mockResolvedValueOnce({ types: [{ ...blocked, muted: true, muted_until: soon }] })
        .mockResolvedValueOnce({ types: [blocked] });
      render(<AlertMutesSection onToast={vi.fn()} />);
      expect(await screen.findByText(/^Muted until /)).toBeInTheDocument();

      await vi.advanceTimersByTimeAsync(63_000);

      expect(await screen.findByText('Sending')).toBeInTheDocument();
      expect(listAlertMutes).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
