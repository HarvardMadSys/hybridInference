// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RoutewiseSettingsSection } from './RoutewiseSettingsSection';

vi.mock('@/lib/api/admin', () => ({
  listProviderRoutes: vi.fn(),
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
  listProviderRoutes,
  listRoutewiseProbeSamples,
  listRoutewiseSettings,
} from '@/lib/api/admin';
import type { ProviderRoute, RoutewiseSettingItem } from '@/lib/api/admin';
import { chooseValue } from '@/test-utils/select';

function route(overrides: Partial<ProviderRoute>): ProviderRoute {
  return {
    model_id: 'model-a',
    strategy: 'routewise',
    route_id: 'model-a:featherless-api',
    route_type: 'concurrency',
    provider: 'featherless',
    upstream_provider: 'featherless',
    key_provider: 'featherless',
    base_url: 'https://api.featherless.ai/v1',
    api_key_id: null,
    api_key: {
      id: null,
      provider: 'featherless',
      label: 'Provider default',
      key_prefix: null,
      source: 'default',
    },
    provider_model_id: 'provider/model-a',
    quota_limit: null,
    concurrency_limit: 1,
    endpoint_id: 'model-a:featherless-api',
    yaml_weight: 1,
    effective_weight: 1,
    source: 'yaml',
    updated_at: null,
    updated_by: null,
    ...overrides,
  } as ProviderRoute;
}

const setting: RoutewiseSettingItem = {
  key: 'routewise_budget_alpha',
  value: 0.55,
  value_type: 'float',
  default_value: 0.55,
  source: 'global_default',
  overridden: false,
  description: 'RouteWise LP cost budget interpolation',
  min: 0,
  max: 1,
};

beforeEach(() => {
  vi.mocked(listRoutewiseSettings).mockResolvedValue({ model_id: 'model-a', settings: [setting] });
  vi.mocked(listRoutewiseProbeSamples).mockResolvedValue({ samples: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('RoutewiseSettingsSection', () => {
  it('offers only RouteWise models, opens on the first, and switches model', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: [],
      routes: [
        route({ model_id: 'model-b', route_id: 'model-b:api', endpoint_id: 'model-b:api' }),
        route({ model_id: 'model-a' }),
        route({
          model_id: 'fixed-model',
          strategy: 'fixed',
          route_id: 'fixed-model:api',
          endpoint_id: 'fixed-model:api',
        }),
      ],
    });

    const onUnsavedChange = vi.fn();
    render(<RoutewiseSettingsSection onUnsavedChange={onUnsavedChange} />);

    const picker = await screen.findByLabelText('Selected model');
    expect(picker).toHaveTextContent('model-a');
    expect(await screen.findByLabelText('Cost budget alpha value')).toBeInTheDocument();
    expect(listRoutewiseSettings).toHaveBeenLastCalledWith('model-a');

    // A fixed model is not offered.
    fireEvent.click(picker);
    expect(screen.queryByRole('option', { name: 'fixed-model' })).not.toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });

    chooseValue(picker, 'model-b');
    await waitFor(() => expect(listRoutewiseSettings).toHaveBeenLastCalledWith('model-b'));
  });

  it('holds the model picker while a parameter draft is unsaved and reports it', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: [],
      routes: [route({ model_id: 'model-a' }), route({ model_id: 'model-b', endpoint_id: 'b' })],
    });
    const onUnsavedChange = vi.fn();
    render(<RoutewiseSettingsSection onUnsavedChange={onUnsavedChange} />);

    const input = await screen.findByLabelText('Cost budget alpha value');
    const picker = screen.getByLabelText('Selected model');
    expect(picker).toBeEnabled();
    expect(onUnsavedChange).toHaveBeenLastCalledWith(0);

    fireEvent.change(input, { target: { value: '0.4' } });
    expect(picker).toBeDisabled();
    expect(onUnsavedChange).toHaveBeenLastCalledWith(1);

    fireEvent.click(
      screen.getByRole('button', { name: 'Discard changes to RouteWise parameters' }),
    );
    expect(picker).toBeEnabled();
    expect(onUnsavedChange).toHaveBeenLastCalledWith(0);
  });

  it('shows an empty state when no model uses RouteWise', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: [],
      routes: [route({ model_id: 'fixed-model', strategy: 'fixed' })],
    });

    render(<RoutewiseSettingsSection />);

    expect(await screen.findByText('No RouteWise models available.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Selected model')).not.toBeInTheDocument();
    expect(listRoutewiseSettings).not.toHaveBeenCalled();
  });

  it('shows the load error and retries', async () => {
    vi.mocked(listProviderRoutes)
      .mockRejectedValueOnce(new Error('routes unavailable'))
      .mockResolvedValue({ provider_options: [], routes: [route({ model_id: 'model-a' })] });

    render(<RoutewiseSettingsSection />);

    expect(await screen.findByText('routes unavailable')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByLabelText('Cost budget alpha value')).toBeInTheDocument();
    expect(listProviderRoutes).toHaveBeenCalledTimes(2);
  });
  it('does not load until the sub-tab is first shown', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: [],
      routes: [route({ model_id: 'model-a' })],
    });

    const view = render(<RoutewiseSettingsSection active={false} />);
    expect(listProviderRoutes).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('Selected model')).not.toBeInTheDocument();

    view.rerender(<RoutewiseSettingsSection active />);
    expect(await screen.findByLabelText('Cost budget alpha value')).toBeInTheDocument();
    expect(listProviderRoutes).toHaveBeenCalledTimes(1);

    // Shown again later: no reload.
    view.rerender(<RoutewiseSettingsSection active={false} />);
    view.rerender(<RoutewiseSettingsSection active />);
    expect(listProviderRoutes).toHaveBeenCalledTimes(1);
  });

  it('labels probe endpoints with the provider display name', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: [
        {
          provider: 'featherless',
          label: 'Featherless',
          kind: 'featherless',
          key_provider: 'featherless',
          default_base_url: 'https://api.featherless.ai/v1',
        },
      ],
      routes: [route({ model_id: 'model-a' })],
    });

    render(<RoutewiseSettingsSection />);
    await screen.findByLabelText('Cost budget alpha value');

    fireEvent.click(screen.getByRole('combobox', { name: 'Endpoint' }));
    expect(
      await screen.findByRole('option', { name: 'concurrency · Featherless' }),
    ).toBeInTheDocument();
  });
});
