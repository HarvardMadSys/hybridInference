// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AgentRunnerHostSection } from './AgentRunnerHostSection';

vi.mock('@/lib/api/admin', () => ({
  getAgentRunnerHosts: vi.fn(),
  setActiveAgentRunnerHost: vi.fn(),
  forgetAgentRunnerHost: vi.fn(),
}));

import {
  forgetAgentRunnerHost,
  getAgentRunnerHosts,
  setActiveAgentRunnerHost,
} from '@/lib/api/admin';

function host(name: string, overrides: Partial<Record<string, unknown>> = {}) {
  return {
    host: name,
    active: false,
    last_worker_id: `w-${name}`,
    first_seen_at: '2026-07-31T00:00:00Z',
    last_seen_at: '2026-07-31T00:00:00Z',
    seconds_since_seen: 3,
    ...overrides,
  };
}

describe('AgentRunnerHostSection', () => {
  const onToast = vi.fn();

  afterEach(() => cleanup());
  beforeEach(() => vi.clearAllMocks());

  it('shows the pool with the active host selected', async () => {
    vi.mocked(getAgentRunnerHosts).mockResolvedValue({
      hosts: [host('runner-a', { active: true }), host('runner-b', { seconds_since_seen: 7200 })],
      active_host: 'runner-a',
    });

    render(<AgentRunnerHostSection onToast={onToast} />);

    expect(await screen.findByText('runner-a')).toBeInTheDocument();
    expect(screen.getByLabelText(/runner-a/)).toBeChecked();
    expect(screen.getByLabelText(/runner-b/)).not.toBeChecked();
    // Poll age, not an invented liveness claim: a busy runner does not poll.
    expect(screen.getByText(/Last polled 2h ago/)).toBeInTheDocument();
  });

  it('pins the chosen host and says the switch is not preemptive', async () => {
    vi.mocked(getAgentRunnerHosts).mockResolvedValue({
      hosts: [host('runner-a', { active: true }), host('runner-b')],
      active_host: 'runner-a',
    });
    vi.mocked(setActiveAgentRunnerHost).mockResolvedValue({
      hosts: [host('runner-a'), host('runner-b', { active: true })],
      active_host: 'runner-b',
    });

    render(<AgentRunnerHostSection onToast={onToast} />);
    fireEvent.click(await screen.findByLabelText(/runner-b/));

    await waitFor(() => expect(setActiveAgentRunnerHost).toHaveBeenCalledWith('runner-b'));
    await waitFor(() => expect(screen.getByLabelText(/runner-b/)).toBeChecked());
    expect(onToast).toHaveBeenCalledWith(expect.stringContaining('keep running there'));
  });

  it('warns outright when the pinned host has gone quiet', async () => {
    // Pinning removes the failover: if this machine is down, nothing claims.
    vi.mocked(getAgentRunnerHosts).mockResolvedValue({
      hosts: [host('runner-a', { active: true, seconds_since_seen: 3600 })],
      active_host: 'runner-a',
    });

    render(<AgentRunnerHostSection onToast={onToast} />);

    expect(await screen.findByText(/last polled 1h ago/)).toBeInTheDocument();
    expect(screen.getByText(/the queue is stopped/)).toBeInTheDocument();
  });

  it('does not warn about a stale host that is not the pinned one', async () => {
    vi.mocked(getAgentRunnerHosts).mockResolvedValue({
      hosts: [host('runner-a', { active: true }), host('old-box', { seconds_since_seen: 90000 })],
      active_host: 'runner-a',
    });

    render(<AgentRunnerHostSection onToast={onToast} />);
    await screen.findByText('old-box');

    expect(screen.queryByText(/last polled 1h ago/)).not.toBeInTheDocument();
  });

  it('keeps polling, so a host that goes quiet after load still raises the warning', async () => {
    // Fetched once, the ages freeze and the warning can never fire for a
    // machine that dies while the operator is looking at the page.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(getAgentRunnerHosts)
      .mockResolvedValueOnce({
        hosts: [host('runner-a', { active: true, seconds_since_seen: 4 })],
        active_host: 'runner-a',
      })
      .mockResolvedValue({
        hosts: [host('runner-a', { active: true, seconds_since_seen: 3600 })],
        active_host: 'runner-a',
      });

    render(<AgentRunnerHostSection onToast={onToast} />);
    await screen.findByText('runner-a');
    expect(screen.queryByText(/last polled 1h ago/)).not.toBeInTheDocument();

    await vi.advanceTimersByTimeAsync(31_000);

    await waitFor(() => expect(screen.getByText(/last polled 1h ago/)).toBeInTheDocument());
    vi.useRealTimers();
  });

  it('leaves the last good pool on screen when a background poll fails', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(getAgentRunnerHosts)
      .mockResolvedValueOnce({
        hosts: [host('runner-a', { active: true })],
        active_host: 'runner-a',
      })
      .mockRejectedValue(new Error('gateway hiccup'));

    render(<AgentRunnerHostSection onToast={onToast} />);
    await screen.findByText('runner-a');

    await vi.advanceTimersByTimeAsync(31_000);

    expect(screen.getByText('runner-a')).toBeInTheDocument();
    expect(screen.queryByText(/gateway hiccup/)).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it('unpins through the "Any host" choice', async () => {
    vi.mocked(getAgentRunnerHosts).mockResolvedValue({
      hosts: [host('runner-b', { active: true })],
      active_host: 'runner-b',
    });
    vi.mocked(setActiveAgentRunnerHost).mockResolvedValue({
      hosts: [host('runner-b')],
      active_host: null,
    });

    render(<AgentRunnerHostSection onToast={onToast} />);
    fireEvent.click(await screen.findByLabelText(/Any host/));

    await waitFor(() => expect(setActiveAgentRunnerHost).toHaveBeenCalledWith(null));
  });

  it('offers no way to remove the host that is currently running jobs', async () => {
    vi.mocked(getAgentRunnerHosts).mockResolvedValue({
      hosts: [host('runner-a', { active: true }), host('old-box')],
      active_host: 'runner-a',
    });

    render(<AgentRunnerHostSection onToast={onToast} />);
    await screen.findByText('runner-a');

    const removes = screen.getAllByRole('button', { name: 'Remove' });
    expect(removes).toHaveLength(1);

    vi.mocked(forgetAgentRunnerHost).mockResolvedValue({
      hosts: [host('runner-a', { active: true })],
      active_host: 'runner-a',
    });
    fireEvent.click(removes[0]);
    await waitFor(() => expect(forgetAgentRunnerHost).toHaveBeenCalledWith('old-box'));
  });

  it('tells the operator how a machine joins when the pool is empty', async () => {
    vi.mocked(getAgentRunnerHosts).mockResolvedValue({ hosts: [], active_host: null });

    render(<AgentRunnerHostSection onToast={onToast} />);

    expect(await screen.findByText(/No runner has reported a host yet/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Any host/)).toBeChecked();
  });

  it('surfaces a failed switch instead of showing it as applied', async () => {
    vi.mocked(getAgentRunnerHosts).mockResolvedValue({
      hosts: [host('runner-a', { active: true }), host('runner-b')],
      active_host: 'runner-a',
    });
    vi.mocked(setActiveAgentRunnerHost).mockRejectedValue(new Error('no runner on runner-b'));

    render(<AgentRunnerHostSection onToast={onToast} />);
    fireEvent.click(await screen.findByLabelText(/runner-b/));

    await waitFor(() =>
      expect(onToast).toHaveBeenCalledWith(expect.stringContaining('Could not switch host')),
    );
    expect(screen.getByLabelText(/runner-a/)).toBeChecked();
  });
});
