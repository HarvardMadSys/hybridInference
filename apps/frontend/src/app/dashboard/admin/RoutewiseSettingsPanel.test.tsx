// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RoutewiseSettingsPanel } from './RoutewiseSettingsPanel';

vi.mock('@/lib/api/admin', () => ({
  listRoutewiseProbeSamples: vi.fn(),
  listRoutewiseSettings: vi.fn(),
  runRoutewiseProbe: vi.fn(),
  updateRoutewiseSetting: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

import { listRoutewiseProbeSamples, listRoutewiseSettings } from '@/lib/api/admin';
import type { RoutewiseProbeSampleItem } from '@/lib/api/admin';

const LIVE_ENDPOINT = 'minimax-fast:openrouter[minimax/highspeed]-api';
const ORPHANED_ENDPOINT = 'minimax-fast:featherless-api';

const liveSample: RoutewiseProbeSampleItem = {
  model_id: 'minimax/minimax-m2.5',
  endpoint_id: LIVE_ENDPOINT,
  ttft_ms: 1189,
  ok: true,
  error: null,
  checked_at: '2026-07-01T19:12:25Z',
};

// A leftover sample for an endpoint that was replaced by a route override. It
// is still inside the 24h lookback window returned by the list endpoint.
const orphanedSample: RoutewiseProbeSampleItem = {
  model_id: 'minimax/minimax-m2.5',
  endpoint_id: ORPHANED_ENDPOINT,
  ttft_ms: null,
  ok: false,
  error: "All 1 keys for provider 'featherless' are muted",
  checked_at: '2026-06-30T21:19:23Z',
};

beforeEach(() => {
  vi.mocked(listRoutewiseSettings).mockResolvedValue({ settings: [] });
  // Backend returns newest-first; the orphaned sample lands after the live one.
  vi.mocked(listRoutewiseProbeSamples).mockResolvedValue({
    samples: [liveSample, orphanedSample],
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('RoutewiseSettingsPanel probe table', () => {
  it('hides probe samples for endpoints that are no longer live route candidates', async () => {
    render(
      <RoutewiseSettingsPanel
        modelId="minimax-fast"
        endpoints={[{ endpointId: LIVE_ENDPOINT, label: `concurrency · ${LIVE_ENDPOINT}` }]}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(LIVE_ENDPOINT)).toBeInTheDocument();
    });
    // The featherless endpoint was replaced by the override, so its stale
    // "muted" sample must not linger in the status table.
    expect(screen.queryByText(ORPHANED_ENDPOINT)).not.toBeInTheDocument();
  });

  it('shows every sample when no live endpoint list is provided', async () => {
    render(<RoutewiseSettingsPanel modelId="minimax-fast" endpoints={[]} />);

    await waitFor(() => {
      expect(screen.getByText(LIVE_ENDPOINT)).toBeInTheDocument();
    });
    // Without a live endpoint list to filter against, fall back to showing all
    // persisted samples rather than an empty table.
    expect(screen.getByText(ORPHANED_ENDPOINT)).toBeInTheDocument();
  });
});
