// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

import toast from 'react-hot-toast';
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

function editParameters() {
  fireEvent.click(screen.getByRole('button', { name: 'Edit RouteWise parameters' }));
}

function saveParameters() {
  fireEvent.click(screen.getByRole('button', { name: 'Save changes to RouteWise parameters' }));
}

function confirmIn(label: string) {
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: label }));
}

const probeSetting: RoutewiseSettingItem = {
  key: 'routewise_probe_enabled',
  value: false,
  value_type: 'bool',
  default_value: false,
  source: 'global_default',
  overridden: false,
  description: 'Enable RouteWise background active latency probes.',
  min: null,
  max: null,
};

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
    confirmIn('Run probe');
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
  it('saves an override for only the selected model after confirmation', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'model-a',
      settings: [setting()],
    });
    vi.mocked(updateRoutewiseSetting).mockResolvedValue(
      setting({ value: 0.4, default_value: 0.55, source: 'runtime_override', overridden: true }),
    );

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    const value = await screen.findByLabelText('Cost budget alpha value');
    expect(value.tagName).toBe('OUTPUT');
    expect(value).toHaveTextContent('0.55');
    editParameters();
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '0.4' },
    });
    saveParameters();

    const dialog = screen.getByRole('dialog', {
      name: 'Save 1 change to model-a RouteWise parameters?',
    });
    expect(dialog).toHaveTextContent('Cost budget alpha0.55 → 0.4');
    expect(updateRoutewiseSetting).not.toHaveBeenCalled();
    confirmIn('Confirm save');

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(updateRoutewiseSetting).toHaveBeenCalledTimes(1);
    expect(updateRoutewiseSetting).toHaveBeenCalledWith('model-a', 'routewise_budget_alpha', 0.4);
    expect(await screen.findByText('Source: Override')).toBeInTheDocument();
    expect(screen.getByLabelText('Cost budget alpha value')).toHaveTextContent('0.4');
    expect(toast.success).toHaveBeenCalledWith('Updated Cost budget alpha.');
  });

  it('keeps values read-only until Edit, and Cancel writes nothing', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'model-a',
      settings: [setting()],
    });

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    await screen.findByLabelText('Cost budget alpha value');
    expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Save changes/ })).not.toBeInTheDocument();

    editParameters();
    const input = screen.getByLabelText('Cost budget alpha value');
    expect(input).toHaveValue(0.55);
    expect(screen.getByText('No changes yet')).toBeVisible();
    fireEvent.change(input, { target: { value: '0.4' } });
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    // Typed back to the stored value, it is no change.
    fireEvent.change(input, { target: { value: '0.55' } });
    expect(
      screen.getByRole('button', { name: 'Save changes to RouteWise parameters' }),
    ).toBeDisabled();

    fireEvent.change(input, { target: { value: '0.4' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel editing RouteWise parameters' }));
    expect(screen.getByLabelText('Cost budget alpha value')).toHaveTextContent('0.55');
    expect(updateRoutewiseSetting).not.toHaveBeenCalled();
  });

  it('refuses to review an invalid value', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'model-a',
      settings: [setting()],
    });

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    await screen.findByLabelText('Cost budget alpha value');
    editParameters();
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '2' },
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Must be ≤ 1.');
    saveParameters();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByText('Cost budget alpha: Must be ≤ 1.')).toBeVisible();
    expect(updateRoutewiseSetting).not.toHaveBeenCalled();
  });

  it('keeps drafts on Back, and saves each changed setting once on Confirm', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'model-a',
      settings: [setting(), probeSetting],
    });
    vi.mocked(updateRoutewiseSetting).mockImplementation(async (_model, key, value) =>
      key === 'routewise_probe_enabled'
        ? { ...probeSetting, value, source: 'runtime_override', overridden: true }
        : setting({ value: value as number, source: 'runtime_override', overridden: true }),
    );

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    await screen.findByLabelText('Cost budget alpha value');
    expect(screen.getByLabelText('Background probes enabled')).toHaveTextContent('Off');
    editParameters();
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '0.4' },
    });
    fireEvent.click(screen.getByLabelText('Background probes enabled'));
    saveParameters();

    const dialog = screen.getByRole('dialog', {
      name: 'Save 2 changes to model-a RouteWise parameters?',
    });
    expect(dialog).toHaveTextContent('Background probesOff → On');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Back to editing' }));
    expect(screen.getByLabelText('Cost budget alpha value')).toHaveValue(0.4);
    expect(screen.getByLabelText('Background probes enabled')).toBeChecked();
    expect(updateRoutewiseSetting).not.toHaveBeenCalled();

    saveParameters();
    confirmIn('Confirm save');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(vi.mocked(updateRoutewiseSetting).mock.calls).toEqual([
      ['model-a', 'routewise_budget_alpha', 0.4],
      ['model-a', 'routewise_probe_enabled', true],
    ]);
    expect(screen.getByLabelText('Background probes enabled')).toHaveTextContent('On');
  });

  it('blocks a second Confirm and closing while the save is in flight', async () => {
    let finish!: (value: RoutewiseSettingItem) => void;
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'model-a',
      settings: [setting()],
    });
    vi.mocked(updateRoutewiseSetting).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    await screen.findByLabelText('Cost budget alpha value');
    editParameters();
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '0.4' },
    });
    saveParameters();
    confirmIn('Confirm save');

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('button', { name: 'Saving…' })).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: 'Back to editing' })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Saving…' }));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(updateRoutewiseSetting).toHaveBeenCalledTimes(1);

    finish(setting({ value: 0.4, source: 'runtime_override', overridden: true }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(updateRoutewiseSetting).toHaveBeenCalledTimes(1);
  });

  it('keeps the dialog and drafts after a failure, and retries from the dialog', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'model-a',
      settings: [setting()],
    });
    vi.mocked(updateRoutewiseSetting)
      .mockRejectedValueOnce(new Error('write failed'))
      .mockResolvedValueOnce(setting({ value: 0.4, source: 'runtime_override', overridden: true }));

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    await screen.findByLabelText('Cost budget alpha value');
    editParameters();
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '0.4' },
    });
    saveParameters();
    confirmIn('Confirm save');

    const dialog = screen.getByRole('dialog');
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Cost budget alpha could not be saved: write failed',
    );
    expect(screen.getByLabelText('Cost budget alpha value')).toHaveValue(0.4);

    confirmIn('Confirm save');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(updateRoutewiseSetting).toHaveBeenCalledTimes(2);
  });

  it('reports a partial save and retries only the unsaved setting', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'model-a',
      settings: [setting(), probeSetting],
    });
    vi.mocked(updateRoutewiseSetting)
      .mockResolvedValueOnce(setting({ value: 0.4, source: 'runtime_override', overridden: true }))
      .mockRejectedValueOnce(new Error('write failed'));

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    await screen.findByLabelText('Cost budget alpha value');
    editParameters();
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '0.4' },
    });
    fireEvent.click(screen.getByLabelText('Background probes enabled'));
    saveParameters();
    confirmIn('Confirm save');

    expect(
      await screen.findByText(/Background probes could not be saved: write failed/),
    ).toHaveTextContent('1 change was saved; the rest are still unsaved.');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    vi.mocked(updateRoutewiseSetting).mockResolvedValueOnce({
      ...probeSetting,
      value: true,
      source: 'runtime_override',
      overridden: true,
    });
    saveParameters();
    expect(
      screen.getByRole('dialog', { name: 'Save 1 change to model-a RouteWise parameters?' }),
    ).not.toHaveTextContent('Cost budget alpha');
    confirmIn('Confirm save');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(vi.mocked(updateRoutewiseSetting).mock.calls.map(([, key]) => key)).toEqual([
      'routewise_budget_alpha',
      'routewise_probe_enabled',
      'routewise_probe_enabled',
    ]);
  });

  it('resets only an explicit override, after confirmation, and shows the inherited source', async () => {
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
    const dialog = screen.getByRole('dialog', { name: 'Reset Cost budget alpha for model-a?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(resetRoutewiseSetting).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Reset Cost budget alpha' }));
    confirmIn('Reset');

    await waitFor(() => {
      expect(resetRoutewiseSetting).toHaveBeenCalledWith('model-a', 'routewise_budget_alpha');
    });
    expect(resetRoutewiseSetting).toHaveBeenCalledTimes(1);
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

    expect(await screen.findByLabelText('Cost budget alpha value')).toHaveTextContent('0.8');

    resolveModelA({
      model_id: 'model-a',
      settings: [setting({ value: 0.2, default_value: 0.2 })],
    });

    await waitFor(() => {
      expect(listRoutewiseSettings).toHaveBeenCalledWith('model-a');
      expect(listRoutewiseSettings).toHaveBeenCalledWith('model-b');
      expect(screen.getByLabelText('Cost budget alpha value')).toHaveTextContent('0.8');
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
    await screen.findByLabelText('Cost budget alpha value');
    editParameters();
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '0.4' },
    });
    saveParameters();
    confirmIn('Confirm save');
    await waitFor(() => expect(updateRoutewiseSetting).toHaveBeenCalled());

    view.rerender(<RoutewiseSettingsPanel modelId="model-b" />);
    await waitFor(() =>
      expect(screen.getByLabelText('Cost budget alpha value')).toHaveTextContent('0.8'),
    );
    view.rerender(<RoutewiseSettingsPanel modelId="model-a" />);
    await waitFor(() =>
      expect(screen.getByLabelText('Cost budget alpha value')).toHaveTextContent('0.7'),
    );

    resolveOldMutation(
      setting({
        value: 0.4,
        default_value: 0.55,
        source: 'runtime_override',
        overridden: true,
      }),
    );

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByLabelText('Cost budget alpha value')).toHaveTextContent('0.7');
    expect(screen.getByText('Source: models.yaml')).toBeInTheDocument();
    expect(updateRoutewiseSetting).toHaveBeenCalledTimes(1);
  });
});

