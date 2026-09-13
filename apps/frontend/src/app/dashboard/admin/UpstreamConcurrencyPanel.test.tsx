// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { UpstreamConcurrencyPanel } from './UpstreamConcurrencyPanel';

vi.mock('@/lib/api/admin', () => ({ getUpstreamConcurrency: vi.fn() }));

import { getUpstreamConcurrency } from '@/lib/api/admin';
import type { UpstreamConcurrencyResponse } from '@/lib/api/admin';

const config: UpstreamConcurrencyResponse['config'] = {
  enabled: true,
  initial_limit: 8,
  max_limit: 64,
  probe_success_interval: 100,
  acquire_timeout_sec: 30,
};

const idle: UpstreamConcurrencyResponse = { config, buckets: [] };

const busy: UpstreamConcurrencyResponse = {
  config,
  buckets: [
    {
      provider: 'chutes',
      key_fingerprint: 'aaaaaaaa1111',
      limit: 12,
      in_flight: 4,
      waiting: 0,
      successes_since_probe: 37,
      probing: false,
    },
    {
      provider: 'zai',
      key_fingerprint: 'bbbbbbbb2222',
      limit: 3,
      in_flight: 3,
      waiting: 2,
      successes_since_probe: 0,
      probing: true,
    },
  ],
};

function rowFor(fingerprint: string): HTMLElement {
  return screen.getByText(fingerprint).closest('tr') as HTMLElement;
}

describe('UpstreamConcurrencyPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getUpstreamConcurrency).mockResolvedValue(busy);
  });

  afterEach(() => {
    cleanup();
  });

  it('renders one row per bucket with its live counters', async () => {
    render(<UpstreamConcurrencyPanel />);

    await screen.findByRole('heading', { level: 3, name: 'Upstream concurrency' });

    const zai = rowFor('bbbbbbbb2222');
    expect(within(zai).getByText('zai')).toBeInTheDocument();
    // The learned limit is the number the panel exists to show.
    expect(within(zai).getByTestId('upstream-limit-bbbbbbbb2222')).toHaveTextContent('3');
    expect(within(zai).getByText('2')).toBeInTheDocument();
    expect(within(zai).getByText('0/100')).toBeInTheDocument();
    expect(within(zai).getByText('probing')).toBeInTheDocument();

    const chutes = rowFor('aaaaaaaa1111');
    expect(within(chutes).getByTestId('upstream-limit-aaaaaaaa1111')).toHaveTextContent('12');
    expect(within(chutes).getByText('37/100')).toBeInTheDocument();
    expect(within(chutes).queryByText('probing')).not.toBeInTheDocument();
  });

  it('shows the effective limiter config', async () => {
    render(<UpstreamConcurrencyPanel />);

    const summary = await screen.findByTestId('upstream-concurrency-config');
    expect(within(summary).getByText('8')).toBeInTheDocument();
    expect(within(summary).getByText('64')).toBeInTheDocument();
    expect(within(summary).getByText('100')).toBeInTheDocument();
    expect(within(summary).getByText('30s')).toBeInTheDocument();
  });

  it('explains an empty result rather than rendering an empty table', async () => {
    vi.mocked(getUpstreamConcurrency).mockResolvedValue(idle);

    render(<UpstreamConcurrencyPanel />);

    expect(await screen.findByText(/No remote traffic yet/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('warns when the limiter is switched off', async () => {
    vi.mocked(getUpstreamConcurrency).mockResolvedValue({
      ...idle,
      config: { ...config, enabled: false },
    });

    render(<UpstreamConcurrencyPanel />);

    expect(await screen.findByText(/The limiter is disabled/)).toBeInTheDocument();
  });

  it('surfaces a load failure with a retry that refetches', async () => {
    vi.mocked(getUpstreamConcurrency).mockRejectedValueOnce(new Error('gateway unreachable'));

    render(<UpstreamConcurrencyPanel />);

    expect(await screen.findByText(/gateway unreachable/)).toBeInTheDocument();

    vi.mocked(getUpstreamConcurrency).mockResolvedValue(busy);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText('bbbbbbbb2222')).toBeInTheDocument();
  });

  it('refetches when Refresh is clicked', async () => {
    render(<UpstreamConcurrencyPanel />);

    await screen.findByText('bbbbbbbb2222');
    expect(getUpstreamConcurrency).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    await waitFor(() => {
      expect(getUpstreamConcurrency).toHaveBeenCalledTimes(2);
    });
  });

  it('polls on an interval and stops polling once unmounted', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { unmount } = render(<UpstreamConcurrencyPanel />);
      await vi.waitFor(() => {
        expect(getUpstreamConcurrency).toHaveBeenCalledTimes(1);
      });

      await vi.advanceTimersByTimeAsync(10_000);
      expect(getUpstreamConcurrency).toHaveBeenCalledTimes(2);

      unmount();
      await vi.advanceTimersByTimeAsync(30_000);
      // The interval was cleared on unmount, so the count stands still.
      expect(getUpstreamConcurrency).toHaveBeenCalledTimes(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('never asks the backend for a raw key', async () => {
    render(<UpstreamConcurrencyPanel />);

    await screen.findByText('bbbbbbbb2222');
    // The endpoint takes no arguments at all — there is no key to pass, and the
    // fingerprint is the only credential-derived value the panel ever renders.
    expect(vi.mocked(getUpstreamConcurrency).mock.calls[0]).toEqual([]);
  });
});
