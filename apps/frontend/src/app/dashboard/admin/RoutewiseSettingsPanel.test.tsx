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
import { chooseOption, chooseValue } from '@/test-utils/select';

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

function saveParameters() {
  fireEvent.click(screen.getByRole('button', { name: 'Save changes to RouteWise parameters' }));
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

    chooseOption('Endpoint', 'second provider');

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
  it('saves an override for only the selected model on Save, with no confirmation', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'model-a',
      settings: [setting()],
    });
    vi.mocked(updateRoutewiseSetting).mockResolvedValue(
      setting({ value: 0.4, default_value: 0.55, source: 'runtime_override', overridden: true }),
    );

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    const value = await screen.findByLabelText('Cost budget alpha value');
    expect(value).toHaveValue(0.55);
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '0.4' },
    });
    expect(updateRoutewiseSetting).not.toHaveBeenCalled();
    saveParameters();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() => expect(updateRoutewiseSetting).toHaveBeenCalledTimes(1));
    expect(updateRoutewiseSetting).toHaveBeenCalledWith('model-a', 'routewise_budget_alpha', 0.4);
    expect(await screen.findByText('Source: Override')).toBeInTheDocument();
    expect(screen.getByLabelText('Cost budget alpha value')).toHaveValue(0.4);
    expect(screen.queryByRole('button', { name: /^Save changes/ })).not.toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith('Updated Cost budget alpha.');
  });

  it('edits values in place with no save bar until a change, and Discard writes nothing', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'model-a',
      settings: [setting()],
    });

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    const input = await screen.findByLabelText('Cost budget alpha value');
    expect(screen.queryByRole('button', { name: /^Edit / })).not.toBeInTheDocument();
    expect(input).toHaveValue(0.55);
    expect(screen.queryByRole('button', { name: /^Save changes/ })).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: '0.4' } });
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    expect(
      screen.getByRole('button', { name: 'Save changes to RouteWise parameters' }),
    ).toBeEnabled();
    // Typed back to the stored value, it is no change.
    fireEvent.change(input, { target: { value: '0.55' } });
    expect(screen.queryByRole('button', { name: /^Save changes/ })).not.toBeInTheDocument();

    fireEvent.change(input, { target: { value: '0.4' } });
    fireEvent.click(
      screen.getByRole('button', { name: 'Discard changes to RouteWise parameters' }),
    );
    expect(screen.getByLabelText('Cost budget alpha value')).toHaveValue(0.55);
    expect(screen.queryByRole('button', { name: /^Save changes/ })).not.toBeInTheDocument();
    expect(updateRoutewiseSetting).not.toHaveBeenCalled();
  });

  it('reports only unsaved changes or a write in flight to the parent', async () => {
    let finishProbe!: (value: RunRoutewiseProbeResponse) => void;
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'model-a',
      settings: [setting()],
    });
    vi.mocked(runRoutewiseProbe).mockReturnValueOnce(
      new Promise((resolve) => {
        finishProbe = resolve;
      }),
    );
    const onEditingChange = vi.fn();

    render(<RoutewiseSettingsPanel modelId="model-a" onEditingChange={onEditingChange} />);

    const input = await screen.findByLabelText('Cost budget alpha value');
    expect(onEditingChange).toHaveBeenLastCalledWith(false);
    fireEvent.change(input, { target: { value: '0.4' } });
    expect(onEditingChange).toHaveBeenLastCalledWith(true);
    fireEvent.change(input, { target: { value: '0.55' } });
    expect(onEditingChange).toHaveBeenLastCalledWith(false);

    fireEvent.click(screen.getByRole('button', { name: 'Run probe' }));
    expect(onEditingChange).toHaveBeenLastCalledWith(true);
    finishProbe({ results: [] });
    await waitFor(() => expect(onEditingChange).toHaveBeenLastCalledWith(false));
  });

  it('refuses to save an invalid value', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'model-a',
      settings: [setting()],
    });

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    await screen.findByLabelText('Cost budget alpha value');
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '2' },
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Must be ≤ 1.');
    saveParameters();

    expect(screen.getByText('Cost budget alpha: Must be ≤ 1.')).toBeVisible();
    expect(updateRoutewiseSetting).not.toHaveBeenCalled();
  });

  it('saves each changed setting once on Save', async () => {
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
    expect(screen.getByLabelText('Background probes enabled')).not.toBeChecked();
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '0.4' },
    });
    fireEvent.click(screen.getByLabelText('Background probes enabled'));
    expect(screen.getByText('2 unsaved changes')).toBeVisible();
    expect(updateRoutewiseSetting).not.toHaveBeenCalled();
    saveParameters();

    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /^Save changes/ })).not.toBeInTheDocument(),
    );
    expect(vi.mocked(updateRoutewiseSetting).mock.calls).toEqual([
      ['model-a', 'routewise_budget_alpha', 0.4],
      ['model-a', 'routewise_probe_enabled', true],
    ]);
    expect(screen.getByLabelText('Background probes enabled')).toBeChecked();
    expect(toast.success).toHaveBeenCalledWith('Updated 2 RouteWise parameters.');
  });

  it('holds a second Save, Discard, the fields and the probe while the save is in flight', async () => {
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
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '0.4' },
    });
    saveParameters();

    const saving = screen.getByRole('button', { name: 'Save changes to RouteWise parameters' });
    expect(saving).toHaveTextContent('Saving…');
    expect(saving).toBeDisabled();
    expect(
      screen.getByRole('button', { name: 'Discard changes to RouteWise parameters' }),
    ).toBeDisabled();
    expect(screen.getByLabelText('Cost budget alpha value')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Run probe' })).toBeDisabled();
    fireEvent.click(saving);
    expect(updateRoutewiseSetting).toHaveBeenCalledTimes(1);

    finish(setting({ value: 0.4, source: 'runtime_override', overridden: true }));
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /^Save changes/ })).not.toBeInTheDocument(),
    );
    expect(updateRoutewiseSetting).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('Cost budget alpha value')).toBeEnabled();
  });

  it('keeps the drafts and shows the error after a failure, and saves them again', async () => {
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'model-a',
      settings: [setting()],
    });
    vi.mocked(updateRoutewiseSetting)
      .mockRejectedValueOnce(new Error('write failed'))
      .mockResolvedValueOnce(setting({ value: 0.4, source: 'runtime_override', overridden: true }));

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    await screen.findByLabelText('Cost budget alpha value');
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '0.4' },
    });
    saveParameters();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Cost budget alpha could not be saved: write failed',
    );
    expect(screen.getByLabelText('Cost budget alpha value')).toHaveValue(0.4);
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    saveParameters();
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /^Save changes/ })).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(updateRoutewiseSetting).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText('Cost budget alpha value')).toHaveValue(0.4);
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
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '0.4' },
    });
    fireEvent.click(screen.getByLabelText('Background probes enabled'));
    saveParameters();

    expect(
      await screen.findByText(/Background probes could not be saved: write failed/),
    ).toHaveTextContent('1 change was saved; the rest are still unsaved.');
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    expect(screen.getByLabelText('Cost budget alpha value')).toHaveValue(0.4);
    expect(screen.getByLabelText('Background probes enabled')).toBeChecked();

    vi.mocked(updateRoutewiseSetting).mockResolvedValueOnce({
      ...probeSetting,
      value: true,
      source: 'runtime_override',
      overridden: true,
    });
    saveParameters();
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /^Save changes/ })).not.toBeInTheDocument(),
    );
    expect(vi.mocked(updateRoutewiseSetting).mock.calls.map(([, key]) => key)).toEqual([
      'routewise_budget_alpha',
      'routewise_probe_enabled',
      'routewise_probe_enabled',
    ]);
  });

  it('resets only an explicit override, once, on the click, and shows the inherited source', async () => {
    let finish!: (value: RoutewiseSettingItem) => void;
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
    vi.mocked(resetRoutewiseSetting).mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    const reset = await screen.findByRole('button', { name: 'Reset Cost budget alpha' });
    fireEvent.click(reset);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(resetRoutewiseSetting).toHaveBeenCalledWith('model-a', 'routewise_budget_alpha');
    // While it runs, the button and the field are held, and a second click resets nothing.
    expect(reset).toBeDisabled();
    expect(screen.getByLabelText('Cost budget alpha value')).toBeDisabled();
    fireEvent.click(reset);
    expect(resetRoutewiseSetting).toHaveBeenCalledTimes(1);

    finish(setting());
    expect(await screen.findByText('Source: models.yaml')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Reset Cost budget alpha' }),
    ).not.toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith('Reset Cost budget alpha to its inherited value.');
  });

  it('says why a reset failed and keeps the override, which can be reset again', async () => {
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
    vi.mocked(resetRoutewiseSetting)
      .mockRejectedValueOnce(new Error('backend unavailable'))
      .mockResolvedValueOnce(setting());

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    fireEvent.click(await screen.findByRole('button', { name: 'Reset Cost budget alpha' }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'Failed to reset Cost budget alpha: backend unavailable',
      ),
    );
    expect(screen.getByText('Source: Override')).toBeInTheDocument();
    expect(screen.getByLabelText('Cost budget alpha value')).toHaveValue(0.4);

    fireEvent.click(screen.getByRole('button', { name: 'Reset Cost budget alpha' }));
    expect(await screen.findByText('Source: models.yaml')).toBeInTheDocument();
    expect(resetRoutewiseSetting).toHaveBeenCalledTimes(2);
  });

  it('holds Reset while that setting has an unsaved change', async () => {
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

    render(<RoutewiseSettingsPanel modelId="model-a" />);

    const reset = await screen.findByRole('button', { name: 'Reset Cost budget alpha' });
    expect(reset).toBeEnabled();
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '0.3' },
    });
    expect(reset).toBeDisabled();
    expect(reset).toHaveAttribute('title', 'Save or discard the unsaved change first');

    fireEvent.click(
      screen.getByRole('button', { name: 'Discard changes to RouteWise parameters' }),
    );
    expect(reset).toBeEnabled();
    expect(resetRoutewiseSetting).not.toHaveBeenCalled();
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

    expect(await screen.findByLabelText('Cost budget alpha value')).toHaveValue(0.8);

    resolveModelA({
      model_id: 'model-a',
      settings: [setting({ value: 0.2, default_value: 0.2 })],
    });

    await waitFor(() => {
      expect(listRoutewiseSettings).toHaveBeenCalledWith('model-a');
      expect(listRoutewiseSettings).toHaveBeenCalledWith('model-b');
      expect(screen.getByLabelText('Cost budget alpha value')).toHaveValue(0.8);
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
    fireEvent.change(screen.getByLabelText('Cost budget alpha value'), {
      target: { value: '0.4' },
    });
    saveParameters();
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

    // The old write ends: the fields are free again, and nothing of it reaches this view.
    await waitFor(() => expect(screen.getByLabelText('Cost budget alpha value')).toBeEnabled());
    expect(screen.getByLabelText('Cost budget alpha value')).toHaveValue(0.7);
    expect(screen.getByText('Source: models.yaml')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(toast.success).not.toHaveBeenCalled();
    expect(updateRoutewiseSetting).toHaveBeenCalledTimes(1);
  });
});

