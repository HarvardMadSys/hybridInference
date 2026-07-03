// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ProviderRoutesTab } from './ProviderRoutesTab';

vi.mock('@/lib/api/admin', () => ({
  clearRouteWeight: vi.fn(),
  createProviderRouteModel: vi.fn(),
  createProviderRouteCandidate: vi.fn(),
  deleteProviderRoute: vi.fn(),
  deleteProviderRouteCandidate: vi.fn(),
  getRoutewiseDecisions: vi.fn(() =>
    Promise.resolve({
      model_id: 'minimax-fast',
      range: '24h',
      bucket_seconds: 3600,
      total_requests: 0,
      unattributed_requests: 0,
      lp_status_counts: {},
      selection_share: [],
      hedge_summary: {
        hedged: 0,
        hedge_rate: 0,
        backup_won: 0,
        backup_win_rate: 0,
        median_hedge_delay_ms: null,
      },
      buckets: [],
    }),
  ),
  listOpenRouterProviderOptions: vi.fn(),
  listProviderKeys: vi.fn(),
  listProviderRoutes: vi.fn(),
  listRecentRequests: vi.fn(() =>
    Promise.resolve({ requests: [], total: 0, limit: 20, offset: 0 }),
  ),
  listRouteWeights: vi.fn(),
  listRoutewiseProbeSamples: vi.fn(),
  listRoutewiseSettings: vi.fn(() => Promise.resolve({ settings: [] })),
  runRoutewiseProbe: vi.fn(),
  setRouteWeight: vi.fn(),
  updateRoutewiseSetting: vi.fn(),
  updateProviderRoute: vi.fn(),
  updateProviderRouteCandidate: vi.fn(),
  updateProviderRouteStrategy: vi.fn(),
  verifyProviderRoute: vi.fn(),
  verifyProviderRouteModel: vi.fn(),
  verifyProviderRouteCandidate: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

import {
  clearRouteWeight,
  createProviderRouteModel,
  createProviderRouteCandidate,
  deleteProviderRoute,
  deleteProviderRouteCandidate,
  listOpenRouterProviderOptions,
  listProviderKeys,
  listProviderRoutes,
  listRouteWeights,
  listRoutewiseProbeSamples,
  listRoutewiseSettings,
  runRoutewiseProbe,
  setRouteWeight,
  updateProviderRoute,
  updateProviderRouteCandidate,
  updateProviderRouteStrategy,
  updateRoutewiseSetting,
  verifyProviderRoute,
  verifyProviderRouteModel,
  verifyProviderRouteCandidate,
} from '@/lib/api/admin';
import type { ListProviderApiKeysResponse } from '@/lib/api/admin';

const providerOptions = [
  {
    provider: 'featherless',
    label: 'Featherless',
    kind: 'featherless',
    key_provider: 'featherless',
    default_base_url: 'https://api.featherless.ai/v1',
  },
  {
    provider: 'openrouter',
    label: 'OpenRouter',
    kind: 'openrouter',
    key_provider: 'openrouter',
    default_base_url: 'https://openrouter.ai/api/v1',
  },
];

const openRouterProviderOptions = [
  { provider: 'deepinfra', label: 'DeepInfra' },
  { provider: 'parasail', label: 'Parasail' },
];

const providerKeysResponse = (provider?: string): ListProviderApiKeysResponse => {
  const keys: ListProviderApiKeysResponse['keys'] = [];
  if (provider === 'openrouter') {
    keys.push({
      id: 'key-1',
      provider: 'openrouter',
      key_prefix: 'sk-or...1234',
      label: 'staging',
      source: 'db',
      status: 'active',
      created_at: null,
    });
  }
  return { provider: provider ?? null, keys };
};

const discoveredOpenRouterProviderOptions = [
  { provider: 'inceptron', label: 'Inceptron' },
  { provider: 'akashml', label: 'AkashML' },
  { provider: 'deepinfra', label: 'DeepInfra' },
  { provider: 'chutes', label: 'Chutes' },
  { provider: 'parasail', label: 'Parasail' },
];

function fillRuntimeModelPricing() {
  fireEvent.change(screen.getByLabelText('Prompt $/M'), { target: { value: '0.14' } });
  fireEvent.change(screen.getByLabelText('Completion $/M'), { target: { value: '0.28' } });
}

const route = {
  model_id: 'minimax-fast',
  strategy: 'routewise',
  route_id: 'minimax-fast:featherless-api',
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
    source: 'default' as const,
  },
  provider_model_id: 'MiniMaxAI/MiniMax-M2.5',
  quota_limit: null,
  concurrency_limit: 1,
  endpoint_id: 'minimax-fast:featherless-api',
  yaml_weight: 1,
  effective_weight: 1,
  source: 'yaml' as const,
  updated_at: null,
  updated_by: null,
};

describe('ProviderRoutesTab', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listRouteWeights).mockResolvedValue([
      {
        model_id: route.model_id,
        strategy: route.strategy,
        endpoint_id: route.endpoint_id,
        provider: route.provider,
        base_url: route.base_url,
        yaml_weight: route.yaml_weight,
        override_weight: null,
        effective_weight: route.effective_weight,
      },
    ]);
    vi.mocked(listOpenRouterProviderOptions).mockResolvedValue({
      provider_model_id: 'minimax/minimax-m2.5',
      providers: discoveredOpenRouterProviderOptions,
    });
    vi.mocked(listRoutewiseProbeSamples).mockResolvedValue({ samples: [] });
    vi.mocked(runRoutewiseProbe).mockResolvedValue({ results: [] });
    vi.mocked(verifyProviderRoute).mockResolvedValue({ ok: true });
    vi.mocked(verifyProviderRouteCandidate).mockResolvedValue({ ok: true });
    vi.mocked(verifyProviderRouteModel).mockResolvedValue({ ok: true });
  });

  it('renders routewise provider candidates without weight columns', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'featherless', keys: [] });

    render(<ProviderRoutesTab />);

    expect(await screen.findByText('minimax-fast')).toBeInTheDocument();
    expect(screen.getByText('Featherless')).toBeInTheDocument();
    expect(screen.getByText('Configured default featherless key')).toBeInTheDocument();
    expect(screen.queryByText('Effective')).not.toBeInTheDocument();
  });

  it('hides fixed weight when adding a routewise provider route', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));

    expect(screen.getByLabelText('Routing policy')).toHaveValue('routewise');
    expect(screen.queryByLabelText('Fixed weight')).not.toBeInTheDocument();
  });

  it('renders settings and runs RouteWise probes when requested', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'featherless', keys: [] });
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      settings: [
        {
          key: 'routewise_budget_alpha',
          value: 0.75,
          value_type: 'float',
          default_value: 0.75,
          description: 'RouteWise LP cost budget interpolation.',
          min: 0,
          max: 1,
        },
        {
          key: 'routewise_latency_slo_sec',
          value: 3,
          value_type: 'float',
          default_value: 3,
          description: 'Latency SLO in seconds for Routewise LP decisions.',
          min: 0.1,
          max: null,
        },
        {
          key: 'routewise_probe_enabled',
          value: false,
          value_type: 'bool',
          default_value: false,
          description: 'Enable RouteWise background active latency probes.',
          min: null,
          max: null,
        },
      ],
    });
    vi.mocked(listRoutewiseProbeSamples).mockResolvedValue({
      samples: [
        {
          model_id: 'minimax-fast',
          endpoint_id: 'minimax-fast:featherless-api',
          ok: true,
          ttft_ms: 123.4,
          error: null,
          checked_at: '2026-06-22T00:00:00Z',
        },
        {
          model_id: 'minimax-fast',
          endpoint_id: 'minimax-fast:featherless-api',
          ok: false,
          ttft_ms: null,
          error: 'RuntimeError',
          checked_at: '2026-06-21T23:59:00Z',
        },
        {
          model_id: 'minimax-fast',
          endpoint_id: 'minimax-fast:openrouter[akashml]-api',
          ok: false,
          ttft_ms: null,
          error:
            '{"error":{"message":"Provider returned error","code":429,"metadata":{"raw":"/-m2.5 is temporarily rate-limited upstream. Please retry shortly. diagnostic-tail-that-should-be-truncated"}}}',
          checked_at: '2026-06-22T00:00:01Z',
        },
      ],
    });
    vi.mocked(runRoutewiseProbe).mockResolvedValue({
      results: [
        {
          model_id: 'minimax-fast',
          endpoint_id: 'minimax-fast:featherless-api',
          ok: true,
          ttft_ms: 120,
          error: null,
        },
      ],
    });
    vi.mocked(updateRoutewiseSetting).mockImplementation(async (key, value) => ({
      key,
      value,
      value_type: key === 'routewise_probe_enabled' ? 'bool' : 'float',
      default_value: key === 'routewise_probe_enabled' ? false : 0.75,
      description:
        key === 'routewise_probe_enabled'
          ? 'Enable RouteWise background active latency probes.'
          : 'RouteWise LP cost budget interpolation.',
      min: key === 'routewise_probe_enabled' ? null : 0,
      max: key === 'routewise_probe_enabled' ? null : 1,
    }));

    render(<ProviderRoutesTab showRoutewiseSettings />);

    expect(await screen.findByText('RouteWise parameters')).toBeInTheDocument();
    const alphaInput = await screen.findByLabelText('Cost budget alpha value');
    expect(alphaInput).toHaveValue(0.75);

    fireEvent.change(alphaInput, { target: { value: '0.4' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Cost budget alpha' }));

    await waitFor(() => {
      expect(updateRoutewiseSetting).toHaveBeenCalledWith('routewise_budget_alpha', 0.4);
    });
    expect(alphaInput).toHaveValue(0.4);

    const probeToggle = screen.getByLabelText('Background probes enabled');
    expect(probeToggle).not.toBeChecked();
    fireEvent.click(probeToggle);
    fireEvent.click(screen.getByRole('button', { name: 'Save Background probes' }));

    await waitFor(() => {
      expect(updateRoutewiseSetting).toHaveBeenCalledWith('routewise_probe_enabled', true);
    });

    expect((await screen.findAllByText('minimax-fast:featherless-api')).length).toBeGreaterThan(0);
    expect(screen.getByText('123 ms')).toBeInTheDocument();
    expect(screen.queryByText('RuntimeError')).not.toBeInTheDocument();
    expect(screen.getByText(/temporarily rate-limited upstream/)).toBeInTheDocument();
    expect(screen.queryByText(/diagnostic-tail-that-should-be-truncated/)).not.toBeInTheDocument();
    expect(listRoutewiseProbeSamples).toHaveBeenCalledWith(expect.objectContaining({ limit: 100 }));

    fireEvent.click(screen.getByRole('button', { name: 'Run probe' }));

    await waitFor(() => {
      expect(runRoutewiseProbe).toHaveBeenCalledWith({
        model_id: 'minimax-fast',
        endpoint_id: null,
        idle_only: false,
      });
    });
    expect(await screen.findByText('Probe succeeded')).toBeInTheDocument();
  });

  it('shows fixed weight when adding a fixed provider route', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [{ ...route, strategy: 'fixed' }],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));

    expect(screen.getByLabelText('Routing policy')).toHaveValue('fixed');
    const routeTypeSelect = screen.getByLabelText('Route type');
    expect(within(routeTypeSelect).getByRole('option', { name: 'on_demand' })).toBeInTheDocument();
    expect(
      within(routeTypeSelect).queryByRole('option', { name: 'concurrency' }),
    ).not.toBeInTheDocument();
    expect(
      within(routeTypeSelect).queryByRole('option', { name: 'quota' }),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText('Fixed weight')).toHaveValue(1);
  });

  it('edits fixed route weights inline', async () => {
    const fixedRoute = { ...route, strategy: 'fixed' };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [fixedRoute],
    });
    vi.mocked(listRouteWeights).mockResolvedValue([
      {
        model_id: 'minimax-fast',
        strategy: 'fixed',
        endpoint_id: 'minimax-fast:featherless-api',
        provider: 'featherless',
        base_url: 'https://api.featherless.ai/v1',
        yaml_weight: 1,
        override_weight: null,
        effective_weight: 1,
      },
    ]);
    vi.mocked(setRouteWeight).mockResolvedValue({
      model_id: 'minimax-fast',
      strategy: 'fixed',
      endpoint_id: 'minimax-fast:featherless-api',
      provider: 'featherless',
      base_url: 'https://api.featherless.ai/v1',
      yaml_weight: 1,
      override_weight: 3,
      effective_weight: 3,
    });
    vi.mocked(clearRouteWeight).mockResolvedValue({
      model_id: 'minimax-fast',
      strategy: 'fixed',
      endpoint_id: 'minimax-fast:featherless-api',
      provider: 'featherless',
      base_url: 'https://api.featherless.ai/v1',
      yaml_weight: 1,
      override_weight: null,
      effective_weight: 1,
    });

    render(<ProviderRoutesTab />);

    const input = await screen.findByLabelText('Runtime weight for minimax-fast:featherless-api');
    expect(input).toHaveValue(1);
    fireEvent.change(input, { target: { value: '3' } });
    fireEvent.click(
      screen.getByRole('button', { name: 'Save minimax-fast:featherless-api weight' }),
    );

    await waitFor(() => {
      expect(setRouteWeight).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:featherless-api',
        3,
      );
    });
    expect(
      await screen.findByLabelText('Runtime weight for minimax-fast:featherless-api'),
    ).toHaveValue(3);

    fireEvent.click(
      await screen.findByRole('button', {
        name: 'Clear minimax-fast:featherless-api weight override',
      }),
    );

    await waitFor(() => {
      expect(clearRouteWeight).toHaveBeenCalledWith('minimax-fast', 'minimax-fast:featherless-api');
    });
  });

  it('disables and enables routewise config routes with weight overrides', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(setRouteWeight).mockResolvedValue({
      model_id: 'minimax-fast',
      strategy: 'routewise',
      endpoint_id: 'minimax-fast:featherless-api',
      provider: 'featherless',
      base_url: 'https://api.featherless.ai/v1',
      yaml_weight: 1,
      override_weight: 0,
      effective_weight: 0,
    });
    vi.mocked(clearRouteWeight).mockResolvedValue({
      model_id: 'minimax-fast',
      strategy: 'routewise',
      endpoint_id: 'minimax-fast:featherless-api',
      provider: 'featherless',
      base_url: 'https://api.featherless.ai/v1',
      yaml_weight: 1,
      override_weight: null,
      effective_weight: 1,
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Disable' }));

    await waitFor(() => {
      expect(setRouteWeight).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:featherless-api',
        0,
      );
    });
    expect(await screen.findByText('Disabled')).toBeInTheDocument();

    fireEvent.click(await screen.findByRole('button', { name: 'Enable' }));

    await waitFor(() => {
      expect(clearRouteWeight).toHaveBeenCalledWith('minimax-fast', 'minimax-fast:featherless-api');
    });
  });

  it('opens the provider route editor in a dialog', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'featherless', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));

    const dialog = await screen.findByRole('dialog', { name: 'Edit provider route' });
    expect(dialog).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Override provider')).toHaveValue('featherless');
    expect(within(dialog).getByLabelText('Provider model ID')).toHaveValue(
      'MiniMaxAI/MiniMax-M2.5',
    );
    expect(within(dialog).getByLabelText('Local concurrency limit')).toHaveValue(1);
  });

  it('opens the add provider form in a dialog', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [{ ...route, strategy: 'fixed' }],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));

    const dialog = await screen.findByRole('dialog', { name: 'Add provider route' });
    expect(dialog).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Route type')).toBeInTheDocument();
  });

  it('opens the create model form in a dialog', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [{ ...route, strategy: 'fixed' }],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Create model' }));

    const dialog = await screen.findByRole('dialog', { name: 'Create model' });
    expect(dialog).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Model ID')).toBeInTheDocument();
  });

  it('updates provider route target', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockImplementation(async (provider?: string) =>
      providerKeysResponse(provider),
    );
    vi.mocked(updateProviderRoute).mockResolvedValue({
      ...route,
      provider: 'featherless',
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      api_key: {
        id: 'key-1',
        provider: 'openrouter',
        label: 'staging',
        key_prefix: 'sk-or...1234',
        source: 'db',
      },
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: null,
      concurrency_limit: 3,
      endpoint_id: 'minimax-fast:openrouter[parasail]-api',
      source: 'override',
      updated_by: '127.0.0.1',
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Override provider'), {
      target: { value: 'openrouter' },
    });
    fireEvent.change(screen.getByLabelText('OpenRouter routing'), {
      target: { value: 'provider:parasail' },
    });
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });
    fireEvent.change(screen.getByLabelText('Local concurrency limit'), {
      target: { value: '3' },
    });

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-1' } });
    expect(screen.getByLabelText('Base URL')).toHaveValue('https://openrouter.ai/api/v1');
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() => {
      expect(updateProviderRoute).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:featherless-api',
        {
          upstream_provider: 'openrouter',
          openrouter_provider: 'parasail',
          openrouter_sort: null,
          base_url: 'https://openrouter.ai/api/v1',
          api_key_id: 'key-1',
          provider_model_id: 'minimax/minimax-m2.5',
          quota_limit: null,
          concurrency_limit: 3,
        },
      );
    });
    expect(screen.getByLabelText('OpenRouter routing')).toHaveValue('provider:parasail');
  });

  it('verifies provider route target without applying it', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({
      provider: 'openrouter',
      keys: [
        {
          id: 'key-1',
          provider: 'openrouter',
          key_prefix: 'sk-or...1234',
          label: 'staging',
          source: 'db',
          status: 'active',
          created_at: null,
        },
      ],
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Override provider'), {
      target: { value: 'openrouter' },
    });
    fireEvent.change(screen.getByLabelText('OpenRouter routing'), {
      target: { value: 'provider:parasail' },
    });
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });
    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));

    await waitFor(() => {
      expect(verifyProviderRoute).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:featherless-api',
        {
          upstream_provider: 'openrouter',
          openrouter_provider: 'parasail',
          openrouter_sort: null,
          base_url: 'https://openrouter.ai/api/v1',
          api_key_id: 'key-1',
          provider_model_id: 'minimax/minimax-m2.5',
          quota_limit: null,
          concurrency_limit: 1,
        },
      );
    });
    expect(updateProviderRoute).not.toHaveBeenCalled();
    expect(await screen.findByRole('button', { name: 'Verified' })).toHaveClass('bg-emerald-600');
  });

  it('clears stale provider model ID when switching to a provider without a mapping', async () => {
    const openRouterRoute = {
      ...route,
      route_id: 'minimax-fast:openrouter[parasail]-api',
      route_type: 'on_demand',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      provider_model_id: 'minimax/minimax-m2.5',
      endpoint_id: 'minimax-fast:openrouter[parasail]-api',
    };
    const tencentOption = {
      provider: 'tencent_token_plan',
      label: 'Tencent Token Plan',
      kind: 'openai_compat',
      key_provider: 'tencent_token_plan',
      default_base_url: 'https://api.lkeap.cloud.tencent.com/plan/v3',
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: [...providerOptions, tencentOption],
      openrouter_provider_options: openRouterProviderOptions,
      routes: [openRouterRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({
      provider: 'tencent_token_plan',
      keys: [
        {
          id: 'key-tencent',
          provider: 'tencent_token_plan',
          key_prefix: 'tencent...1234',
          label: 'tencent',
          source: 'db',
          status: 'active',
          created_at: null,
        },
      ],
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByLabelText('Provider model ID')).toHaveValue('minimax/minimax-m2.5');

    fireEvent.change(screen.getByLabelText('Override provider'), {
      target: { value: 'tencent_token_plan' },
    });

    expect(screen.getByLabelText('Provider model ID')).toHaveValue('');
    expect(screen.getByLabelText('Base URL')).toHaveValue(
      'https://api.lkeap.cloud.tencent.com/plan/v3',
    );
    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('tencent_token_plan');
    });
  });

  it('submits local daily quota for quota provider overrides', async () => {
    const quotaRoute = {
      ...route,
      route_id: 'minimax-fast:chutes-api',
      route_type: 'quota',
      provider: 'chutes',
      upstream_provider: 'chutes',
      key_provider: 'chutes',
      base_url: 'https://llm.chutes.ai/v1',
      provider_model_id: 'MiniMaxAI/MiniMax-M2.5-TEE',
      quota_limit: 5000,
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: [
        {
          provider: 'chutes',
          label: 'Chutes',
          kind: 'chutes',
          key_provider: 'chutes',
          default_base_url: 'https://llm.chutes.ai/v1',
        },
        ...providerOptions,
      ],
      openrouter_provider_options: openRouterProviderOptions,
      routes: [quotaRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });
    vi.mocked(updateProviderRoute).mockResolvedValue({
      ...quotaRoute,
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: 8000,
      source: 'override',
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Override provider'), {
      target: { value: 'openrouter' },
    });
    fireEvent.change(screen.getByLabelText('OpenRouter routing'), {
      target: { value: 'provider:parasail' },
    });
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });
    fireEvent.change(screen.getByLabelText('Local daily quota'), {
      target: { value: '8000' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() => {
      expect(updateProviderRoute).toHaveBeenCalledWith('minimax-fast', 'minimax-fast:chutes-api', {
        upstream_provider: 'openrouter',
        openrouter_provider: 'parasail',
        openrouter_sort: null,
        base_url: 'https://openrouter.ai/api/v1',
        api_key_id: null,
        provider_model_id: 'minimax/minimax-m2.5',
        quota_limit: 8000,
        concurrency_limit: null,
      });
    });
  });

  it('resets an override to the config route', async () => {
    const overrideRoute = {
      ...route,
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      api_key: {
        id: 'key-1',
        provider: 'openrouter',
        label: 'staging',
        key_prefix: 'sk-or...1234',
        source: 'db' as const,
      },
      provider_model_id: 'minimax/minimax-m2.5',
      source: 'override' as const,
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [overrideRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });
    vi.mocked(deleteProviderRoute).mockResolvedValue(route);

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Reset config' }));

    await waitFor(() => {
      expect(deleteProviderRoute).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:featherless-api',
      );
    });
    await waitFor(() => {
      expect(screen.getAllByText('Configured default featherless key').length).toBeGreaterThan(0);
    });
    expect(screen.queryByText('Edit provider route')).not.toBeInTheDocument();
  });

  it('adds a runtime provider route', async () => {
    const deepinfraRoute = {
      ...route,
      route_id: 'minimax-fast:openrouter[deepinfra]-api',
      route_type: 'on_demand',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: 'deepinfra',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      provider_model_id: 'minimax/minimax-m2.5',
      endpoint_id: 'minimax-fast:openrouter[deepinfra]-api',
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route, deepinfraRoute],
    });
    vi.mocked(listProviderKeys).mockImplementation(async (provider?: string) =>
      providerKeysResponse(provider),
    );
    vi.mocked(createProviderRouteCandidate).mockResolvedValue({
      ...route,
      route_id: 'minimax-fast:openrouter[inceptron]-api',
      route_type: 'on_demand',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: 'inceptron',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      api_key: {
        id: 'key-1',
        provider: 'openrouter',
        label: 'staging',
        key_prefix: 'sk-or...1234',
        source: 'db',
      },
      provider_model_id: 'minimax/minimax-m2.5',
      endpoint_id: 'minimax-fast:openrouter[inceptron]-api',
      source: 'runtime',
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));

    await waitFor(() => {
      expect(listOpenRouterProviderOptions).toHaveBeenCalledWith('minimax/minimax-m2.5');
    });
    const openRouterSelect = screen.getByLabelText('OpenRouter routing');
    await waitFor(() => {
      expect(
        within(openRouterSelect).getByRole('option', { name: 'Provider: Inceptron' }),
      ).toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText('OpenRouter routing'), {
      target: { value: 'provider:inceptron' },
    });

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() => {
      expect(createProviderRouteCandidate).toHaveBeenCalledWith('minimax-fast', {
        route_type: 'on_demand',
        upstream_provider: 'openrouter',
        openrouter_provider: 'inceptron',
        openrouter_sort: null,
        base_url: 'https://openrouter.ai/api/v1',
        api_key_id: 'key-1',
        provider_model_id: 'minimax/minimax-m2.5',
        quota_limit: null,
        concurrency_limit: null,
        weight: 1,
      });
    });
    expect(await screen.findByText('Runtime added')).toBeInTheDocument();
  });

  it('adds a runtime OpenRouter provider route as concurrency', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockImplementation(async (provider?: string) =>
      providerKeysResponse(provider),
    );
    vi.mocked(createProviderRouteCandidate).mockResolvedValue({
      ...route,
      route_id: 'minimax-fast:openrouter[parasail]-api',
      route_type: 'concurrency',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      api_key: {
        id: 'key-1',
        provider: 'openrouter',
        label: 'staging',
        key_prefix: 'sk-or...1234',
        source: 'db',
      },
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: null,
      concurrency_limit: 2,
      endpoint_id: 'minimax-fast:openrouter[parasail]-api',
      source: 'runtime',
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));
    fireEvent.change(screen.getByLabelText('Route type'), { target: { value: 'concurrency' } });

    const providerSelect = screen.getByLabelText('Provider');
    expect(providerSelect).toHaveValue('openrouter');
    expect(within(providerSelect).getByRole('option', { name: 'OpenRouter' })).toBeInTheDocument();
    const routingSelect = screen.getByLabelText('OpenRouter routing');
    expect(routingSelect).toHaveValue('provider:deepinfra');
    expect(within(routingSelect).queryByRole('option', { name: 'Auto' })).not.toBeInTheDocument();
    expect(
      within(routingSelect).queryByRole('option', { name: 'Sort by price' }),
    ).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });
    fireEvent.change(routingSelect, {
      target: { value: 'provider:parasail' },
    });
    fireEvent.change(screen.getByLabelText('Concurrency limit'), {
      target: { value: '2' },
    });

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() => {
      expect(createProviderRouteCandidate).toHaveBeenCalledWith('minimax-fast', {
        route_type: 'concurrency',
        upstream_provider: 'openrouter',
        openrouter_provider: 'parasail',
        openrouter_sort: null,
        base_url: 'https://openrouter.ai/api/v1',
        api_key_id: 'key-1',
        provider_model_id: 'minimax/minimax-m2.5',
        quota_limit: null,
        concurrency_limit: 2,
        weight: 1,
      });
    });
    expect(await screen.findByText('Runtime added')).toBeInTheDocument();
  });

  it('keeps OpenRouter endpoint variant pins from discovery', async () => {
    const deepinfraRoute = {
      ...route,
      route_id: 'minimax-fast:openrouter[deepinfra]-api',
      route_type: 'on_demand',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: 'deepinfra',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      provider_model_id: 'minimax/minimax-m2.5',
      endpoint_id: 'minimax-fast:openrouter[deepinfra]-api',
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route, deepinfraRoute],
    });
    vi.mocked(listOpenRouterProviderOptions).mockResolvedValue({
      provider_model_id: 'minimax/minimax-m2.5',
      providers: [
        { provider: 'minimax/fp8', label: 'MiniMax Fp8' },
        { provider: 'minimax/highspeed', label: 'MiniMax Highspeed' },
      ],
    });
    vi.mocked(listProviderKeys).mockImplementation(async (provider?: string) =>
      providerKeysResponse(provider),
    );
    vi.mocked(createProviderRouteCandidate).mockResolvedValue({
      ...route,
      route_id: 'minimax-fast:openrouter[minimax/highspeed]-api',
      route_type: 'on_demand',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: 'minimax/highspeed',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      api_key: {
        id: 'key-1',
        provider: 'openrouter',
        label: 'staging',
        key_prefix: 'sk-or...1234',
        source: 'db',
      },
      provider_model_id: 'minimax/minimax-m2.5',
      endpoint_id: 'minimax-fast:openrouter[minimax/highspeed]-api',
      source: 'runtime',
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));

    const openRouterSelect = screen.getByLabelText('OpenRouter routing');
    await waitFor(() => {
      expect(
        within(openRouterSelect).getByRole('option', { name: 'Provider: MiniMax Highspeed' }),
      ).toBeInTheDocument();
    });
    fireEvent.change(openRouterSelect, { target: { value: 'provider:minimax/highspeed' } });

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() => {
      expect(createProviderRouteCandidate).toHaveBeenCalledWith('minimax-fast', {
        route_type: 'on_demand',
        upstream_provider: 'openrouter',
        openrouter_provider: 'minimax/highspeed',
        openrouter_sort: null,
        base_url: 'https://openrouter.ai/api/v1',
        api_key_id: 'key-1',
        provider_model_id: 'minimax/minimax-m2.5',
        quota_limit: null,
        concurrency_limit: null,
        weight: 1,
      });
    });
  });

  it('updates runtime OpenRouter concurrency limit inline', async () => {
    const runtimeRoute = {
      ...route,
      route_id: 'minimax-fast:openrouter[parasail]-api',
      route_type: 'concurrency',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      api_key: {
        id: 'key-1',
        provider: 'openrouter',
        label: 'staging',
        key_prefix: 'sk-or...1234',
        source: 'db' as const,
      },
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: null,
      concurrency_limit: 2,
      endpoint_id: 'minimax-fast:openrouter[parasail]-api',
      source: 'runtime' as const,
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route, runtimeRoute],
    });
    vi.mocked(updateProviderRouteCandidate).mockResolvedValue({
      ...runtimeRoute,
      concurrency_limit: 4,
    });

    render(<ProviderRoutesTab />);

    const limitInput = await screen.findByLabelText(
      'Concurrency limit for minimax-fast:openrouter[parasail]-api',
    );
    expect(limitInput).toHaveValue(2);
    expect(
      screen.queryByLabelText('Concurrency limit for minimax-fast:featherless-api'),
    ).not.toBeInTheDocument();

    fireEvent.change(limitInput, { target: { value: '4' } });
    fireEvent.click(
      screen.getByRole('button', {
        name: 'Save minimax-fast:openrouter[parasail]-api concurrency limit',
      }),
    );

    await waitFor(() => {
      expect(updateProviderRouteCandidate).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:openrouter[parasail]-api',
        { concurrency_limit: 4 },
      );
    });
    await waitFor(() => {
      expect(limitInput).toHaveValue(4);
    });
  });

  it('creates a runtime model with an initial provider route', async () => {
    const createdRoute = {
      ...route,
      model_id: 'deepseek-v4-flash',
      strategy: 'fixed',
      route_id: 'deepseek-v4-flash:openrouter-api',
      route_type: 'on_demand',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: null,
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      api_key: {
        id: 'key-1',
        provider: 'openrouter',
        label: 'staging',
        key_prefix: 'sk-or...1234',
        source: 'db' as const,
      },
      provider_model_id: 'deepseek/deepseek-v4-flash',
      endpoint_id: 'deepseek-v4-flash:openrouter-api',
      source: 'runtime' as const,
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockImplementation(async (provider?: string) =>
      providerKeysResponse(provider),
    );
    vi.mocked(createProviderRouteModel).mockResolvedValue(createdRoute);

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Create model' }));
    const routeTypeSelect = screen.getByLabelText('Route type');
    expect(within(routeTypeSelect).getByRole('option', { name: 'on_demand' })).toBeInTheDocument();
    expect(
      within(routeTypeSelect).queryByRole('option', { name: 'concurrency' }),
    ).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Model ID'), {
      target: { value: 'deepseek-v4-flash' },
    });
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'deepseek/deepseek-v4-flash' },
    });
    fillRuntimeModelPricing();

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(createProviderRouteModel).toHaveBeenCalledWith({
        model_id: 'deepseek-v4-flash',
        strategy: 'fixed',
        required_role: 'admin',
        route_type: 'on_demand',
        upstream_provider: 'openrouter',
        openrouter_provider: null,
        openrouter_sort: null,
        base_url: 'https://openrouter.ai/api/v1',
        api_key_id: 'key-1',
        provider_model_id: 'deepseek/deepseek-v4-flash',
        quota_limit: null,
        concurrency_limit: null,
        weight: 1,
        pricing: {
          prompt: '0.14',
          completion: '0.28',
        },
      });
    });
    expect(await screen.findByText('deepseek-v4-flash')).toBeInTheDocument();
    expect(await screen.findByText('Runtime added')).toBeInTheDocument();
  });

  it('creates a runtime model with an initial OpenRouter concurrency route', async () => {
    const createdRoute = {
      ...route,
      model_id: 'deepseek-v4-flash',
      strategy: 'routewise',
      route_id: 'deepseek-v4-flash:openrouter[parasail]-api',
      route_type: 'concurrency',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      api_key_id: 'key-1',
      api_key: {
        id: 'key-1',
        provider: 'openrouter',
        label: 'staging',
        key_prefix: 'sk-or...1234',
        source: 'db' as const,
      },
      provider_model_id: 'deepseek/deepseek-v4-flash',
      quota_limit: null,
      concurrency_limit: 2,
      endpoint_id: 'deepseek-v4-flash:openrouter[parasail]-api',
      source: 'runtime' as const,
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockImplementation(async (provider?: string) =>
      providerKeysResponse(provider),
    );
    vi.mocked(createProviderRouteModel).mockResolvedValue(createdRoute);

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Create model' }));
    fireEvent.change(screen.getByLabelText('Model ID'), {
      target: { value: 'deepseek-v4-flash' },
    });
    fireEvent.change(screen.getByLabelText('Initial routing policy'), {
      target: { value: 'routewise' },
    });
    fireEvent.change(screen.getByLabelText('Route type'), { target: { value: 'concurrency' } });
    fireEvent.change(screen.getByLabelText('Provider'), { target: { value: 'openrouter' } });
    const routingSelect = screen.getByLabelText('OpenRouter routing');
    expect(routingSelect).toHaveValue('provider:deepinfra');
    expect(within(routingSelect).queryByRole('option', { name: 'Auto' })).not.toBeInTheDocument();
    expect(
      within(routingSelect).queryByRole('option', { name: 'Sort by price' }),
    ).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'deepseek/deepseek-v4-flash' },
    });
    fireEvent.change(routingSelect, {
      target: { value: 'provider:parasail' },
    });
    fireEvent.change(screen.getByLabelText('Concurrency limit'), {
      target: { value: '2' },
    });
    fillRuntimeModelPricing();

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => {
      expect(createProviderRouteModel).toHaveBeenCalledWith({
        model_id: 'deepseek-v4-flash',
        strategy: 'routewise',
        required_role: 'admin',
        route_type: 'concurrency',
        upstream_provider: 'openrouter',
        openrouter_provider: 'parasail',
        openrouter_sort: null,
        base_url: 'https://openrouter.ai/api/v1',
        api_key_id: 'key-1',
        provider_model_id: 'deepseek/deepseek-v4-flash',
        quota_limit: null,
        concurrency_limit: 2,
        weight: 1,
        pricing: {
          prompt: '0.14',
          completion: '0.28',
        },
      });
    });
    expect(await screen.findByText('deepseek-v4-flash')).toBeInTheDocument();
    expect(await screen.findByText('Runtime added')).toBeInTheDocument();
  });

  it('verifies a runtime model without creating it', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Create model' }));
    fireEvent.change(screen.getByLabelText('Model ID'), {
      target: { value: 'deepseek-v4-flash' },
    });
    fireEvent.change(screen.getByLabelText('Initial routing policy'), {
      target: { value: 'routewise' },
    });
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'deepseek/deepseek-v4-flash' },
    });
    fillRuntimeModelPricing();
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));

    await waitFor(() => {
      expect(verifyProviderRouteModel).toHaveBeenCalledWith({
        model_id: 'deepseek-v4-flash',
        strategy: 'routewise',
        required_role: 'admin',
        route_type: 'on_demand',
        upstream_provider: 'openrouter',
        openrouter_provider: null,
        openrouter_sort: null,
        base_url: 'https://openrouter.ai/api/v1',
        api_key_id: null,
        provider_model_id: 'deepseek/deepseek-v4-flash',
        quota_limit: null,
        concurrency_limit: null,
        weight: 1,
        pricing: {
          prompt: '0.14',
          completion: '0.28',
        },
      });
    });
    expect(createProviderRouteModel).not.toHaveBeenCalled();
    expect(await screen.findByRole('button', { name: 'Verified' })).toHaveClass('bg-emerald-600');
  });

  it('threads create-model visibility into the payload', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Create model' }));
    fireEvent.change(screen.getByLabelText('Model ID'), {
      target: { value: 'deepseek-v4-flash' },
    });
    fireEvent.change(screen.getByLabelText('Visibility'), {
      target: { value: 'free' },
    });
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'deepseek/deepseek-v4-flash' },
    });
    fillRuntimeModelPricing();
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));

    await waitFor(() => {
      expect(verifyProviderRouteModel).toHaveBeenCalledWith(
        expect.objectContaining({
          model_id: 'deepseek-v4-flash',
          required_role: 'free',
        }),
      );
    });
  });

  it('does not filter create-model providers by the selected model routes', async () => {
    const chutesOption = {
      provider: 'chutes',
      label: 'Chutes',
      kind: 'chutes',
      key_provider: 'chutes',
      default_base_url: 'https://llm.chutes.ai/v1',
    };
    const quotaRoute = {
      ...route,
      route_id: 'minimax-fast:chutes-api',
      route_type: 'quota',
      provider: 'chutes',
      upstream_provider: 'chutes',
      key_provider: 'chutes',
      base_url: 'https://llm.chutes.ai/v1',
      provider_model_id: 'MiniMaxAI/MiniMax-M2.5-TEE',
      quota_limit: 5000,
      endpoint_id: 'minimax-fast:chutes-api',
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: [chutesOption, ...providerOptions],
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route, quotaRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Create model' }));
    fireEvent.change(screen.getByLabelText('Initial routing policy'), {
      target: { value: 'routewise' },
    });
    fireEvent.change(screen.getByLabelText('Route type'), { target: { value: 'quota' } });

    const providerSelect = screen.getByLabelText('Provider');
    expect(providerSelect).toHaveValue('chutes');
    expect(within(providerSelect).getByRole('option', { name: 'Chutes' })).toBeInTheDocument();
    expect(screen.getByLabelText('Base URL')).toHaveValue('https://llm.chutes.ai/v1');
  });

  it('verifies a runtime provider route without adding it', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });
    fireEvent.change(screen.getByLabelText('OpenRouter routing'), {
      target: { value: 'sort:throughput' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));

    await waitFor(() => {
      expect(verifyProviderRouteCandidate).toHaveBeenCalledWith('minimax-fast', {
        route_type: 'on_demand',
        upstream_provider: 'openrouter',
        openrouter_provider: null,
        openrouter_sort: 'throughput',
        base_url: 'https://openrouter.ai/api/v1',
        api_key_id: null,
        provider_model_id: 'minimax/minimax-m2.5',
        quota_limit: null,
        concurrency_limit: null,
        weight: 1,
      });
    });
    expect(createProviderRouteCandidate).not.toHaveBeenCalled();
    expect(await screen.findByRole('button', { name: 'Verified' })).toHaveClass('bg-emerald-600');
  });

  it('adds a runtime OpenRouter route with a custom provider slug', async () => {
    const autoRoute = {
      ...route,
      route_id: 'minimax-fast:openrouter-api',
      route_type: 'on_demand',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: null,
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      provider_model_id: 'minimax/minimax-m2.5',
      endpoint_id: 'minimax-fast:openrouter-api',
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: [{ provider: 'parasail', label: 'Parasail' }],
      routes: [route, autoRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({
      provider: 'openrouter',
      keys: [
        {
          id: 'key-1',
          provider: 'openrouter',
          key_prefix: 'sk-or...1234',
          label: 'staging',
          source: 'db',
          status: 'active',
          created_at: null,
        },
      ],
    });
    vi.mocked(createProviderRouteCandidate).mockResolvedValue({
      ...autoRoute,
      route_id: 'minimax-fast:openrouter[novita]-api',
      openrouter_provider: 'novita',
      api_key_id: 'key-1',
      api_key: {
        id: 'key-1',
        provider: 'openrouter',
        label: 'staging',
        key_prefix: 'sk-or...1234',
        source: 'db',
      },
      endpoint_id: 'minimax-fast:openrouter[novita]-api',
      source: 'runtime',
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));
    fireEvent.change(screen.getByLabelText('OpenRouter routing'), {
      target: { value: 'provider:__custom__' },
    });
    fireEvent.change(screen.getByLabelText('Custom OpenRouter provider'), {
      target: { value: 'novita' },
    });

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() => {
      expect(createProviderRouteCandidate).toHaveBeenCalledWith('minimax-fast', {
        route_type: 'on_demand',
        upstream_provider: 'openrouter',
        openrouter_provider: 'novita',
        openrouter_sort: null,
        base_url: 'https://openrouter.ai/api/v1',
        api_key_id: 'key-1',
        provider_model_id: 'minimax/minimax-m2.5',
        quota_limit: null,
        concurrency_limit: null,
        weight: 1,
      });
    });
  });

  it('adds an auto OpenRouter route with a sort policy', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });
    vi.mocked(createProviderRouteCandidate).mockResolvedValue({
      ...route,
      route_id: 'minimax-fast:openrouter-api',
      route_type: 'on_demand',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: null,
      openrouter_sort: 'throughput',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      provider_model_id: 'minimax/minimax-m2.5',
      endpoint_id: 'minimax-fast:openrouter-api',
      source: 'runtime',
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });
    fireEvent.change(screen.getByLabelText('OpenRouter routing'), {
      target: { value: 'sort:throughput' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() => {
      expect(createProviderRouteCandidate).toHaveBeenCalledWith('minimax-fast', {
        route_type: 'on_demand',
        upstream_provider: 'openrouter',
        openrouter_provider: null,
        openrouter_sort: 'throughput',
        base_url: 'https://openrouter.ai/api/v1',
        api_key_id: null,
        provider_model_id: 'minimax/minimax-m2.5',
        quota_limit: null,
        concurrency_limit: null,
        weight: 1,
      });
    });
  });

  it('filters add-provider choices by route type', async () => {
    const deepinfraRoute = {
      ...route,
      route_id: 'minimax-fast:openrouter[deepinfra]-api',
      route_type: 'on_demand',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: 'deepinfra',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      provider_model_id: 'minimax/minimax-m2.5',
      endpoint_id: 'minimax-fast:openrouter[deepinfra]-api',
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: [
        {
          provider: 'chutes',
          label: 'Chutes',
          kind: 'chutes',
          key_provider: 'chutes',
          default_base_url: 'https://llm.chutes.ai/v1',
        },
        {
          provider: 'deepinfra',
          label: 'DeepInfra via OpenRouter',
          kind: 'openrouter[deepinfra]',
          key_provider: 'openrouter',
          default_base_url: 'https://openrouter.ai/api/v1',
        },
        ...providerOptions,
      ],
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route, deepinfraRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));

    await waitFor(() => {
      expect(listOpenRouterProviderOptions).toHaveBeenCalledWith('minimax/minimax-m2.5');
    });

    const providerSelect = screen.getByLabelText('Provider');
    expect(providerSelect).toHaveValue('openrouter');
    expect(
      within(providerSelect).queryByRole('option', { name: 'Chutes' }),
    ).not.toBeInTheDocument();
    expect(
      within(providerSelect).queryByRole('option', { name: 'Featherless' }),
    ).not.toBeInTheDocument();
    expect(
      within(providerSelect).queryByRole('option', { name: 'DeepInfra' }),
    ).not.toBeInTheDocument();
    expect(within(providerSelect).getByRole('option', { name: 'OpenRouter' })).toBeInTheDocument();

    const openRouterSelect = screen.getByLabelText('OpenRouter routing');
    await waitFor(() => {
      expect(
        within(openRouterSelect).queryByRole('option', { name: 'Provider: DeepInfra' }),
      ).not.toBeInTheDocument();
    });
    expect(
      within(openRouterSelect).getByRole('option', { name: 'Provider: Inceptron' }),
    ).toBeInTheDocument();
    expect(
      within(openRouterSelect).getByRole('option', { name: 'Provider: Chutes' }),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Route type'), { target: { value: 'quota' } });

    expect(providerSelect).toHaveValue('chutes');
    expect(within(providerSelect).getByRole('option', { name: 'Chutes' })).toBeInTheDocument();
    expect(screen.getByLabelText('Base URL')).toHaveValue('https://llm.chutes.ai/v1');
  });

  it('explains when no quota provider can be added', async () => {
    const quotaRoute = {
      ...route,
      route_id: 'minimax-fast:chutes-api',
      route_type: 'quota',
      provider: 'chutes',
      upstream_provider: 'chutes',
      key_provider: 'chutes',
      base_url: 'https://llm.chutes.ai/v1',
      provider_model_id: 'MiniMaxAI/MiniMax-M2.5-TEE',
      quota_limit: 5000,
      endpoint_id: 'minimax-fast:chutes-api',
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: [
        {
          provider: 'chutes',
          label: 'Chutes',
          kind: 'chutes',
          key_provider: 'chutes',
          default_base_url: 'https://llm.chutes.ai/v1',
        },
        ...providerOptions,
      ],
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route, quotaRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));
    fireEvent.change(screen.getByLabelText('Route type'), { target: { value: 'quota' } });

    const providerSelect = screen.getByLabelText('Provider');
    expect(providerSelect).toHaveValue('');
    expect(
      within(providerSelect).getByRole('option', { name: 'No quota providers available' }),
    ).toBeInTheDocument();
    expect(
      screen.getByText('This model already has every configured quota provider.'),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('API key')).toHaveValue('');
    expect(
      within(screen.getByLabelText('API key')).getByRole('option', {
        name: 'No provider selected',
      }),
    ).toBeInTheDocument();
  });

  it('deletes a runtime provider route', async () => {
    const runtimeRoute = {
      ...route,
      route_id: 'minimax-fast:openrouter[parasail]-api',
      route_type: 'on_demand',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      provider_model_id: 'minimax/minimax-m2.5',
      endpoint_id: 'minimax-fast:openrouter[parasail]-api',
      source: 'runtime' as const,
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route, runtimeRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });
    vi.mocked(deleteProviderRouteCandidate).mockResolvedValue({
      model_id: 'minimax-fast',
      strategy: 'routewise',
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));

    await waitFor(() => {
      expect(deleteProviderRouteCandidate).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:openrouter[parasail]-api',
      );
    });
    await waitFor(() => {
      expect(screen.queryByText('Runtime added')).not.toBeInTheDocument();
    });
  });

  it('keeps the current override provider selectable when it is not a default option', async () => {
    const zaiRoute = {
      ...route,
      upstream_provider: 'zai',
      key_provider: 'zai',
      base_url: 'https://api.z.ai/api/paas/v4',
      provider_model_id: 'glm-4.5',
      source: 'override' as const,
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [zaiRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'zai', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));

    expect(screen.getByLabelText('Override provider')).toHaveValue('zai');
    expect(screen.getByRole('option', { name: 'zai' })).toBeInTheDocument();
    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('zai');
    });
  });

  it('updates the selected model routing policy', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'featherless', keys: [] });
    vi.mocked(updateProviderRouteStrategy).mockResolvedValue({
      model_id: 'minimax-fast',
      strategy: 'fixed',
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [{ ...route, strategy: 'fixed' }],
    });

    render(<ProviderRoutesTab />);

    const select = await screen.findByLabelText('Routing policy');
    expect(select).toHaveValue('routewise');

    fireEvent.change(select, { target: { value: 'fixed' } });

    await waitFor(() => {
      expect(updateProviderRouteStrategy).toHaveBeenCalledWith('minimax-fast', 'fixed');
    });
    expect(screen.getByLabelText('Routing policy')).toHaveValue('fixed');
  });
});
