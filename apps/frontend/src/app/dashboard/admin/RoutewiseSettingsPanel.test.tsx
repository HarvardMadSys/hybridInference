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
const REMOVED_ENDPOINT = 'minimax-fast:featherless-api';
const FRESH_NON_ROUTE_ENDPOINT = 'minimax-fast:openrouter[akashml]-api';

function sample(overrides: Partial<RoutewiseProbeSampleItem>): RoutewiseProbeSampleItem {
  return {
    model_id: 'minimax/minimax-m2.5',
    endpoint_id: LIVE_ENDPOINT,
    ttft_ms: 1189,
    ok: true,
    error: null,
    checked_at: '2026-07-01T19:12:25Z',
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(listRoutewiseSettings).mockResolvedValue({ settings: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('RoutewiseSettingsPanel probe table', () => {
  it('hides a stale sample for an endpoint that is no longer a live route', async () => {
    // featherless was replaced by the override, so its last probe (a day old)
    // is a leftover in the 24h window and must not read as a current failure.
    vi.mocked(listRoutewiseProbeSamples).mockResolvedValue({
      samples: [
        sample({ endpoint_id: LIVE_ENDPOINT, checked_at: '2026-07-01T19:12:25Z' }),
        sample({
          endpoint_id: REMOVED_ENDPOINT,
          ok: false,
          ttft_ms: null,
          error: "All 1 keys for provider 'featherless' are muted",
          checked_at: '2026-06-30T21:19:23Z',
        }),
      ],
    });

    render(
      <RoutewiseSettingsPanel
        modelId="minimax-fast"
        endpoints={[{ endpointId: LIVE_ENDPOINT, label: `concurrency · ${LIVE_ENDPOINT}` }]}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(LIVE_ENDPOINT)).toBeInTheDocument();
    });
    expect(screen.queryByText(REMOVED_ENDPOINT)).not.toBeInTheDocument();
  });

  it('keeps a freshly probed endpoint even when it is not in the live route set', async () => {
    // akashml is not a route row, but it was probed in the latest cycle and its
    // failure is real current signal — it must stay visible.
    vi.mocked(listRoutewiseProbeSamples).mockResolvedValue({
      samples: [
        sample({ endpoint_id: LIVE_ENDPOINT, checked_at: '2026-07-01T19:12:25Z' }),
        sample({
          endpoint_id: FRESH_NON_ROUTE_ENDPOINT,
          ok: false,
          ttft_ms: null,
          error: 'No endpoints found for /-m2.5.',
          checked_at: '2026-07-01T19:03:57Z',
        }),
      ],
    });

    render(
      <RoutewiseSettingsPanel
        modelId="minimax-fast"
        endpoints={[{ endpointId: LIVE_ENDPOINT, label: `concurrency · ${LIVE_ENDPOINT}` }]}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(LIVE_ENDPOINT)).toBeInTheDocument();
    });
    expect(screen.getByText(FRESH_NON_ROUTE_ENDPOINT)).toBeInTheDocument();
  });

  it('keeps a stale sample when its endpoint is still a live route', async () => {
    // A current route is always shown, even if its last probe is old — that is
    // diagnostic, not a ghost.
    vi.mocked(listRoutewiseProbeSamples).mockResolvedValue({
      samples: [
        sample({
          endpoint_id: LIVE_ENDPOINT,
          ok: false,
          ttft_ms: null,
          error: 'timeout',
          checked_at: '2026-06-29T00:00:00Z',
        }),
      ],
    });

    render(
      <RoutewiseSettingsPanel
        modelId="minimax-fast"
        endpoints={[{ endpointId: LIVE_ENDPOINT, label: `concurrency · ${LIVE_ENDPOINT}` }]}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(LIVE_ENDPOINT)).toBeInTheDocument();
    });
  });
});