describe('RoutewiseSettingsPanel probe run', () => {
  it('runs a probe for the chosen endpoint on the click, with no confirmation', async () => {
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

    // The panel says what a run costs before it is clicked.
    expect(screen.getByText(/counts against the providers' quotas/)).toBeInTheDocument();
    chooseValue(screen.getByLabelText('Endpoint'), LIVE_ENDPOINT);
    // Choosing the endpoint is a filter, not a probe.
    expect(runRoutewiseProbe).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Run probe' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    expect(await screen.findByText('Probe succeeded')).toBeInTheDocument();
    expect(runRoutewiseProbe).toHaveBeenCalledTimes(1);
    expect(runRoutewiseProbe).toHaveBeenCalledWith({
      model_id: 'model-a',
      endpoint_id: LIVE_ENDPOINT,
      idle_only: false,
    });
  });

  it('shows why a probe run failed, and runs it again on the next click', async () => {
    vi.mocked(runRoutewiseProbe)
      .mockRejectedValueOnce(new Error('No RouteWise router found'))
      .mockResolvedValueOnce({
        results: [
          { model_id: 'model-a', endpoint_id: LIVE_ENDPOINT, ok: true, ttft_ms: 100, error: null },
        ],
      });
    render(<RoutewiseSettingsPanel modelId="model-a" />);

    fireEvent.click(screen.getByRole('button', { name: 'Run probe' }));
    expect(await screen.findByText('No RouteWise router found')).toBeInTheDocument();
    expect(toast.error).toHaveBeenCalledWith('Probe failed: No RouteWise router found');
    expect(runRoutewiseProbe).toHaveBeenCalledWith({
      model_id: 'model-a',
      endpoint_id: null,
      idle_only: false,
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Run probe' }));
    expect(await screen.findByText('Probe succeeded')).toBeInTheDocument();
    expect(screen.queryByText('No RouteWise router found')).not.toBeInTheDocument();
    expect(runRoutewiseProbe).toHaveBeenCalledTimes(2);
  });
});
