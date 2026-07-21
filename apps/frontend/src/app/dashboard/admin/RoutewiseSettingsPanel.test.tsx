// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RoutewiseSettingsPanel } from './RoutewiseSettingsPanel';

vi.mock('@/lib/api/admin', () => ({
  listRoutewiseProbeSamples: vi.fn(),
  listRoutewiseSettings: vi.fn(),
  resetRoutewiseSetting: vi.fn(),
  runRoutewiseProbe: vi.fn(),
  updateRoutewiseSetting: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

import {
  listRoutewiseProbeSamples,
  listRoutewiseSettings,
  resetRoutewiseSetting,
  runRoutewiseProbe,
  updateRoutewiseSetting,
} from '@/lib/api/admin';
import type {
  RoutewiseProbeSampleItem,
  RoutewiseSettingItem,
  RunRoutewiseProbeResponse,
} from '@/lib/api/admin';

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

function setting(overrides: Partial<RoutewiseSettingItem> = {}): RoutewiseSettingItem {
  return {
    key: 'routewise_budget_alpha',
    value: 0.55,
    value_type: 'float',
    default_value: 0.55,
    source: 'model_config',
    overridden: false,
    description: 'RouteWise LP cost budget interpolation',
    min: 0,
    max: 1,
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(listRoutewiseSettings).mockResolvedValue({
    model_id: 'minimax-fast',
    settings: [],
  });
  vi.mocked(listRoutewiseProbeSamples).mockResolvedValue({ samples: [] });
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

  it('reloads probe samples with the newly selected endpoint', async () => {
    const secondEndpoint = 'minimax-fast:second-provider-api';

    render(
      <RoutewiseSettingsPanel
        modelId="minimax-fast"
        endpoints={[
          { endpointId: LIVE_ENDPOINT, label: 'live provider' },
          { endpointId: secondEndpoint, label: 'second provider' },
        ]}
      />,
    );

    await waitFor(() => {
      expect(listRoutewiseProbeSamples).toHaveBeenCalledWith({
        modelId: 'minimax-fast',
        endpointId: undefined,
        sinceSeconds: 86_400,
        limit: 100,
      });
    });
    vi.mocked(listRoutewiseProbeSamples).mockClear();

    fireEvent.change(screen.getByLabelText('Endpoint'), {
      target: { value: secondEndpoint },
    });

    await waitFor(() => {
      expect(listRoutewiseProbeSamples).toHaveBeenCalledWith({
        modelId: 'minimax-fast',
        endpointId: secondEndpoint,
        sinceSeconds: 86_400,
        limit: 100,
      });
    });
  });

  it('ignores stale probe data and probe completion after the model changes', async () => {
    let resolveModelAList!: (value: { samples: RoutewiseProbeSampleItem[] }) => void;
    let resolveModelARun!: (value: RunRoutewiseProbeResponse) => void;
    const modelAList = new Promise<{ samples: RoutewiseProbeSampleItem[] }>((resolve) => {
      resolveModelAList = resolve;
    });
    const modelARun = new Promise<RunRoutewiseProbeResponse>((resolve) => {
      resolveModelARun = resolve;
    });
    const modelBEndpoint = 'model-b:provider-api';
    vi.mocked(listRoutewiseProbeSamples).mockImplementation((options) => {
      if (options?.modelId === 'model-a') return modelAList;
      return Promise.resolve({
        samples: [
          sample({
            model_id: 'model-b',
            endpoint_id: modelBEndpoint,
            checked_at: '2026-07-01T20:00:00Z',
          }),
        ],
      });
    });
    vi.mocked(runRoutewiseProbe).mockReturnValue(modelARun);

    const view = render(
      <RoutewiseSettingsPanel
        modelId="model-a"
        endpoints={[{ endpointId: LIVE_ENDPOINT, label: LIVE_ENDPOINT }]}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Run probe' }));
    await waitFor(() => expect(runRoutewiseProbe).toHaveBeenCalled());

    view.rerender(
      <RoutewiseSettingsPanel
        modelId="model-b"
        endpoints={[{ endpointId: modelBEndpoint, label: 'model-b provider' }]}
      />,
    );
    expect(await screen.findByText(modelBEndpoint)).toBeInTheDocument();

    resolveModelAList({ samples: [sample({ endpoint_id: LIVE_ENDPOINT })] });
    resolveModelARun({
      results: [
        {
          model_id: 'model-a',
          endpoint_id: LIVE_ENDPOINT,
          ok: true,
          ttft_ms: 100,
          error: null,
        },
      ],
    });

    await waitFor(() => {
      expect(screen.getByText(modelBEndpoint)).toBeInTheDocument();
      expect(screen.queryByText(LIVE_ENDPOINT)).not.toBeInTheDocument();
      expect(screen.queryByText('Probe succeeded')).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Run probe' })).toBeEnabled();
    });
  });
});

describe('RoutewiseSettingsPanel model settings', () => {
  it('saves an override for only the selected model', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'model-a',
      settings: [setting()],
    });
    vi.mocked(updateRoutewiseSetting).mockResolvedValue(
      setting({ value: 0.4, default_value: 0.55, source: 'runtime_override', overridden: true }),
    );

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    const input = await screen.findByLabelText('Cost budget alpha value');
    fireEvent.change(input, { target: { value: '0.4' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Cost budget alpha' }));

    await waitFor(() => {
      expect(updateRoutewiseSetting).toHaveBeenCalledWith('model-a', 'routewise_budget_alpha', 0.4);
    });
    expect(await screen.findByText('Source: Override')).toBeInTheDocument();
  });

  it('resets only an explicit override and shows the inherited source', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'model-a',
      settings: [
        setting({
          value: 0.4,
          default_value: 0.55,
          source: 'runtime_override',
          overridden: true,
        }),
      ],
    });
    vi.mocked(resetRoutewiseSetting).mockResolvedValue(setting());

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    fireEvent.click(await screen.findByRole('button', { name: 'Reset Cost budget alpha' }));

    await waitFor(() => {
      expect(resetRoutewiseSetting).toHaveBeenCalledWith('model-a', 'routewise_budget_alpha');
    });
    expect(await screen.findByText('Source: models.yaml')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Reset Cost budget alpha' }),
    ).not.toBeInTheDocument();
  });

  it('reloads on model changes and ignores a stale response from the old model', async () => {
    let resolveModelA!: (value: { model_id: string; settings: RoutewiseSettingItem[] }) => void;
    const modelAResponse = new Promise<{
      model_id: string;
      settings: RoutewiseSettingItem[];
    }>((resolve) => {
      resolveModelA = resolve;
    });
    vi.mocked(listRoutewiseSettings).mockImplementation((modelId) => {
      if (modelId === 'model-a') return modelAResponse;
      return Promise.resolve({
        model_id: 'model-b',
        settings: [setting({ value: 0.8, default_value: 0.8 })],
      });
    });

    const view = render(<RoutewiseSettingsPanel modelId="model-a" />);
    view.rerender(<RoutewiseSettingsPanel modelId="model-b" />);

    const input = await screen.findByLabelText('Cost budget alpha value');
    expect(input).toHaveValue(0.8);

    resolveModelA({
      model_id: 'model-a',
      settings: [setting({ value: 0.2, default_value: 0.2 })],
    });

    await waitFor(() => {
      expect(listRoutewiseSettings).toHaveBeenCalledWith('model-a');
      expect(listRoutewiseSettings).toHaveBeenCalledWith('model-b');
      expect(input).toHaveValue(0.8);
    });
  });

  it('ignores a stale mutation after switching away from and back to the same model', async () => {
    let resolveOldMutation!: (value: RoutewiseSettingItem) => void;
    const oldMutation = new Promise<RoutewiseSettingItem>((resolve) => {
      resolveOldMutation = resolve;
    });
    let modelALoads = 0;
    vi.mocked(listRoutewiseSettings).mockImplementation((modelId) => {
      if (modelId === 'model-b') {
        return Promise.resolve({
          model_id: 'model-b',
          settings: [setting({ value: 0.8, default_value: 0.8 })],
        });
      }
      modelALoads += 1;
      const value = modelALoads === 1 ? 0.55 : 0.7;
      return Promise.resolve({
        model_id: 'model-a',
        settings: [setting({ value, default_value: value })],
      });
    });
    vi.mocked(updateRoutewiseSetting).mockReturnValue(oldMutation);

    const view = render(<RoutewiseSettingsPanel modelId="model-a" />);
    const initialInput = await screen.findByLabelText('Cost budget alpha value');
    fireEvent.change(initialInput, { target: { value: '0.4' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Cost budget alpha' }));
    await waitFor(() => expect(updateRoutewiseSetting).toHaveBeenCalled());

    view.rerender(<RoutewiseSettingsPanel modelId="model-b" />);
    await waitFor(() => expect(screen.getByLabelText('Cost budget alpha value')).toHaveValue(0.8));
    view.rerender(<RoutewiseSettingsPanel modelId="model-a" />);
    await waitFor(() => expect(screen.getByLabelText('Cost budget alpha value')).toHaveValue(0.7));

    resolveOldMutation(
      setting({
        value: 0.4,
        default_value: 0.55,
        source: 'runtime_override',
        overridden: true,
      }),
    );

    await waitFor(() => {
      expect(screen.getByLabelText('Cost budget alpha value')).toHaveValue(0.7);
      expect(screen.getByText('Source: models.yaml')).toBeInTheDocument();
    });
  });
});
