// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ProviderRoutesTab } from './ProviderRoutesTab';

vi.mock('@/lib/api/admin', () => ({
  clearOffloadRoute: vi.fn(),
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
  listOffloadRoutes: vi.fn(),
  listOpenRouterProviderOptions: vi.fn(),
  listProviderKeys: vi.fn(),
  listProviderRoutes: vi.fn(),
  listRecentRequests: vi.fn(() =>
    Promise.resolve({ requests: [], total: 0, limit: 20, offset: 0 }),
  ),
  listRouteWeights: vi.fn(),
  listRoutewiseProbeSamples: vi.fn(),
  listRoutewiseSettings: vi.fn(() => Promise.resolve({ model_id: 'minimax-fast', settings: [] })),
  resetRoutewiseSetting: vi.fn(),
  runRoutewiseProbe: vi.fn(),
  setOffloadRoute: vi.fn(),
  setRouteWeight: vi.fn(),
  updateRoutewiseSetting: vi.fn(),
  updateProviderRoute: vi.fn(),
  updateProviderRouteCandidate: vi.fn(),
  updateProviderRouteCapabilities: vi.fn(),
  resetProviderRouteCapabilities: vi.fn(),
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
  listOffloadRoutes,
  listOpenRouterProviderOptions,
  listProviderKeys,
  listProviderRoutes,
  listRouteWeights,
  listRoutewiseProbeSamples,
  listRoutewiseSettings,
  runRoutewiseProbe,
  setOffloadRoute,
  setRouteWeight,
  updateProviderRoute,
  updateProviderRouteCandidate,
  updateProviderRouteCapabilities,
  resetProviderRouteCapabilities,
  updateProviderRouteStrategy,
  updateRoutewiseSetting,
  verifyProviderRoute,
  verifyProviderRouteModel,
  verifyProviderRouteCandidate,
} from '@/lib/api/admin';
import type { ListProviderApiKeysResponse, RoutewiseSettingItem } from '@/lib/api/admin';
import { chooseValue, openOptions } from '@/test-utils/select';
import toast from 'react-hot-toast';
import { APIError } from '@/lib/utils/errors';

// A deployment whose Featherless plan is concurrency-based and whose OpenRouter
// account is used on demand or with a concurrency cap (PROVIDER_ROUTE_TYPES).
const providerOptions = [
  {
    provider: 'featherless',
    label: 'Featherless',
    kind: 'featherless',
    key_provider: 'featherless',
    default_base_url: 'https://api.featherless.ai/v1',
    route_types: ['concurrency' as const],
  },
  {
    provider: 'openrouter',
    label: 'OpenRouter',
    kind: 'openrouter',
    key_provider: 'openrouter',
    default_base_url: 'https://openrouter.ai/api/v1',
    route_types: ['on_demand' as const, 'concurrency' as const],
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
      min_role: 'free',
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

const RUNTIME_MODEL_CAPABILITIES = {
  context_length: 131072,
  max_output_length: 65536,
  input_modalities: ['text', 'image'],
  supports_tools: true,
};

function fillRuntimeModelCapabilities() {
  fireEvent.change(screen.getByLabelText('Context length (tokens)'), {
    target: { value: '131072' },
  });
  fireEvent.change(screen.getByLabelText('Max output (tokens)'), {
    target: { value: '65536' },
  });
  chooseValue(screen.getByLabelText('Image input'), 'yes');
  chooseValue(screen.getByLabelText('Tool calling'), 'yes');
}

// What the fixture route serves with; an added route starts from it.
const ROUTE_CAPABILITIES = {
  context_length: 196608,
  max_output_length: 8192,
  input_modalities: ['text'],
  supports_tools: true,
};

const route = {
  model_id: 'minimax-fast',
  strategy: 'routewise',
  route_id: 'minimax-fast:featherless-api',
  route_type: 'concurrency',
  provider: 'featherless',
  upstream_provider: 'featherless',
  key_provider: 'featherless',
  base_url: 'https://api.featherless.ai/v1',
  deployment_type: 'auto' as const,
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
  capabilities: ROUTE_CAPABILITIES,
  capabilities_baseline: ROUTE_CAPABILITIES,
  capabilities_overridden: false,
};

const quotaRoute = {
  ...route,
  route_id: 'minimax-fast:chutes-api',
  route_type: 'quota',
  provider: 'chutes',
  upstream_provider: 'chutes',
  key_provider: 'chutes',
  base_url: 'https://llm.chutes.ai/v1',
  deployment_type: 'auto' as const,
  provider_model_id: 'MiniMaxAI/MiniMax-M2.5-TEE',
  quota_limit: 5000,
  concurrency_limit: null,
  quota_current_limit: 5000,
  quota_used: 123,
  quota_remaining: 4877,
  quota_reset_at: '2026-07-04T16:00:00Z',
  endpoint_id: 'minimax-fast:chutes-api',
};

/**
 * Press the confirm button of the dialog a route change opens. It is the
 * topmost dialog: editing a route opens it over the edit dialog.
 */
function confirmAction(label: string) {
  const dialogs = screen.getAllByRole('dialog');
  fireEvent.click(within(dialogs[dialogs.length - 1]).getByRole('button', { name: label }));
}

/** The topmost dialog: the confirmation, over the editor it confirms. */
function topDialog() {
  const dialogs = screen.getAllByRole('dialog');
  return dialogs[dialogs.length - 1];
}

/** Save the open route editor's drafts; this only opens the review. */
function saveRouteEdit() {
  fireEvent.click(screen.getByRole('button', { name: /^Save changes to .+ \/ .+$/ }));
}

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
    vi.mocked(listOffloadRoutes).mockResolvedValue({
      queue_enabled: true,
      max_wait_seconds: 30,
      offload_routes: [],
    });
  });

  it('marks the offload route of a fixed model and saves a new one', async () => {
    const fixedRoute = { ...route, strategy: 'fixed', route_type: 'on_demand' };
    const reservedRoute = {
      ...fixedRoute,
      route_id: 'minimax-fast:openrouter-api',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      deployment_type: 'auto' as const,
      endpoint_id: 'minimax-fast:openrouter-api',
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [fixedRoute, reservedRoute],
    });
    const offload = {
      model_id: 'minimax-fast',
      route_id: reservedRoute.route_id,
      wait_seconds: 5,
      endpoint_id: reservedRoute.endpoint_id,
      active: true,
      inactive_reason: null,
      updated_at: null,
      updated_by: 'admin@example.com',
    };
    vi.mocked(listOffloadRoutes).mockResolvedValue({
      queue_enabled: true,
      max_wait_seconds: 30,
      offload_routes: [offload],
    });
    vi.mocked(setOffloadRoute).mockResolvedValue({
      model_id: 'minimax-fast',
      offload: { ...offload, route_id: fixedRoute.route_id, endpoint_id: fixedRoute.endpoint_id },
    });

    render(<ProviderRoutesTab />);

    expect(await screen.findByTestId('offload-route-panel')).toBeInTheDocument();
    expect(await screen.findByTestId(`offload-badge-${reservedRoute.route_id}`)).toHaveTextContent(
      'Offload',
    );
    expect(screen.queryByTestId(`offload-badge-${fixedRoute.route_id}`)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Edit Queue offload' }));
    chooseValue(screen.getByLabelText('Offload route'), fixedRoute.route_id);
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Queue offload' }));
    expect(setOffloadRoute).not.toHaveBeenCalled();
    confirmAction('Confirm save');

    await waitFor(() => {
      expect(setOffloadRoute).toHaveBeenCalledWith('minimax-fast', fixedRoute.route_id, 5, {
        engineQueueLimit: null,
        maxInputTokens: null,
      });
    });
    // The badge follows the saved route without a reload.
    expect(await screen.findByTestId(`offload-badge-${fixedRoute.route_id}`)).toBeInTheDocument();
    expect(screen.queryByTestId(`offload-badge-${reservedRoute.route_id}`)).not.toBeInTheDocument();
  });

  it('shows no offload panel for a routewise model without one', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'featherless', keys: [] });

    render(<ProviderRoutesTab />);

    expect(await screen.findByText('minimax-fast')).toBeInTheDocument();
    expect(screen.queryByTestId('offload-route-panel')).not.toBeInTheDocument();
  });

  it('keeps the routes table when the offload routes fail to load', async () => {
    const fixedRoute = { ...route, strategy: 'fixed' };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [fixedRoute],
    });
    vi.mocked(listOffloadRoutes).mockRejectedValue(new Error('offload store down'));

    render(<ProviderRoutesTab />);

    expect(
      await screen.findByText('Failed to load offload routes: offload store down'),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText('Runtime weight for minimax-fast:featherless-api'),
    ).toBeInTheDocument();
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

  it('shows current daily quota remaining in the RouteWise summary and routes table', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [quotaRoute, route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'chutes', keys: [] });

    render(<ProviderRoutesTab />);

    expect(await screen.findByTestId('routewise-quota-summary')).toHaveTextContent(
      '4,877 left / 5,000',
    );
    expect(screen.getByText('4,877 left')).toBeInTheDocument();
    expect(screen.getByText('of 5,000 today')).toBeInTheDocument();
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

    expect(screen.getByLabelText('Routing policy')).toHaveTextContent('routewise');
    expect(screen.queryByLabelText('Fixed weight')).not.toBeInTheDocument();
  });

  it('holds the model selection while an offload edit or its review is open', async () => {
    const fixedRoute = { ...route, strategy: 'fixed', route_type: 'on_demand' };
    // Offload needs a second route to point at, on each model.
    const secondRoute = (base: typeof fixedRoute) => ({
      ...base,
      route_id: `${base.model_id}:openrouter-api`,
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      endpoint_id: `${base.model_id}:openrouter-api`,
    });
    const otherRoute = {
      ...fixedRoute,
      model_id: 'other-model',
      route_id: 'other-model:chutes-api',
      endpoint_id: 'other-model:chutes-api',
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [fixedRoute, secondRoute(fixedRoute), otherRoute, secondRoute(otherRoute)],
    });

    render(<ProviderRoutesTab />);
    await screen.findByTestId('offload-route-panel');
    const selector = screen.getByLabelText('Selected model');
    const createModel = screen.getByRole('button', { name: 'Create model' });
    expect(selector).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: 'Edit Queue offload' }));
    expect(selector).toBeDisabled();
    expect(createModel).toBeDisabled();
    chooseValue(screen.getByLabelText('Offload route'), fixedRoute.route_id);
    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '9' } });

    // Still held while the review is open, and after going back to it.
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Queue offload' }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(selector).toBeDisabled();
    confirmAction('Back to editing');
    expect(selector).toBeDisabled();
    expect(screen.getByLabelText('Offload wait seconds')).toHaveValue(9);

    // Cancel releases it; the next model starts from its own stored values.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel editing Queue offload' }));
    expect(selector).toBeEnabled();
    expect(createModel).toBeEnabled();
    chooseValue(selector, 'other-model');
    expect(selector).toHaveAttribute('data-value', 'other-model');
    fireEvent.click(await screen.findByRole('button', { name: 'Edit Queue offload' }));
    expect(screen.getByLabelText('Offload wait seconds')).not.toHaveValue(9);
    expect(setOffloadRoute).not.toHaveBeenCalled();
  });

  it('releases the model selection once an offload save is confirmed', async () => {
    const fixedRoute = { ...route, strategy: 'fixed', route_type: 'on_demand' };
    const reservedRoute = {
      ...fixedRoute,
      route_id: 'minimax-fast:openrouter-api',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      endpoint_id: 'minimax-fast:openrouter-api',
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [fixedRoute, reservedRoute],
    });
    vi.mocked(setOffloadRoute).mockResolvedValue({
      model_id: 'minimax-fast',
      offload: {
        model_id: 'minimax-fast',
        route_id: fixedRoute.route_id,
        wait_seconds: 5,
        endpoint_id: fixedRoute.endpoint_id,
        active: true,
        inactive_reason: null,
        updated_at: null,
        updated_by: 'admin@example.com',
      },
    });

    render(<ProviderRoutesTab />);
    await screen.findByTestId('offload-route-panel');
    const selector = screen.getByLabelText('Selected model');
    fireEvent.click(screen.getByRole('button', { name: 'Edit Queue offload' }));
    chooseValue(screen.getByLabelText('Offload route'), fixedRoute.route_id);
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Queue offload' }));
    expect(selector).toBeDisabled();
    confirmAction('Confirm save');
    await waitFor(() => expect(setOffloadRoute).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(selector).toBeEnabled());
  });

  it('holds the model selection while RouteWise parameters are being edited', async () => {
    const otherRoute = {
      ...route,
      model_id: 'other-routewise',
      route_id: 'other-routewise:chutes-api',
      endpoint_id: 'other-routewise:chutes-api',
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route, otherRoute],
    });
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'minimax-fast',
      settings: [
        {
          key: 'routewise_budget_alpha',
          value: 0.75,
          value_type: 'float',
          default_value: 0.75,
          source: 'global_default',
          overridden: false,
          description: 'RouteWise LP cost budget interpolation.',
          min: 0,
          max: 1,
        },
      ],
    });

    render(<ProviderRoutesTab showRoutewiseSettings />);
    const edit = await screen.findByRole('button', { name: 'Edit RouteWise parameters' });
    const selector = screen.getByLabelText('Selected model');
    expect(selector).toBeEnabled();

    fireEvent.click(edit);
    expect(selector).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Create model' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel editing RouteWise parameters' }));
    expect(selector).toBeEnabled();
    chooseValue(selector, 'other-routewise');
    await waitFor(() => expect(listRoutewiseSettings).toHaveBeenLastCalledWith('other-routewise'));
    expect(updateRoutewiseSetting).not.toHaveBeenCalled();
  });

  describe('routing policy and the panels it shows', () => {
    const LOCKED = 'Save or cancel the open edit first';
    const budgetSetting: RoutewiseSettingItem = {
      key: 'routewise_budget_alpha',
      value: 0.75,
      value_type: 'float',
      default_value: 0.75,
      source: 'global_default',
      overridden: false,
      description: 'RouteWise LP cost budget interpolation.',
      min: 0,
      max: 1,
    };

    function renderRoutewiseWithParameters() {
      vi.mocked(listProviderRoutes).mockResolvedValue({
        provider_options: providerOptions,
        openrouter_provider_options: openRouterProviderOptions,
        routes: [route],
      });
      vi.mocked(listRoutewiseSettings).mockResolvedValue({
        model_id: 'minimax-fast',
        settings: [budgetSetting],
      });
      render(<ProviderRoutesTab showRoutewiseSettings />);
      return screen.findByRole('button', { name: 'Edit RouteWise parameters' });
    }

    it('holds the routing policy while RouteWise parameters are being edited', async () => {
      const editParameters = await renderRoutewiseWithParameters();
      const editPolicy = screen.getByRole('button', { name: 'Edit minimax-fast routing policy' });
      expect(editPolicy).toBeEnabled();

      // Switching to fixed would unmount the RouteWise panel and drop its drafts.
      fireEvent.click(editParameters);
      expect(editPolicy).toBeDisabled();
      expect(editPolicy).toHaveAttribute('title', LOCKED);

      fireEvent.click(screen.getByRole('button', { name: 'Cancel editing RouteWise parameters' }));
      expect(editPolicy).toBeEnabled();
      expect(editPolicy).not.toHaveAttribute('title');
    });

    it('holds the routing policy while a Queue offload draft is open', async () => {
      const fixedRoute = { ...route, strategy: 'fixed', route_type: 'on_demand' };
      vi.mocked(listProviderRoutes).mockResolvedValue({
        provider_options: providerOptions,
        openrouter_provider_options: openRouterProviderOptions,
        routes: [
          fixedRoute,
          {
            ...fixedRoute,
            route_id: 'minimax-fast:openrouter-api',
            provider: 'openrouter',
            upstream_provider: 'openrouter',
            key_provider: 'openrouter',
            base_url: 'https://openrouter.ai/api/v1',
            endpoint_id: 'minimax-fast:openrouter-api',
          },
        ],
      });

      render(<ProviderRoutesTab />);
      await screen.findByTestId('offload-route-panel');
      const editPolicy = screen.getByRole('button', { name: 'Edit minimax-fast routing policy' });
      expect(editPolicy).toBeEnabled();

      // Switching to routewise would unmount the offload panel and drop a new draft.
      fireEvent.click(screen.getByRole('button', { name: 'Edit Queue offload' }));
      expect(editPolicy).toBeDisabled();
      expect(editPolicy).toHaveAttribute('title', LOCKED);

      fireEvent.click(screen.getByRole('button', { name: 'Cancel editing Queue offload' }));
      expect(editPolicy).toBeEnabled();
    });

    it('holds a policy switch while a parameter edit opened after it is open', async () => {
      const editParameters = await renderRoutewiseWithParameters();
      fireEvent.click(screen.getByRole('button', { name: 'Edit minimax-fast routing policy' }));
      chooseValue(screen.getByLabelText('Routing policy'), 'fixed');
      const savePolicy = screen.getByRole('button', {
        name: 'Save changes to minimax-fast routing policy',
      });
      expect(savePolicy).toBeEnabled();

      fireEvent.click(editParameters);
      expect(savePolicy).toBeDisabled();
      expect(screen.getByText(LOCKED)).toBeVisible();

      fireEvent.click(screen.getByRole('button', { name: 'Cancel editing RouteWise parameters' }));
      expect(savePolicy).toBeEnabled();
      expect(updateProviderRouteStrategy).not.toHaveBeenCalled();
    });
  });

  it('renders settings and runs RouteWise probes when requested', async () => {
    const tencentRoute = {
      ...route,
      route_id: 'minimax-fast:lkeap-api',
      route_type: 'on_demand',
      provider: 'lkeap',
      upstream_provider: 'lkeap',
      key_provider: 'lkeap',
      base_url: 'https://api.lkeap.cloud.tencent.com/v1',
      deployment_type: 'auto' as const,
      provider_model_id: 'MiniMax-M2.5',
      endpoint_id: 'minimax-fast:lkeap-api',
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: [
        ...providerOptions,
        {
          provider: 'lkeap',
          label: 'Tencent Token Plan',
          kind: 'lkeap',
          key_provider: 'lkeap',
          default_base_url: 'https://api.lkeap.cloud.tencent.com/v1',
        },
      ],
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route, tencentRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'featherless', keys: [] });
    vi.mocked(listRoutewiseSettings).mockResolvedValue({
      model_id: 'minimax-fast',
      settings: [
        {
          key: 'routewise_budget_alpha',
          value: 0.75,
          value_type: 'float',
          default_value: 0.75,
          source: 'global_default',
          overridden: false,
          description: 'RouteWise LP cost budget interpolation.',
          min: 0,
          max: 1,
        },
        {
          key: 'routewise_latency_slo_sec',
          value: 3,
          value_type: 'float',
          default_value: 3,
          source: 'global_default',
          overridden: false,
          description: 'Latency SLO in seconds for Routewise LP decisions.',
          min: 0.1,
          max: null,
        },
        {
          key: 'routewise_probe_enabled',
          value: false,
          value_type: 'bool',
          default_value: false,
          source: 'global_default',
          overridden: false,
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
        {
          model_id: 'minimax-fast',
          endpoint_id: 'minimax-fast:lkeap-api',
          ok: true,
          ttft_ms: 456.7,
          error: null,
          checked_at: '2026-06-22T00:00:02Z',
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
    vi.mocked(updateRoutewiseSetting).mockImplementation(async (_modelId, key, value) => ({
      key,
      value,
      value_type: key === 'routewise_probe_enabled' ? 'bool' : 'float',
      default_value: key === 'routewise_probe_enabled' ? false : 0.75,
      source: 'runtime_override',
      overridden: true,
      description:
        key === 'routewise_probe_enabled'
          ? 'Enable RouteWise background active latency probes.'
          : 'RouteWise LP cost budget interpolation.',
      min: key === 'routewise_probe_enabled' ? null : 0,
      max: key === 'routewise_probe_enabled' ? null : 1,
    }));

    render(<ProviderRoutesTab showRoutewiseSettings />);

    expect(
      await screen.findByRole('heading', { name: 'RouteWise parameters' }),
    ).toBeInTheDocument();
    expect(await screen.findByLabelText('Cost budget alpha value')).toHaveTextContent('0.75');

    fireEvent.click(screen.getByRole('button', { name: 'Edit RouteWise parameters' }));
    const alphaInput = screen.getByLabelText('Cost budget alpha value');
    expect(alphaInput).toHaveValue(0.75);
    fireEvent.change(alphaInput, { target: { value: '0.4' } });

    const probeToggle = screen.getByLabelText('Background probes enabled');
    expect(probeToggle).not.toBeChecked();
    fireEvent.click(probeToggle);
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to RouteWise parameters' }));
    expect(updateRoutewiseSetting).not.toHaveBeenCalled();
    confirmAction('Confirm save');

    await waitFor(() => {
      expect(updateRoutewiseSetting).toHaveBeenCalledWith(
        'minimax-fast',
        'routewise_budget_alpha',
        0.4,
      );
    });
    await waitFor(() => {
      expect(updateRoutewiseSetting).toHaveBeenCalledWith(
        'minimax-fast',
        'routewise_probe_enabled',
        true,
      );
    });
    await waitFor(() => {
      expect(screen.getByLabelText('Cost budget alpha value')).toHaveTextContent('0.4');
    });

    expect((await screen.findAllByText('minimax-fast:featherless-api')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('on_demand · Tencent Token Plan').length).toBeGreaterThan(0);
    expect(screen.getAllByText('minimax-fast:lkeap-api').length).toBeGreaterThan(0);
    expect(screen.getByText('123 ms')).toBeInTheDocument();
    expect(screen.queryByText('RuntimeError')).not.toBeInTheDocument();
    expect(screen.getByText(/temporarily rate-limited upstream/)).toBeInTheDocument();
    expect(screen.queryByText(/diagnostic-tail-that-should-be-truncated/)).not.toBeInTheDocument();
    expect(listRoutewiseProbeSamples).toHaveBeenCalledWith(expect.objectContaining({ limit: 100 }));

    fireEvent.click(screen.getByRole('button', { name: 'Run probe' }));
    expect(runRoutewiseProbe).not.toHaveBeenCalled();
    confirmAction('Run probe');

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

    expect(screen.getByLabelText('Routing policy')).toHaveTextContent('fixed');
    const routeTypeSelect = screen.getByLabelText('Route type');
    expect(
      within(openOptions(routeTypeSelect)).getByRole('option', { name: 'on_demand' }),
    ).toBeInTheDocument();
    expect(
      within(openOptions(routeTypeSelect)).queryByRole('option', { name: 'concurrency' }),
    ).not.toBeInTheDocument();
    expect(
      within(openOptions(routeTypeSelect)).queryByRole('option', { name: 'quota' }),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText('Fixed weight')).toHaveValue(1);
  });

  it('edits fixed route weights after Edit and confirms each change', async () => {
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

    const weight = await screen.findByLabelText('Runtime weight for minimax-fast:featherless-api');
    expect(weight.tagName).toBe('OUTPUT');
    expect(weight).toHaveTextContent('1');

    fireEvent.click(screen.getByRole('button', { name: 'Edit minimax-fast weights' }));
    const input = screen.getByLabelText('Runtime weight for minimax-fast:featherless-api');
    expect(input).toHaveValue(1);
    expect(
      screen.getByRole('button', { name: 'Save changes to minimax-fast weights' }),
    ).toBeDisabled();
    fireEvent.change(input, { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to minimax-fast weights' }));

    const review = screen.getByRole('dialog', { name: 'Save 1 change to minimax-fast weights?' });
    expect(review).toHaveTextContent('minimax-fast:featherless-api1 → 3');
    expect(setRouteWeight).not.toHaveBeenCalled();
    confirmAction('Confirm save');

    await waitFor(() => {
      expect(setRouteWeight).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:featherless-api',
        3,
      );
    });
    expect(setRouteWeight).toHaveBeenCalledTimes(1);
    expect(
      await screen.findByLabelText('Runtime weight for minimax-fast:featherless-api'),
    ).toHaveTextContent('3');

    fireEvent.click(
      await screen.findByRole('button', {
        name: 'Clear minimax-fast:featherless-api weight override',
      }),
    );
    const reset = screen.getByRole('dialog', {
      name: 'Reset minimax-fast:featherless-api weight?',
    });
    expect(reset).toHaveTextContent('Weight3 → 1 (configured)');
    expect(clearRouteWeight).not.toHaveBeenCalled();
    confirmAction('Reset');

    await waitFor(() => {
      expect(clearRouteWeight).toHaveBeenCalledWith('minimax-fast', 'minimax-fast:featherless-api');
    });
    expect(clearRouteWeight).toHaveBeenCalledTimes(1);
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
    confirmAction('Disable');

    await waitFor(() => {
      expect(setRouteWeight).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:featherless-api',
        0,
      );
    });
    expect(await screen.findByText('Disabled')).toBeInTheDocument();

    fireEvent.click(await screen.findByRole('button', { name: 'Enable' }));
    confirmAction('Enable');

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
    expect(within(dialog).getByLabelText('Override provider')).toHaveAttribute(
      'data-value',
      'featherless',
    );
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

  it('lists the changed fields and saves nothing until the save is confirmed', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockImplementation(async (provider?: string) =>
      providerKeysResponse(provider),
    );

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    saveRouteEdit();

    const confirm = screen.getByRole('dialog', {
      name: 'Save 1 change to minimax-fast / featherless?',
    });
    expect(within(confirm).getByText('Provider model ID')).toBeInTheDocument();
    expect(within(confirm).getByText(/→ minimax\/minimax-m2\.5$/)).toBeInTheDocument();
    expect(within(confirm).queryByText('Base URL')).not.toBeInTheDocument();
    // It stacks over the editor, and focus starts on the safe choice.
    expect(topDialog()).toBe(confirm);
    expect(within(confirm).getByRole('button', { name: 'Back to editing' })).toHaveFocus();

    fireEvent.click(within(confirm).getByRole('button', { name: 'Back to editing' }));
    expect(updateProviderRoute).not.toHaveBeenCalled();
    // The edit stays open with what was typed.
    expect(screen.getByLabelText('Provider model ID')).toHaveValue('minimax/minimax-m2.5');
  });

  it('asks before deleting a runtime route', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [{ ...route, source: 'runtime' as const }],
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    const confirm = screen.getByRole('dialog', { name: 'Delete minimax-fast / featherless?' });
    fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }));
    expect(deleteProviderRouteCandidate).not.toHaveBeenCalled();
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
      deployment_type: 'auto' as const,
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
    chooseValue(screen.getByLabelText('Override provider'), 'openrouter');
    chooseValue(screen.getByLabelText('OpenRouter routing'), 'provider:parasail');
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });
    fireEvent.change(screen.getByLabelText('Local concurrency limit'), {
      target: { value: '3' },
    });
    expect(screen.getByLabelText('OpenRouter routing')).toHaveAttribute(
      'data-value',
      'provider:parasail',
    );

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    chooseValue(screen.getByLabelText('API key'), 'key-1');
    expect(screen.getByLabelText('Base URL')).toHaveValue('https://openrouter.ai/api/v1');
    saveRouteEdit();
    expect(updateProviderRoute).not.toHaveBeenCalled();
    confirmAction('Confirm save');

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
    expect(updateProviderRoute).toHaveBeenCalledTimes(1);
    // The editor closes and the row shows the saved target.
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(screen.getByText('OpenRouter provider: Parasail')).toBeInTheDocument();
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
          min_role: 'free',
        },
      ],
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    chooseValue(screen.getByLabelText('Override provider'), 'openrouter');
    chooseValue(screen.getByLabelText('OpenRouter routing'), 'provider:parasail');
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });
    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });
    chooseValue(screen.getByLabelText('API key'), 'key-1');
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
      deployment_type: 'auto' as const,
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
          min_role: 'free',
        },
      ],
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByLabelText('Provider model ID')).toHaveValue('minimax/minimax-m2.5');

    chooseValue(screen.getByLabelText('Override provider'), 'tencent_token_plan');

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
      deployment_type: 'auto' as const,
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
      deployment_type: 'auto' as const,
      provider_model_id: 'minimax/minimax-m2.5',
      quota_limit: 8000,
      source: 'override',
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    chooseValue(screen.getByLabelText('Override provider'), 'openrouter');
    chooseValue(screen.getByLabelText('OpenRouter routing'), 'provider:parasail');
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });
    fireEvent.change(screen.getByLabelText('Local daily quota'), {
      target: { value: '8000' },
    });
    saveRouteEdit();
    expect(topDialog()).toHaveTextContent('Quota limit5000 → 8000');
    confirmAction('Confirm save');

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
      deployment_type: 'auto' as const,
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
    confirmAction('Reset');

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
      deployment_type: 'provider_api' as const,
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
      deployment_type: 'provider_api' as const,
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
    expect(screen.getByLabelText('Deployment type')).toHaveAttribute('data-value', 'auto');
    chooseValue(screen.getByLabelText('Deployment type'), 'provider_api');

    await waitFor(() => {
      expect(listOpenRouterProviderOptions).toHaveBeenCalledWith('minimax/minimax-m2.5');
    });
    const openRouterSelect = screen.getByLabelText('OpenRouter routing');
    await waitFor(() => {
      expect(
        within(openOptions(openRouterSelect)).getByRole('option', { name: 'Provider: Inceptron' }),
      ).toBeInTheDocument();
    });
    chooseValue(screen.getByLabelText('OpenRouter routing'), 'provider:inceptron');

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    chooseValue(screen.getByLabelText('API key'), 'key-1');
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await screen.findByRole('button', { name: 'Verified' });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    expect(createProviderRouteCandidate).not.toHaveBeenCalled();
    confirmAction('Add');

    await waitFor(() => {
      expect(createProviderRouteCandidate).toHaveBeenCalledWith('minimax-fast', {
        route_type: 'on_demand',
        upstream_provider: 'openrouter',
        openrouter_provider: 'inceptron',
        openrouter_sort: null,
        base_url: 'https://openrouter.ai/api/v1',
        deployment_type: 'provider_api' as const,
        api_key_id: 'key-1',
        provider_model_id: 'minimax/minimax-m2.5',
        quota_limit: null,
        concurrency_limit: null,
        weight: 1,
        capabilities: ROUTE_CAPABILITIES,
      });
    });
    expect(await screen.findByText('Runtime added')).toBeInTheDocument();
  });

  it('offers discovered OpenRouter pins for a quota route the policy allows', async () => {
    // This deployment's policy lets OpenRouter carry a quota route.
    const quotaCapableOptions = providerOptions.map((option) => {
      if (option.provider !== 'openrouter') return option;
      return {
        ...option,
        route_types: ['on_demand' as const, 'quota' as const, 'concurrency' as const],
      };
    });
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: quotaCapableOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockImplementation(async (provider?: string) =>
      providerKeysResponse(provider),
    );

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));
    chooseValue(screen.getByLabelText('Route type'), 'quota');

    const providerSelect = screen.getByLabelText('Provider');
    expect(providerSelect).toHaveAttribute('data-value', 'openrouter');
    const routingSelect = screen.getByLabelText('OpenRouter routing');
    // A quota route is metered against one provider, so the discovered pins
    // are the choices; Auto is not, and Custom stays as the escape hatch.
    expect(routingSelect).toHaveAttribute('data-value', 'provider:deepinfra');
    expect(
      within(openOptions(routingSelect)).getByRole('option', { name: 'Provider: DeepInfra' }),
    ).toBeInTheDocument();
    expect(
      within(openOptions(routingSelect)).getByRole('option', { name: 'Provider: Parasail' }),
    ).toBeInTheDocument();
    expect(
      within(openOptions(routingSelect)).queryByRole('option', { name: 'Auto' }),
    ).not.toBeInTheDocument();
    expect(
      within(openOptions(routingSelect)).getByRole('option', { name: /^Custom/ }),
    ).toBeInTheDocument();
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
      deployment_type: 'auto' as const,
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
    chooseValue(screen.getByLabelText('Route type'), 'concurrency');

    const providerSelect = screen.getByLabelText('Provider');
    expect(providerSelect).toHaveAttribute('data-value', 'openrouter');
    expect(
      within(openOptions(providerSelect)).getByRole('option', { name: 'OpenRouter' }),
    ).toBeInTheDocument();
    const routingSelect = screen.getByLabelText('OpenRouter routing');
    expect(routingSelect).toHaveAttribute('data-value', 'provider:deepinfra');
    expect(
      within(openOptions(routingSelect)).queryByRole('option', { name: 'Auto' }),
    ).not.toBeInTheDocument();
    expect(
      within(openOptions(routingSelect)).queryByRole('option', { name: 'Sort by price' }),
    ).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });
    chooseValue(routingSelect, 'provider:parasail');
    fireEvent.change(screen.getByLabelText('Concurrency limit'), {
      target: { value: '2' },
    });

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    chooseValue(screen.getByLabelText('API key'), 'key-1');
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    expect(createProviderRouteCandidate).not.toHaveBeenCalled();
    confirmAction('Add');

    await waitFor(() => {
      expect(createProviderRouteCandidate).toHaveBeenCalledWith('minimax-fast', {
        route_type: 'concurrency',
        upstream_provider: 'openrouter',
        openrouter_provider: 'parasail',
        openrouter_sort: null,
        base_url: 'https://openrouter.ai/api/v1',
        deployment_type: 'auto' as const,
        api_key_id: 'key-1',
        provider_model_id: 'minimax/minimax-m2.5',
        quota_limit: null,
        concurrency_limit: 2,
        weight: 1,
        capabilities: ROUTE_CAPABILITIES,
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
      deployment_type: 'auto' as const,
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
      deployment_type: 'auto' as const,
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
        within(openOptions(openRouterSelect)).getByRole('option', {
          name: 'Provider: MiniMax Highspeed',
        }),
      ).toBeInTheDocument();
    });
    chooseValue(openRouterSelect, 'provider:minimax/highspeed');

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    chooseValue(screen.getByLabelText('API key'), 'key-1');
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    expect(createProviderRouteCandidate).not.toHaveBeenCalled();
    confirmAction('Add');

    await waitFor(() => {
      expect(createProviderRouteCandidate).toHaveBeenCalledWith('minimax-fast', {
        route_type: 'on_demand',
        upstream_provider: 'openrouter',
        openrouter_provider: 'minimax/highspeed',
        openrouter_sort: null,
        base_url: 'https://openrouter.ai/api/v1',
        deployment_type: 'auto' as const,
        api_key_id: 'key-1',
        provider_model_id: 'minimax/minimax-m2.5',
        quota_limit: null,
        concurrency_limit: null,
        weight: 1,
        capabilities: ROUTE_CAPABILITIES,
      });
    });
  });

  it('updates a runtime OpenRouter concurrency limit after Edit and confirmation', async () => {
    const runtimeRoute = {
      ...route,
      route_id: 'minimax-fast:openrouter[parasail]-api',
      route_type: 'concurrency',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      deployment_type: 'auto' as const,
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

    const limit = await screen.findByLabelText(
      'Concurrency limit for minimax-fast:openrouter[parasail]-api',
    );
    expect(limit).toHaveTextContent('2');
    expect(
      screen.queryByLabelText('Concurrency limit for minimax-fast:featherless-api'),
    ).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole('button', {
        name: 'Edit minimax-fast:openrouter[parasail]-api concurrency limit',
      }),
    );
    const limitInput = screen.getByLabelText(
      'Concurrency limit for minimax-fast:openrouter[parasail]-api',
    );
    expect(limitInput).toHaveValue(2);
    fireEvent.change(limitInput, { target: { value: '4' } });
    fireEvent.click(
      screen.getByRole('button', {
        name: 'Save changes to minimax-fast:openrouter[parasail]-api concurrency limit',
      }),
    );
    expect(topDialog()).toHaveTextContent('Concurrency limit2 → 4');
    expect(updateProviderRouteCandidate).not.toHaveBeenCalled();
    confirmAction('Confirm save');

    await waitFor(() => {
      expect(updateProviderRouteCandidate).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:openrouter[parasail]-api',
        { concurrency_limit: 4 },
      );
    });
    expect(updateProviderRouteCandidate).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(
        screen.getByLabelText('Concurrency limit for minimax-fast:openrouter[parasail]-api'),
      ).toHaveTextContent('4');
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
      deployment_type: 'auto' as const,
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
    expect(
      within(openOptions(routeTypeSelect)).getByRole('option', { name: 'on_demand' }),
    ).toBeInTheDocument();
    expect(
      within(openOptions(routeTypeSelect)).queryByRole('option', { name: 'concurrency' }),
    ).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Model ID'), {
      target: { value: 'deepseek-v4-flash' },
    });
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'deepseek/deepseek-v4-flash' },
    });
    fillRuntimeModelPricing();
    fillRuntimeModelCapabilities();

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    chooseValue(screen.getByLabelText('API key'), 'key-1');
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await screen.findByRole('button', { name: 'Verified' });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    expect(createProviderRouteModel).not.toHaveBeenCalled();
    confirmAction('Create');

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
        deployment_type: 'auto' as const,
        api_key_id: 'key-1',
        provider_model_id: 'deepseek/deepseek-v4-flash',
        quota_limit: null,
        concurrency_limit: null,
        weight: 1,
        pricing: {
          prompt: '0.14',
          completion: '0.28',
        },
        capabilities: RUNTIME_MODEL_CAPABILITIES,
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
      deployment_type: 'auto' as const,
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
    chooseValue(screen.getByLabelText('Initial routing policy'), 'routewise');
    chooseValue(screen.getByLabelText('Route type'), 'concurrency');
    chooseValue(screen.getByLabelText('Provider'), 'openrouter');
    const routingSelect = screen.getByLabelText('OpenRouter routing');
    expect(routingSelect).toHaveAttribute('data-value', 'provider:deepinfra');
    expect(
      within(openOptions(routingSelect)).queryByRole('option', { name: 'Auto' }),
    ).not.toBeInTheDocument();
    expect(
      within(openOptions(routingSelect)).queryByRole('option', { name: 'Sort by price' }),
    ).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'deepseek/deepseek-v4-flash' },
    });
    chooseValue(routingSelect, 'provider:parasail');
    fireEvent.change(screen.getByLabelText('Concurrency limit'), {
      target: { value: '2' },
    });
    fillRuntimeModelPricing();
    fillRuntimeModelCapabilities();

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    chooseValue(screen.getByLabelText('API key'), 'key-1');
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    expect(createProviderRouteModel).not.toHaveBeenCalled();
    confirmAction('Create');

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
        deployment_type: 'auto' as const,
        api_key_id: 'key-1',
        provider_model_id: 'deepseek/deepseek-v4-flash',
        quota_limit: null,
        concurrency_limit: 2,
        weight: 1,
        pricing: {
          prompt: '0.14',
          completion: '0.28',
        },
        capabilities: RUNTIME_MODEL_CAPABILITIES,
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
    chooseValue(screen.getByLabelText('Initial routing policy'), 'routewise');
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'deepseek/deepseek-v4-flash' },
    });
    fillRuntimeModelPricing();
    fillRuntimeModelCapabilities();
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
        deployment_type: 'auto' as const,
        api_key_id: null,
        provider_model_id: 'deepseek/deepseek-v4-flash',
        quota_limit: null,
        concurrency_limit: null,
        weight: 1,
        pricing: {
          prompt: '0.14',
          completion: '0.28',
        },
        capabilities: RUNTIME_MODEL_CAPABILITIES,
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
    chooseValue(screen.getByLabelText('Visibility'), 'free');
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'deepseek/deepseek-v4-flash' },
    });
    fillRuntimeModelPricing();
    fillRuntimeModelCapabilities();
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

  it('keeps a new model unsubmittable until every capability is answered', async () => {
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
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'deepseek/deepseek-v4-flash' },
    });
    fillRuntimeModelPricing();

    const create = screen.getByRole('button', { name: 'Create' });
    const verify = screen.getByRole('button', { name: 'Verify' });
    expect(screen.getByLabelText('Image input')).toHaveAttribute('data-value', '');
    expect(screen.getByLabelText('Tool calling')).toHaveAttribute('data-value', '');
    expect(create).toBeDisabled();
    expect(verify).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Context length (tokens)'), {
      target: { value: '65536' },
    });
    fireEvent.change(screen.getByLabelText('Max output (tokens)'), {
      target: { value: '8192' },
    });
    chooseValue(screen.getByLabelText('Image input'), 'no');
    // Unanswered is not "no": leaving tool calling unset still blocks the form.
    expect(create).toBeDisabled();

    chooseValue(screen.getByLabelText('Tool calling'), 'no');
    expect(create).toBeEnabled();
    fireEvent.click(verify);

    await waitFor(() => {
      expect(verifyProviderRouteModel).toHaveBeenCalledWith(
        expect.objectContaining({
          capabilities: {
            context_length: 65536,
            max_output_length: 8192,
            input_modalities: ['text'],
            supports_tools: false,
          },
        }),
      );
    });
  });

  it('refuses a max output larger than the context length', async () => {
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
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'deepseek/deepseek-v4-flash' },
    });
    fillRuntimeModelPricing();
    fillRuntimeModelCapabilities();
    expect(screen.getByRole('button', { name: 'Create' })).toBeEnabled();

    fireEvent.change(screen.getByLabelText('Max output (tokens)'), {
      target: { value: '262144' },
    });

    expect(screen.getByText('Max output cannot exceed the context length.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled();
  });

  it.each([
    ['Context length (tokens)', '128e3'],
    ['Context length (tokens)', '0'],
    ['Context length (tokens)', '20000000'],
    ['Max output (tokens)', '1.5'],
  ])('says which field blocks Create when %s is %s', async (label, value) => {
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
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'deepseek/deepseek-v4-flash' },
    });
    fillRuntimeModelPricing();
    fillRuntimeModelCapabilities();
    expect(screen.queryByText(/Enter a whole number of tokens/)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(label), { target: { value } });

    expect(
      screen.getByText('Enter a whole number of tokens from 1 to 10,000,000.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled();
  });

  it("asks for an added route's own capabilities, starting from the first route's", async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));
    await screen.findByRole('dialog', { name: 'Add provider route' });
    expect(screen.getByLabelText('Context length (tokens)')).toHaveValue(196608);
    expect(screen.getByLabelText('Max output (tokens)')).toHaveValue(8192);
    expect(screen.getByLabelText('Image input')).toHaveAttribute('data-value', 'no');
    expect(screen.getByLabelText('Tool calling')).toHaveAttribute('data-value', 'yes');

    fireEvent.change(screen.getByLabelText('Context length (tokens)'), {
      target: { value: '0' },
    });
    expect(
      screen.getByText('Enter a whole number of tokens from 1 to 10,000,000.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled();
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
      deployment_type: 'auto' as const,
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
    chooseValue(screen.getByLabelText('Initial routing policy'), 'routewise');
    chooseValue(screen.getByLabelText('Route type'), 'quota');

    const providerSelect = screen.getByLabelText('Provider');
    expect(providerSelect).toHaveAttribute('data-value', 'chutes');
    expect(
      within(openOptions(providerSelect)).getByRole('option', { name: 'Chutes' }),
    ).toBeInTheDocument();
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
    chooseValue(screen.getByLabelText('OpenRouter routing'), 'sort:throughput');
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));

    await waitFor(() => {
      expect(verifyProviderRouteCandidate).toHaveBeenCalledWith('minimax-fast', {
        route_type: 'on_demand',
        upstream_provider: 'openrouter',
        openrouter_provider: null,
        openrouter_sort: 'throughput',
        base_url: 'https://openrouter.ai/api/v1',
        deployment_type: 'auto' as const,
        api_key_id: null,
        provider_model_id: 'minimax/minimax-m2.5',
        quota_limit: null,
        concurrency_limit: null,
        weight: 1,
        capabilities: ROUTE_CAPABILITIES,
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
      deployment_type: 'auto' as const,
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
          min_role: 'free',
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
    chooseValue(screen.getByLabelText('OpenRouter routing'), 'provider:__custom__');
    fireEvent.change(screen.getByLabelText('Custom OpenRouter provider'), {
      target: { value: 'novita' },
    });

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });
    chooseValue(screen.getByLabelText('API key'), 'key-1');
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await screen.findByRole('button', { name: 'Verified' });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    expect(createProviderRouteCandidate).not.toHaveBeenCalled();
    confirmAction('Add');

    await waitFor(() => {
      expect(createProviderRouteCandidate).toHaveBeenCalledWith('minimax-fast', {
        route_type: 'on_demand',
        upstream_provider: 'openrouter',
        openrouter_provider: 'novita',
        openrouter_sort: null,
        base_url: 'https://openrouter.ai/api/v1',
        deployment_type: 'auto' as const,
        api_key_id: 'key-1',
        provider_model_id: 'minimax/minimax-m2.5',
        quota_limit: null,
        concurrency_limit: null,
        weight: 1,
        capabilities: ROUTE_CAPABILITIES,
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
      deployment_type: 'auto' as const,
      provider_model_id: 'minimax/minimax-m2.5',
      endpoint_id: 'minimax-fast:openrouter-api',
      source: 'runtime',
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });
    chooseValue(screen.getByLabelText('OpenRouter routing'), 'sort:throughput');
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await screen.findByRole('button', { name: 'Verified' });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    expect(createProviderRouteCandidate).not.toHaveBeenCalled();
    confirmAction('Add');

    await waitFor(() => {
      expect(createProviderRouteCandidate).toHaveBeenCalledWith('minimax-fast', {
        route_type: 'on_demand',
        upstream_provider: 'openrouter',
        openrouter_provider: null,
        openrouter_sort: 'throughput',
        base_url: 'https://openrouter.ai/api/v1',
        deployment_type: 'auto' as const,
        api_key_id: null,
        provider_model_id: 'minimax/minimax-m2.5',
        quota_limit: null,
        concurrency_limit: null,
        weight: 1,
        capabilities: ROUTE_CAPABILITIES,
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
      deployment_type: 'auto' as const,
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
          route_types: ['quota' as const],
        },
        {
          provider: 'deepinfra',
          label: 'DeepInfra via OpenRouter',
          kind: 'openrouter[deepinfra]',
          key_provider: 'openrouter',
          default_base_url: 'https://openrouter.ai/api/v1',
          route_types: ['on_demand' as const, 'concurrency' as const],
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
    expect(providerSelect).toHaveAttribute('data-value', 'openrouter');
    expect(
      within(openOptions(providerSelect)).queryByRole('option', { name: 'Chutes' }),
    ).not.toBeInTheDocument();
    expect(
      within(openOptions(providerSelect)).queryByRole('option', { name: 'Featherless' }),
    ).not.toBeInTheDocument();
    expect(
      within(openOptions(providerSelect)).queryByRole('option', { name: 'DeepInfra' }),
    ).not.toBeInTheDocument();
    expect(
      within(openOptions(providerSelect)).getByRole('option', { name: 'OpenRouter' }),
    ).toBeInTheDocument();

    const openRouterSelect = screen.getByLabelText('OpenRouter routing');
    await waitFor(() => {
      expect(
        within(openOptions(openRouterSelect)).queryByRole('option', {
          name: 'Provider: DeepInfra',
        }),
      ).not.toBeInTheDocument();
    });
    expect(
      within(openOptions(openRouterSelect)).getByRole('option', { name: 'Provider: Inceptron' }),
    ).toBeInTheDocument();
    expect(
      within(openOptions(openRouterSelect)).getByRole('option', { name: 'Provider: Chutes' }),
    ).toBeInTheDocument();

    chooseValue(screen.getByLabelText('Route type'), 'quota');

    expect(providerSelect).toHaveAttribute('data-value', 'chutes');
    expect(
      within(openOptions(providerSelect)).getByRole('option', { name: 'Chutes' }),
    ).toBeInTheDocument();
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
      deployment_type: 'auto' as const,
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
          route_types: ['quota' as const],
        },
        ...providerOptions,
      ],
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route, quotaRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));
    chooseValue(screen.getByLabelText('Route type'), 'quota');

    const providerSelect = screen.getByLabelText('Provider');
    expect(providerSelect).toHaveAttribute('data-value', '');
    // Nothing to choose: the trigger says so instead of offering a fake option.
    expect(providerSelect).toHaveTextContent('No quota providers available');
    expect(
      screen.getByText('This model already has every configured quota provider.'),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('API key')).toHaveAttribute('data-value', '');
    expect(screen.getByLabelText('API key')).toHaveTextContent('No provider selected');
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
      deployment_type: 'auto' as const,
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
    confirmAction('Delete');

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
      deployment_type: 'auto' as const,
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

    expect(screen.getByLabelText('Override provider')).toHaveAttribute('data-value', 'zai');
    expect(
      within(openOptions(screen.getByLabelText('Override provider'))).getByRole('option', {
        name: 'zai',
      }),
    ).toBeInTheDocument();
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

    const policy = await screen.findByLabelText('Routing policy');
    expect(policy.tagName).toBe('OUTPUT');
    expect(policy).toHaveTextContent('routewise');

    fireEvent.click(screen.getByRole('button', { name: 'Edit minimax-fast routing policy' }));
    const select = screen.getByLabelText('Routing policy');
    expect(select).toHaveAttribute('data-value', 'routewise');
    chooseValue(select, 'fixed');
    expect(updateProviderRouteStrategy).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole('button', { name: 'Save changes to minimax-fast routing policy' }),
    );
    const review = screen.getByRole('dialog', {
      name: 'Save 1 change to minimax-fast routing policy?',
    });
    expect(review).toHaveTextContent('Routing policyroutewise → fixed');
    expect(updateProviderRouteStrategy).not.toHaveBeenCalled();
    confirmAction('Confirm save');

    await waitFor(() => {
      expect(updateProviderRouteStrategy).toHaveBeenCalledWith('minimax-fast', 'fixed');
    });
    expect(updateProviderRouteStrategy).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(screen.getByLabelText('Routing policy')).toHaveTextContent('fixed');
    });
  });

  it('preserves an engine declaration and invalidates verification when deployment changes', async () => {
    const engineRoute = { ...route, deployment_type: 'self_hosted' as const };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      routes: [engineRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'featherless', keys: [] });
    vi.mocked(verifyProviderRoute).mockResolvedValue({ ok: true });
    vi.mocked(updateProviderRoute).mockResolvedValue({
      ...engineRoute,
      deployment_type: 'provider_api',
    });
    render(<ProviderRoutesTab />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByLabelText('Deployment type')).toHaveAttribute('data-value', 'self_hosted');
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await screen.findByRole('button', { name: 'Verified' });
    // Untouched, the declaration is left to the server, which keeps it.
    const verified = vi.mocked(verifyProviderRoute).mock.calls[0];
    expect(verified.slice(0, 2)).toEqual([route.model_id, route.route_id]);
    expect(verified[2]).toMatchObject({ base_url: route.base_url });
    expect(verified[2]).not.toHaveProperty('deployment_type');
    chooseValue(screen.getByLabelText('Deployment type'), 'provider_api');
    expect(screen.getByRole('button', { name: 'Verify' })).toBeInTheDocument();
    expect(screen.getByLabelText('Base URL')).toHaveValue(route.base_url);
    saveRouteEdit();
    expect(topDialog()).toHaveTextContent('Deploymentself_hosted → provider_api');
    confirmAction('Confirm save');
    await waitFor(() => {
      expect(updateProviderRoute).toHaveBeenCalledWith(
        route.model_id,
        route.route_id,
        expect.objectContaining({ deployment_type: 'provider_api', base_url: route.base_url }),
      );
    });
  });

  it('does not carry an engine declaration to a new base URL', async () => {
    const engineRoute = { ...route, deployment_type: 'self_hosted' as const };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      routes: [engineRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'featherless', keys: [] });
    vi.mocked(verifyProviderRoute).mockResolvedValue({ ok: true });
    vi.mocked(updateProviderRoute).mockResolvedValue({ ...engineRoute, deployment_type: 'auto' });
    render(<ProviderRoutesTab />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    chooseValue(screen.getByLabelText('Deployment type'), 'self_hosted');
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://api.featherless.ai/v2' },
    });
    expect(screen.getByLabelText('Deployment type')).toHaveAttribute('data-value', 'auto');
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://API.featherless.ai/v1/' },
    });
    // Back on the route's own endpoint, its declaration shows again.
    expect(screen.getByLabelText('Deployment type')).toHaveAttribute('data-value', 'self_hosted');
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://api.featherless.ai/v2' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
    await screen.findByRole('button', { name: 'Verified' });
    saveRouteEdit();
    confirmAction('Confirm save');
    await waitFor(() => expect(updateProviderRoute).toHaveBeenCalled());
    const payload = vi.mocked(updateProviderRoute).mock.calls[0][2];
    expect(payload).toMatchObject({ base_url: 'https://api.featherless.ai/v2' });
    expect(payload).not.toHaveProperty('deployment_type');
  });

  it('keeps a pinned OpenRouter declaration only while the pin is unchanged', async () => {
    const openRouterRoute = {
      ...route,
      route_id: 'minimax-fast:openrouter[parasail]-api',
      route_type: 'on_demand',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      openrouter_provider: 'parasail',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      deployment_type: 'provider_api' as const,
      provider_model_id: 'minimax/minimax-m2.5',
      endpoint_id: 'minimax-fast:openrouter[parasail]-api',
    };
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [openRouterRoute],
    });
    vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });
    render(<ProviderRoutesTab />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByLabelText('Deployment type')).toHaveAttribute('data-value', 'provider_api');
    // Another pin is another endpoint; the server resets its declaration.
    chooseValue(screen.getByLabelText('OpenRouter routing'), 'provider:deepinfra');
    expect(screen.getByLabelText('Deployment type')).toHaveAttribute('data-value', 'auto');
    chooseValue(screen.getByLabelText('OpenRouter routing'), 'provider:parasail');
    expect(screen.getByLabelText('Deployment type')).toHaveAttribute('data-value', 'provider_api');
  });

  it('flags a saved override that is not in effect', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      routes: [{ ...route, source: 'override', override_applied: false }],
    });
    render(<ProviderRoutesTab />);
    expect(await screen.findByText('Override not applied')).toBeInTheDocument();
    expect(screen.queryByText('Override active')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reset config' })).toBeInTheDocument();
  });
  describe('confirmation of writes', () => {
    const fixedRoute = { ...route, strategy: 'fixed', route_type: 'on_demand' };
    const secondFixedRoute = {
      ...fixedRoute,
      route_id: 'minimax-fast:openrouter-api',
      provider: 'openrouter',
      upstream_provider: 'openrouter',
      key_provider: 'openrouter',
      base_url: 'https://openrouter.ai/api/v1',
      endpoint_id: 'minimax-fast:openrouter-api',
    };
    const weightFor = (endpointId: string, weight: number) => ({
      model_id: 'minimax-fast',
      strategy: 'fixed',
      endpoint_id: endpointId,
      provider: 'featherless',
      base_url: 'https://api.featherless.ai/v1',
      yaml_weight: 1,
      override_weight: weight === 1 ? null : weight,
      effective_weight: weight,
    });

    function renderRoutewiseRoute(routes: unknown[] = [route]) {
      vi.mocked(listProviderRoutes).mockResolvedValue({
        provider_options: providerOptions,
        openrouter_provider_options: openRouterProviderOptions,
        routes: routes as never,
      });
      vi.mocked(listProviderKeys).mockImplementation(async (provider?: string) =>
        providerKeysResponse(provider),
      );
      render(<ProviderRoutesTab />);
    }

    const writes = () => [
      updateProviderRoute,
      updateProviderRouteStrategy,
      updateProviderRouteCandidate,
      updateProviderRouteCapabilities,
      resetProviderRouteCapabilities,
      setRouteWeight,
      clearRouteWeight,
      createProviderRouteCandidate,
      createProviderRouteModel,
      deleteProviderRoute,
      deleteProviderRouteCandidate,
    ];

    it('never writes from browsing or reselecting a select, and holds the model while the policy is edited', async () => {
      const otherRoute = {
        ...route,
        model_id: 'other-model',
        route_id: 'other-model:featherless-api',
        endpoint_id: 'other-model:featherless-api',
      };
      renderRoutewiseRoute([route, otherRoute]);
      await screen.findByRole('button', { name: 'Edit minimax-fast routing policy' });
      const selector = screen.getByLabelText('Selected model');
      const createModel = screen.getByRole('button', { name: 'Create model' });

      // The searchable model list: browsing, dismissing and choosing the same model.
      fireEvent.keyDown(selector, { key: 'ArrowDown' });
      fireEvent.keyDown(selector, { key: 'ArrowDown' });
      fireEvent.keyDown(selector, { key: 'Escape' });
      chooseValue(selector, 'minimax-fast');
      expect(selector).toHaveAttribute('data-value', 'minimax-fast');

      fireEvent.click(screen.getByRole('button', { name: 'Edit minimax-fast routing policy' }));
      expect(selector).toBeDisabled();
      expect(createModel).toBeDisabled();
      const policy = screen.getByLabelText('Routing policy');
      fireEvent.keyDown(policy, { key: 'ArrowDown' });
      fireEvent.keyDown(policy, { key: 'Escape' });
      chooseValue(policy, 'routewise');
      expect(screen.getByText('No changes yet')).toBeVisible();
      expect(
        screen.getByRole('button', { name: 'Save changes to minimax-fast routing policy' }),
      ).toBeDisabled();
      // A different policy is only a draft.
      chooseValue(policy, 'fixed');
      expect(screen.getByText('1 unsaved change')).toBeVisible();
      expect(selector).toBeDisabled();

      // Cancel discards it and releases the model, which can then be switched.
      fireEvent.click(
        screen.getByRole('button', { name: 'Cancel editing minimax-fast routing policy' }),
      );
      expect(screen.getByLabelText('Routing policy')).toHaveTextContent('routewise');
      expect(selector).toBeEnabled();
      expect(createModel).toBeEnabled();
      chooseValue(selector, 'other-model');
      expect(selector).toHaveAttribute('data-value', 'other-model');
      expect(
        await screen.findByRole('button', { name: 'Edit other-model routing policy' }),
      ).toBeEnabled();

      // The route editor's selects: the same provider again is no change.
      fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
      const provider = screen.getByLabelText('Override provider');
      fireEvent.keyDown(provider, { key: 'ArrowDown' });
      fireEvent.keyDown(provider, { key: 'Escape' });
      chooseValue(provider, 'featherless');
      expect(screen.getByText('No changes yet')).toBeVisible();
      expect(
        screen.getByRole('button', { name: 'Save changes to other-model / featherless' }),
      ).toBeDisabled();

      for (const write of writes()) expect(write).not.toHaveBeenCalled();
    });

    it('holds the model selection while fixed weights are edited, and Cancel releases it', async () => {
      const otherRoute = {
        ...fixedRoute,
        model_id: 'other-model',
        route_id: 'other-model:featherless-api',
        endpoint_id: 'other-model:featherless-api',
      };
      vi.mocked(listProviderRoutes).mockResolvedValue({
        provider_options: providerOptions,
        openrouter_provider_options: openRouterProviderOptions,
        routes: [fixedRoute, otherRoute],
      });
      render(<ProviderRoutesTab />);

      fireEvent.click(await screen.findByRole('button', { name: 'Edit minimax-fast weights' }));
      const selector = screen.getByLabelText('Selected model');
      expect(selector).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Create model' })).toBeDisabled();
      fireEvent.change(screen.getByLabelText(`Runtime weight for ${fixedRoute.endpoint_id}`), {
        target: { value: '3' },
      });

      fireEvent.click(screen.getByRole('button', { name: 'Cancel editing minimax-fast weights' }));
      expect(selector).toBeEnabled();
      chooseValue(selector, 'other-model');
      expect(
        await screen.findByRole('button', { name: 'Edit other-model weights' }),
      ).toBeInTheDocument();
      expect(setRouteWeight).not.toHaveBeenCalled();
    });

    it('keeps Save disabled for an unchanged route, and Cancel closes it with zero writes', async () => {
      renderRoutewiseRoute();

      fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
      expect(screen.getByText('No changes yet')).toBeVisible();
      expect(
        screen.getByRole('button', { name: 'Save changes to minimax-fast / featherless' }),
      ).toBeDisabled();

      fireEvent.change(screen.getByLabelText('Provider model ID'), {
        target: { value: 'something-else' },
      });
      // Typed back to the saved value, it is no change.
      fireEvent.change(screen.getByLabelText('Provider model ID'), {
        target: { value: 'MiniMaxAI/MiniMax-M2.5' },
      });
      expect(
        screen.getByRole('button', { name: 'Save changes to minimax-fast / featherless' }),
      ).toBeDisabled();

      fireEvent.change(screen.getByLabelText('Provider model ID'), {
        target: { value: 'something-else' },
      });
      fireEvent.click(
        screen.getByRole('button', { name: 'Cancel editing minimax-fast / featherless' }),
      );
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(updateProviderRoute).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
      expect(screen.getByLabelText('Provider model ID')).toHaveValue('MiniMaxAI/MiniMax-M2.5');
    });

    it('refuses a zero concurrency limit before the review', async () => {
      renderRoutewiseRoute();

      fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
      fireEvent.change(screen.getByLabelText('Local concurrency limit'), {
        target: { value: '0' },
      });
      saveRouteEdit();

      // The browser's own check (min 1) stops the submit; the review never opens.
      expect(screen.getByLabelText('Local concurrency limit')).toBeInvalid();
      expect(screen.getAllByRole('dialog')).toHaveLength(1);
      expect(verifyProviderRoute).not.toHaveBeenCalled();
      expect(updateProviderRoute).not.toHaveBeenCalled();
    });

    it('keeps the editor and its drafts when a route save fails, and retries once', async () => {
      vi.mocked(updateProviderRoute)
        .mockRejectedValueOnce(new Error('upstream refused the target'))
        .mockResolvedValueOnce({ ...route, provider_model_id: 'minimax/minimax-m2.5' });
      renderRoutewiseRoute();

      fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
      fireEvent.change(screen.getByLabelText('Provider model ID'), {
        target: { value: 'minimax/minimax-m2.5' },
      });
      saveRouteEdit();
      confirmAction('Confirm save');

      expect(await within(topDialog()).findByRole('alert')).toHaveTextContent(
        'upstream refused the target',
      );
      expect(screen.getByLabelText('Provider model ID')).toHaveValue('minimax/minimax-m2.5');
      expect(updateProviderRoute).toHaveBeenCalledTimes(1);

      confirmAction('Confirm save');
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(updateProviderRoute).toHaveBeenCalledTimes(2);
    });

    it('blocks a second Confirm and closing while a route save is in flight', async () => {
      let finish!: (value: typeof route) => void;
      vi.mocked(updateProviderRoute).mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve as (value: typeof route) => void;
          }),
      );
      renderRoutewiseRoute();

      fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
      fireEvent.change(screen.getByLabelText('Provider model ID'), {
        target: { value: 'minimax/minimax-m2.5' },
      });
      saveRouteEdit();
      confirmAction('Confirm save');

      const confirm = topDialog();
      expect(within(confirm).getByRole('button', { name: 'Saving…' })).toBeDisabled();
      expect(within(confirm).getByRole('button', { name: 'Back to editing' })).toBeDisabled();
      fireEvent.click(within(confirm).getByRole('button', { name: 'Saving…' }));
      fireEvent.keyDown(window, { key: 'Escape' });
      expect(screen.getAllByRole('dialog')).toHaveLength(2);
      expect(updateProviderRoute).toHaveBeenCalledTimes(1);

      finish({ ...route, provider_model_id: 'minimax/minimax-m2.5' });
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(updateProviderRoute).toHaveBeenCalledTimes(1);
    });

    it('reviews what a new route will add, keeps the form on Back, and adds it once', async () => {
      renderRoutewiseRoute();
      vi.mocked(createProviderRouteCandidate).mockResolvedValue({
        ...route,
        route_id: 'minimax-fast:openrouter-api',
        route_type: 'on_demand',
        provider: 'openrouter',
        upstream_provider: 'openrouter',
        key_provider: 'openrouter',
        base_url: 'https://openrouter.ai/api/v1',
        provider_model_id: 'minimax/minimax-m2.5',
        endpoint_id: 'minimax-fast:openrouter-api',
        source: 'runtime',
      });

      fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));
      fireEvent.change(screen.getByLabelText('Provider model ID'), {
        target: { value: 'minimax/minimax-m2.5' },
      });
      chooseValue(screen.getByLabelText('OpenRouter routing'), 'sort:throughput');
      fireEvent.click(screen.getByRole('button', { name: 'Add' }));

      const review = screen.getByRole('dialog', {
        name: 'Add a provider route to minimax-fast?',
      });
      expect(review).toHaveTextContent('Route typeon_demand');
      expect(review).toHaveTextContent('ProviderOpenRouter');
      expect(review).toHaveTextContent('OpenRouter routingSort by throughput');
      expect(review).toHaveTextContent('Base URLhttps://openrouter.ai/api/v1');
      expect(review).toHaveTextContent('Provider model IDminimax/minimax-m2.5');
      // The added route declares its own capabilities, started from the first route's.
      expect(review).toHaveTextContent('Context length196,608 tokens');
      expect(review).toHaveTextContent('Image inputText only');
      expect(review).toHaveTextContent('Tool callingSupported');
      expect(within(review).getByRole('button', { name: 'Back to editing' })).toHaveFocus();

      fireEvent.click(within(review).getByRole('button', { name: 'Back to editing' }));
      expect(screen.getByRole('dialog', { name: 'Add provider route' })).toBeInTheDocument();
      expect(screen.getByLabelText('Provider model ID')).toHaveValue('minimax/minimax-m2.5');
      expect(createProviderRouteCandidate).not.toHaveBeenCalled();

      fireEvent.click(screen.getByRole('button', { name: 'Add' }));
      confirmAction('Add');
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(createProviderRouteCandidate).toHaveBeenCalledTimes(1);
      expect(createProviderRouteCandidate).toHaveBeenCalledWith(
        'minimax-fast',
        expect.objectContaining({
          provider_model_id: 'minimax/minimax-m2.5',
          openrouter_sort: 'throughput',
          capabilities: ROUTE_CAPABILITIES,
        }),
      );
      expect(await screen.findByText('Runtime added')).toBeInTheDocument();
    });

    it('reviews a new model before creating it and keeps a failed create retryable', async () => {
      vi.mocked(createProviderRouteModel)
        .mockRejectedValueOnce(new Error('Model already exists'))
        .mockResolvedValueOnce({
          ...route,
          model_id: 'deepseek-v4-flash',
          strategy: 'fixed',
          route_id: 'deepseek-v4-flash:openrouter-api',
          route_type: 'on_demand',
          provider: 'openrouter',
          upstream_provider: 'openrouter',
          key_provider: 'openrouter',
          base_url: 'https://openrouter.ai/api/v1',
          provider_model_id: 'deepseek/deepseek-v4-flash',
          endpoint_id: 'deepseek-v4-flash:openrouter-api',
          source: 'runtime',
        });
      renderRoutewiseRoute();

      fireEvent.click(await screen.findByRole('button', { name: 'Create model' }));
      fireEvent.change(screen.getByLabelText('Model ID'), {
        target: { value: 'deepseek-v4-flash' },
      });
      fireEvent.change(screen.getByLabelText('Provider model ID'), {
        target: { value: 'deepseek/deepseek-v4-flash' },
      });
      fillRuntimeModelPricing();
      fillRuntimeModelCapabilities();
      fireEvent.click(screen.getByRole('button', { name: 'Create' }));

      const review = screen.getByRole('dialog', { name: 'Create model deepseek-v4-flash?' });
      expect(review).toHaveTextContent('Model IDdeepseek-v4-flash');
      expect(review).toHaveTextContent('VisibilityAdmin only');
      expect(review).toHaveTextContent('Prompt $/M0.14');
      expect(review).toHaveTextContent('Context length131,072 tokens');
      expect(review).toHaveTextContent('Image inputAccepts images');
      expect(review).toHaveTextContent('Tool callingSupported');
      expect(createProviderRouteModel).not.toHaveBeenCalled();

      confirmAction('Create');
      expect(await within(topDialog()).findByRole('alert')).toHaveTextContent(
        'Model already exists',
      );
      expect(screen.getByRole('dialog', { name: 'Create model' })).toBeInTheDocument();

      confirmAction('Create');
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(createProviderRouteModel).toHaveBeenCalledTimes(2);
      expect(await screen.findByText('deepseek-v4-flash')).toBeInTheDocument();
    });

    it('cancels weight edits with zero writes', async () => {
      vi.mocked(listProviderRoutes).mockResolvedValue({
        provider_options: providerOptions,
        openrouter_provider_options: openRouterProviderOptions,
        routes: [fixedRoute],
      });

      render(<ProviderRoutesTab />);

      fireEvent.click(await screen.findByRole('button', { name: 'Edit minimax-fast weights' }));
      fireEvent.change(screen.getByLabelText('Runtime weight for minimax-fast:featherless-api'), {
        target: { value: '5' },
      });
      expect(screen.getByText('1 unsaved change')).toBeVisible();
      fireEvent.click(screen.getByRole('button', { name: 'Cancel editing minimax-fast weights' }));

      expect(
        screen.getByLabelText('Runtime weight for minimax-fast:featherless-api'),
      ).toHaveTextContent('1');
      expect(setRouteWeight).not.toHaveBeenCalled();
    });

    it('refuses a negative weight before the review', async () => {
      vi.mocked(listProviderRoutes).mockResolvedValue({
        provider_options: providerOptions,
        openrouter_provider_options: openRouterProviderOptions,
        routes: [fixedRoute],
      });

      render(<ProviderRoutesTab />);

      fireEvent.click(await screen.findByRole('button', { name: 'Edit minimax-fast weights' }));
      fireEvent.change(screen.getByLabelText('Runtime weight for minimax-fast:featherless-api'), {
        target: { value: '-1' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Save changes to minimax-fast weights' }));

      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(screen.getByText('Weight must be a non-negative number.')).toBeVisible();
      expect(setRouteWeight).not.toHaveBeenCalled();
    });

    it('reports a partial weight save and retries only the unsaved weight', async () => {
      vi.mocked(listProviderRoutes).mockResolvedValue({
        provider_options: providerOptions,
        openrouter_provider_options: openRouterProviderOptions,
        routes: [fixedRoute, secondFixedRoute],
      });
      vi.mocked(listRouteWeights).mockResolvedValue([
        weightFor(fixedRoute.endpoint_id, 1),
        weightFor(secondFixedRoute.endpoint_id, 1),
      ]);
      vi.mocked(setRouteWeight)
        .mockResolvedValueOnce(weightFor(fixedRoute.endpoint_id, 2))
        .mockRejectedValueOnce(new Error('write failed'))
        .mockResolvedValueOnce(weightFor(secondFixedRoute.endpoint_id, 4));

      render(<ProviderRoutesTab />);

      fireEvent.click(await screen.findByRole('button', { name: 'Edit minimax-fast weights' }));
      fireEvent.change(screen.getByLabelText(`Runtime weight for ${fixedRoute.endpoint_id}`), {
        target: { value: '2' },
      });
      fireEvent.change(
        screen.getByLabelText(`Runtime weight for ${secondFixedRoute.endpoint_id}`),
        { target: { value: '4' } },
      );
      fireEvent.click(screen.getByRole('button', { name: 'Save changes to minimax-fast weights' }));
      expect(
        screen.getByRole('dialog', { name: 'Save 2 changes to minimax-fast weights?' }),
      ).toBeInTheDocument();
      confirmAction('Confirm save');

      expect(
        await screen.findByText(/minimax-fast:openrouter-api could not be saved: write failed/),
      ).toHaveTextContent('1 change was saved; the rest are still unsaved.');
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(screen.getByText('1 unsaved change')).toBeVisible();

      fireEvent.click(screen.getByRole('button', { name: 'Save changes to minimax-fast weights' }));
      expect(
        screen.getByRole('dialog', { name: 'Save 1 change to minimax-fast weights?' }),
      ).not.toHaveTextContent(fixedRoute.endpoint_id);
      confirmAction('Confirm save');
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(vi.mocked(setRouteWeight).mock.calls).toEqual([
        ['minimax-fast', fixedRoute.endpoint_id, 2],
        ['minimax-fast', secondFixedRoute.endpoint_id, 4],
        ['minimax-fast', secondFixedRoute.endpoint_id, 4],
      ]);
    });

    it('cancels a routing policy edit with zero writes', async () => {
      renderRoutewiseRoute();

      fireEvent.click(
        await screen.findByRole('button', { name: 'Edit minimax-fast routing policy' }),
      );
      chooseValue(screen.getByLabelText('Routing policy'), 'fixed');
      fireEvent.click(
        screen.getByRole('button', { name: 'Cancel editing minimax-fast routing policy' }),
      );

      expect(screen.getByLabelText('Routing policy')).toHaveTextContent('routewise');
      expect(updateProviderRouteStrategy).not.toHaveBeenCalled();
    });

    it('refuses an invalid concurrency limit before the review', async () => {
      const runtimeRoute = {
        ...route,
        route_id: 'minimax-fast:openrouter[parasail]-api',
        provider: 'openrouter',
        upstream_provider: 'openrouter',
        openrouter_provider: 'parasail',
        key_provider: 'openrouter',
        base_url: 'https://openrouter.ai/api/v1',
        concurrency_limit: 2,
        endpoint_id: 'minimax-fast:openrouter[parasail]-api',
        source: 'runtime' as const,
      };
      renderRoutewiseRoute([route, runtimeRoute]);

      fireEvent.click(
        await screen.findByRole('button', {
          name: 'Edit minimax-fast:openrouter[parasail]-api concurrency limit',
        }),
      );
      fireEvent.change(
        screen.getByLabelText('Concurrency limit for minimax-fast:openrouter[parasail]-api'),
        { target: { value: '0' } },
      );
      fireEvent.click(
        screen.getByRole('button', {
          name: 'Save changes to minimax-fast:openrouter[parasail]-api concurrency limit',
        }),
      );

      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(screen.getByText('Concurrency limit must be a positive integer.')).toBeVisible();
      expect(updateProviderRouteCandidate).not.toHaveBeenCalled();
    });

    it('keeps a failed row action in its dialog, and Cancel writes nothing', async () => {
      const runtimeRoute = { ...route, source: 'runtime' as const };
      vi.mocked(deleteProviderRouteCandidate).mockRejectedValue(new Error('route is in use'));
      renderRoutewiseRoute([runtimeRoute]);

      fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
      const confirm = screen.getByRole('dialog', { name: 'Delete minimax-fast / featherless?' });
      expect(within(confirm).getByRole('button', { name: 'Cancel' })).toHaveFocus();
      confirmAction('Delete');

      expect(await within(confirm).findByRole('alert')).toHaveTextContent('route is in use');
      expect(deleteProviderRouteCandidate).toHaveBeenCalledTimes(1);
      fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }));
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(deleteProviderRouteCandidate).toHaveBeenCalledTimes(1);
    });

    it('asks before disabling a route, and Cancel writes nothing', async () => {
      renderRoutewiseRoute();

      fireEvent.click(await screen.findByRole('button', { name: 'Disable' }));
      const confirm = screen.getByRole('dialog', { name: 'Disable minimax-fast / featherless?' });
      fireEvent.keyDown(window, { key: 'Escape' });
      expect(confirm).not.toBeInTheDocument();
      expect(setRouteWeight).not.toHaveBeenCalled();
    });
  });

  describe('route capabilities', () => {
    const WIDE = {
      context_length: 1048576,
      max_output_length: 65536,
      input_modalities: ['text', 'image'],
      supports_tools: true,
    };

    const renderWithRoute = async (shown = route) => {
      vi.mocked(listProviderRoutes).mockResolvedValue({
        provider_options: providerOptions,
        openrouter_provider_options: openRouterProviderOptions,
        routes: [shown],
      });
      render(<ProviderRoutesTab />);
      fireEvent.click(
        await screen.findByRole('button', {
          name: 'Edit minimax-fast:featherless-api capabilities',
        }),
      );
      return screen.findByRole('dialog', { name: 'Route capabilities' });
    };

    const saveCapabilities = (dialog: HTMLElement) =>
      fireEvent.click(within(dialog).getByRole('button', { name: 'Save capabilities' }));

    it('edits a route from what it serves now, writing only after the review', async () => {
      vi.mocked(updateProviderRouteCapabilities).mockResolvedValue({
        ...route,
        capabilities: WIDE,
        capabilities_overridden: true,
      });
      const dialog = await renderWithRoute();

      expect(within(dialog).getByLabelText('Context length (tokens)')).toHaveValue(196608);
      expect(within(dialog).getByLabelText('Tool calling')).toHaveAttribute('data-value', 'yes');
      expect(
        within(dialog).getByText('From config: 196,608 context · 8,192 output · text only · tools'),
      ).toBeInTheDocument();
      expect(within(dialog).queryByRole('button', { name: 'Reset to config' })).toBeNull();
      expect(within(dialog).getByText('No changes yet')).toBeVisible();
      expect(within(dialog).getByRole('button', { name: 'Save capabilities' })).toBeDisabled();

      fireEvent.change(within(dialog).getByLabelText('Context length (tokens)'), {
        target: { value: '1048576' },
      });
      fireEvent.change(within(dialog).getByLabelText('Max output (tokens)'), {
        target: { value: '65536' },
      });
      chooseValue(within(dialog).getByLabelText('Image input'), 'yes');
      expect(within(dialog).getByText('3 unsaved changes')).toBeVisible();
      expect(updateProviderRouteCapabilities).not.toHaveBeenCalled();
      saveCapabilities(dialog);

      const review = screen.getByRole('dialog', {
        name: 'Save 3 changes to minimax-fast:featherless-api capabilities?',
      });
      expect(review).toHaveTextContent('Context length196,608 tokens → 1,048,576 tokens');
      expect(review).toHaveTextContent('Max output8,192 tokens → 65,536 tokens');
      expect(review).toHaveTextContent('Image inputText only → Accepts images');
      expect(review).not.toHaveTextContent('Tool calling');
      expect(updateProviderRouteCapabilities).not.toHaveBeenCalled();

      // Back keeps the draft.
      confirmAction('Back to editing');
      expect(screen.getAllByRole('dialog')).toHaveLength(1);
      expect(within(dialog).getByLabelText('Context length (tokens)')).toHaveValue(1048576);
      expect(updateProviderRouteCapabilities).not.toHaveBeenCalled();

      saveCapabilities(dialog);
      confirmAction('Confirm save');
      await waitFor(() => {
        expect(updateProviderRouteCapabilities).toHaveBeenCalledWith(
          'minimax-fast',
          'minimax-fast:featherless-api',
          WIDE,
        );
      });
      expect(updateProviderRouteCapabilities).toHaveBeenCalledTimes(1);
      expect(await screen.findByText('Capabilities edited')).toBeInTheDocument();
      expect(screen.queryByRole('dialog', { name: 'Route capabilities' })).toBeNull();
    });

    it('drafts nothing from browsing or reselecting a capability, and Cancel discards', async () => {
      const dialog = await renderWithRoute();
      const tools = within(dialog).getByLabelText('Tool calling');

      fireEvent.keyDown(tools, { key: 'ArrowDown' });
      fireEvent.keyDown(tools, { key: 'ArrowDown' });
      fireEvent.keyDown(tools, { key: 'Escape' });
      chooseValue(tools, 'yes');
      expect(within(dialog).getByText('No changes yet')).toBeVisible();
      expect(within(dialog).getByRole('button', { name: 'Save capabilities' })).toBeDisabled();

      // Changed and changed back is no change either.
      chooseValue(tools, 'no');
      expect(within(dialog).getByText('1 unsaved change')).toBeVisible();
      chooseValue(tools, 'yes');
      expect(within(dialog).getByRole('button', { name: 'Save capabilities' })).toBeDisabled();

      chooseValue(tools, 'no');
      fireEvent.click(
        within(dialog).getByRole('button', {
          name: 'Cancel editing minimax-fast:featherless-api capabilities',
        }),
      );
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(updateProviderRouteCapabilities).not.toHaveBeenCalled();
      expect(resetProviderRouteCapabilities).not.toHaveBeenCalled();

      fireEvent.click(
        screen.getByRole('button', { name: 'Edit minimax-fast:featherless-api capabilities' }),
      );
      expect(screen.getByLabelText('Tool calling')).toHaveAttribute('data-value', 'yes');
    });

    it('resets an edited route to its config values only after the confirmation', async () => {
      vi.mocked(resetProviderRouteCapabilities).mockResolvedValue(route);
      const dialog = await renderWithRoute({
        ...route,
        capabilities: WIDE,
        capabilities_overridden: true,
      });
      expect(screen.getByText('Capabilities edited')).toBeInTheDocument();

      fireEvent.click(within(dialog).getByRole('button', { name: 'Reset to config' }));
      const confirm = screen.getByRole('dialog', {
        name: 'Reset minimax-fast:featherless-api capabilities to config?',
      });
      expect(confirm).toHaveTextContent('Context length1,048,576 tokens → 196,608 tokens');
      expect(confirm).toHaveTextContent('Image inputAccepts images → Text only');
      expect(resetProviderRouteCapabilities).not.toHaveBeenCalled();

      // Cancel writes nothing and leaves the editor open.
      fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }));
      expect(screen.getAllByRole('dialog')).toHaveLength(1);
      expect(resetProviderRouteCapabilities).not.toHaveBeenCalled();

      fireEvent.click(within(dialog).getByRole('button', { name: 'Reset to config' }));
      confirmAction('Reset');
      await waitFor(() => {
        expect(resetProviderRouteCapabilities).toHaveBeenCalledWith(
          'minimax-fast',
          'minimax-fast:featherless-api',
        );
      });
      expect(resetProviderRouteCapabilities).toHaveBeenCalledTimes(1);
      await waitFor(() => expect(screen.queryByText('Capabilities edited')).toBeNull());
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(updateProviderRouteCapabilities).not.toHaveBeenCalled();
    });

    it('says which value blocks saving', async () => {
      const dialog = await renderWithRoute();

      fireEvent.change(within(dialog).getByLabelText('Max output (tokens)'), {
        target: { value: '262144' },
      });

      expect(
        within(dialog).getByText('Max output cannot exceed the context length.'),
      ).toBeInTheDocument();
      expect(within(dialog).getByRole('button', { name: 'Save capabilities' })).toBeDisabled();
      expect(updateProviderRouteCapabilities).not.toHaveBeenCalled();
    });

    it('blocks a second Confirm and closing while a capability save is in flight', async () => {
      let finish!: (value: typeof route) => void;
      vi.mocked(updateProviderRouteCapabilities).mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve as (value: typeof route) => void;
          }),
      );
      const dialog = await renderWithRoute();
      chooseValue(within(dialog).getByLabelText('Tool calling'), 'no');
      saveCapabilities(dialog);
      confirmAction('Confirm save');

      const confirm = topDialog();
      expect(within(confirm).getByRole('button', { name: 'Saving…' })).toBeDisabled();
      expect(within(confirm).getByRole('button', { name: 'Back to editing' })).toBeDisabled();
      expect(within(dialog).getByRole('button', { name: 'Close' })).toBeDisabled();
      fireEvent.click(within(confirm).getByRole('button', { name: 'Saving…' }));
      fireEvent.keyDown(window, { key: 'Escape' });
      expect(screen.getAllByRole('dialog')).toHaveLength(2);
      expect(updateProviderRouteCapabilities).toHaveBeenCalledTimes(1);

      finish({ ...route, capabilities: { ...ROUTE_CAPABILITIES, supports_tools: false } });
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(updateProviderRouteCapabilities).toHaveBeenCalledTimes(1);
    });

    // As models.yaml can declare a route: inputs the form has no field for.
    const MEDIA = { ...ROUTE_CAPABILITIES, input_modalities: ['text', 'audio', 'video'] };
    const MEDIA_NOTE = 'Also accepts audio and video input, which this form leaves as declared.';

    it('keeps the audio and video a route accepts when saving its other values', async () => {
      vi.mocked(updateProviderRouteCapabilities).mockResolvedValue({
        ...route,
        capabilities: MEDIA,
        capabilities_baseline: MEDIA,
        capabilities_overridden: true,
      });
      const dialog = await renderWithRoute({
        ...route,
        capabilities: MEDIA,
        capabilities_baseline: MEDIA,
      });

      expect(
        within(dialog).getByText(
          'From config: 196,608 context · 8,192 output · text, audio and video · tools',
        ),
      ).toBeInTheDocument();
      expect(within(dialog).getByText(MEDIA_NOTE)).toBeInTheDocument();

      fireEvent.change(within(dialog).getByLabelText('Max output (tokens)'), {
        target: { value: '65536' },
      });
      chooseValue(within(dialog).getByLabelText('Image input'), 'yes');
      saveCapabilities(dialog);
      // The kept modalities are no change, so the review does not list them.
      expect(topDialog()).not.toHaveTextContent('Other input');
      confirmAction('Confirm save');

      await waitFor(() => {
        expect(updateProviderRouteCapabilities).toHaveBeenCalledWith(
          'minimax-fast',
          'minimax-fast:featherless-api',
          {
            ...ROUTE_CAPABILITIES,
            max_output_length: 65536,
            input_modalities: ['text', 'image', 'audio', 'video'],
          },
        );
      });
    });

    it('starts an added route with the audio and video the first route accepts', async () => {
      vi.mocked(listProviderRoutes).mockResolvedValue({
        provider_options: providerOptions,
        openrouter_provider_options: openRouterProviderOptions,
        routes: [{ ...route, capabilities: MEDIA, capabilities_baseline: MEDIA }],
      });
      vi.mocked(listProviderKeys).mockResolvedValue({ provider: 'openrouter', keys: [] });
      vi.mocked(createProviderRouteCandidate).mockResolvedValue({
        ...route,
        route_id: 'minimax-fast:openrouter-api',
        route_type: 'on_demand',
        provider: 'openrouter',
        upstream_provider: 'openrouter',
        key_provider: 'openrouter',
        base_url: 'https://openrouter.ai/api/v1',
        provider_model_id: 'minimax/minimax-m2.5',
        endpoint_id: 'minimax-fast:openrouter-api',
        source: 'runtime',
        capabilities: MEDIA,
      });

      render(<ProviderRoutesTab />);

      fireEvent.click(await screen.findByRole('button', { name: 'Add provider' }));
      const form = await screen.findByRole('dialog', { name: 'Add provider route' });
      expect(within(form).getByText(MEDIA_NOTE)).toBeInTheDocument();
      fireEvent.change(screen.getByLabelText('Provider model ID'), {
        target: { value: 'minimax/minimax-m2.5' },
      });
      chooseValue(screen.getByLabelText('OpenRouter routing'), 'sort:throughput');
      fireEvent.click(screen.getByRole('button', { name: 'Verify' }));
      await screen.findByRole('button', { name: 'Verified' });
      expect(verifyProviderRouteCandidate).toHaveBeenCalledWith(
        'minimax-fast',
        expect.objectContaining({ capabilities: MEDIA }),
      );
      fireEvent.click(screen.getByRole('button', { name: 'Add' }));
      expect(topDialog()).toHaveTextContent('Other inputaudio and video');
      expect(createProviderRouteCandidate).not.toHaveBeenCalled();
      confirmAction('Add');

      await waitFor(() => {
        expect(createProviderRouteCandidate).toHaveBeenCalledWith(
          'minimax-fast',
          expect.objectContaining({ capabilities: MEDIA }),
        );
      });
      expect(createProviderRouteCandidate).toHaveBeenCalledTimes(1);
    });

    it("shows the gateway's reason when a capability change is refused, and keeps the draft", async () => {
      const reason =
        "duplicate provider route id 'minimax-fast:featherless-api'; set unique endpoint_id " +
        'values before editing this route';
      // The shared client reads a typed 409 as a signup conflict; the dialog must not.
      vi.mocked(updateProviderRouteCapabilities).mockRejectedValue(
        new APIError('USER_ALREADY_EXISTS', reason, 409),
      );
      const dialog = await renderWithRoute();
      fireEvent.change(within(dialog).getByLabelText('Context length (tokens)'), {
        target: { value: '262144' },
      });

      saveCapabilities(dialog);
      confirmAction('Confirm save');

      const alert = await within(topDialog()).findByRole('alert');
      expect(alert).toHaveTextContent(reason);
      expect(alert).not.toHaveTextContent('This email is already registered');
      expect(screen.getByRole('dialog', { name: 'Route capabilities' })).toBeInTheDocument();
      expect(within(dialog).getByLabelText('Context length (tokens)')).toHaveValue(262144);
      expect(updateProviderRouteCapabilities).toHaveBeenCalledTimes(1);
      expect(toast.success).not.toHaveBeenCalled();
    });

    it('holds the model selection while a capability edit is open', async () => {
      const dialog = await renderWithRoute();
      expect(screen.getByLabelText('Selected model')).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Create model' })).toBeDisabled();
      fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
      expect(screen.getByLabelText('Selected model')).toBeEnabled();
    });
  });
});