describe('RoutewiseSettingsPanel probe run', () => {
  it('asks before sending live probe traffic and names the endpoint', async () => {
    vi.mocked(runRoutewiseProbe).mockResolvedValue({
      results: [
        { model_id: 'model-a', endpoint_id: LIVE_ENDPOINT, ok: true, ttft_ms: 100, error: null },
      ],
    });
    render(
      <RoutewiseSettingsPanel
        modelId="model-a"
        endpoints={[{ endpointId: LIVE_ENDPOINT, label: 'live provider' }]}
      />,
    );

    fireEvent.change(screen.getByLabelText('Endpoint'), { target: { value: LIVE_ENDPOINT } });
    fireEvent.click(screen.getByRole('button', { name: 'Run probe' }));
    const dialog = screen.getByRole('dialog', { name: 'Run a RouteWise probe for model-a?' });
    expect(dialog).toHaveTextContent('Sends a short live request to live provider');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(runRoutewiseProbe).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Run probe' }));
    confirmIn('Run probe');

    expect(await screen.findByText('Probe succeeded')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(runRoutewiseProbe).toHaveBeenCalledTimes(1);
    expect(runRoutewiseProbe).toHaveBeenCalledWith({
      model_id: 'model-a',
      endpoint_id: LIVE_ENDPOINT,
      idle_only: false,
    });
  });

  it('keeps a failed probe run in the dialog so it can be retried', async () => {
    vi.mocked(runRoutewiseProbe)
      .mockRejectedValueOnce(new Error('No RouteWise router found'))
      .mockResolvedValueOnce({ results: [] });
    render(<RoutewiseSettingsPanel modelId="model-a" />);

    fireEvent.click(screen.getByRole('button', { name: 'Run probe' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('every endpoint of model-a');
    confirmIn('Run probe');

    expect(await within(screen.getByRole('dialog')).findByRole('alert')).toHaveTextContent(
      'No RouteWise router found',
    );
    confirmIn('Run probe');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(runRoutewiseProbe).toHaveBeenCalledTimes(2);
  });
});
