// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ProviderRoutesTab } from './ProviderRoutesTab';

vi.mock('@/lib/api/admin', () => ({
  createProviderRouteCandidate: vi.fn(),
  deleteProviderRoute: vi.fn(),
  deleteProviderRouteCandidate: vi.fn(),
  listOpenRouterProviderOptions: vi.fn(),
  listProviderKeys: vi.fn(),
  listProviderRoutes: vi.fn(),
  updateProviderRoute: vi.fn(),
  updateProviderRouteStrategy: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

import {
  createProviderRouteCandidate,
  deleteProviderRoute,
  deleteProviderRouteCandidate,
  listOpenRouterProviderOptions,
  listProviderKeys,
  listProviderRoutes,
  updateProviderRoute,
  updateProviderRouteStrategy,
} from '@/lib/api/admin';

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

const discoveredOpenRouterProviderOptions = [
  { provider: 'inceptron', label: 'Inceptron' },
  { provider: 'akashml', label: 'AkashML' },
  { provider: 'deepinfra', label: 'DeepInfra' },
  { provider: 'chutes', label: 'Chutes' },
  { provider: 'parasail', label: 'Parasail' },
];

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
    vi.mocked(listOpenRouterProviderOptions).mockResolvedValue({
      provider_model_id: 'minimax/minimax-m2.5',
      providers: discoveredOpenRouterProviderOptions,
    });
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
    expect(screen.getByText('Default featherless pool')).toBeInTheDocument();
    expect(screen.queryByText('Effective')).not.toBeInTheDocument();
  });

  it('updates provider route target', async () => {
    vi.mocked(listProviderRoutes).mockResolvedValue({
      provider_options: providerOptions,
      openrouter_provider_options: openRouterProviderOptions,
      routes: [route],
    });
    vi.mocked(listProviderKeys).mockImplementation(async (provider?: string) => ({
      provider: provider ?? null,
      keys:
        provider === 'openrouter'
          ? [
            {
              id: 'key-1',
              provider: 'openrouter',
              key_prefix: 'sk-or...1234',
              label: 'staging',
              source: 'db',
              status: 'active',
              created_at: null,
            },
          ]
          : [],
    }));
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
      endpoint_id: 'minimax-fast:openrouter[parasail]-api',
      source: 'override',
      updated_by: '127.0.0.1',
    });

    render(<ProviderRoutesTab />);

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Override provider'), {
      target: { value: 'openrouter' },
    });
    fireEvent.change(screen.getByLabelText('OpenRouter provider'), {
      target: { value: 'parasail' },
    });
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-1' } });
    expect(screen.getByLabelText('Base URL')).toHaveValue('https://openrouter.ai/api/v1');
    fireEvent.click(screen.getByRole('button', { name: 'Verify & Apply' }));

    await waitFor(() => {
      expect(updateProviderRoute).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:featherless-api',
        {
          upstream_provider: 'openrouter',
          openrouter_provider: 'parasail',
          base_url: 'https://openrouter.ai/api/v1',
          api_key_id: 'key-1',
          provider_model_id: 'minimax/minimax-m2.5',
          quota_limit: null,
        },
      );
    });
    expect(await screen.findByText('Parasail')).toBeInTheDocument();
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
    fireEvent.change(screen.getByLabelText('OpenRouter provider'), {
      target: { value: 'parasail' },
    });
    fireEvent.change(screen.getByLabelText('Provider model ID'), {
      target: { value: 'minimax/minimax-m2.5' },
    });
    fireEvent.change(screen.getByLabelText('Local daily quota'), {
      target: { value: '8000' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Verify & Apply' }));

    await waitFor(() => {
      expect(updateProviderRoute).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:chutes-api',
        {
          upstream_provider: 'openrouter',
          openrouter_provider: 'parasail',
          base_url: 'https://openrouter.ai/api/v1',
          api_key_id: null,
          provider_model_id: 'minimax/minimax-m2.5',
          quota_limit: 8000,
        },
      );
    });
  });

  it('restores an override to the config route', async () => {
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

    fireEvent.click(await screen.findByRole('button', { name: 'Restore config' }));

    await waitFor(() => {
      expect(deleteProviderRoute).toHaveBeenCalledWith(
        'minimax-fast',
        'minimax-fast:featherless-api',
      );
    });
    await waitFor(() => {
      expect(screen.getAllByText('Default featherless pool').length).toBeGreaterThan(0);
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
    vi.mocked(listProviderKeys).mockImplementation(async (provider?: string) => ({
      provider: provider ?? null,
      keys:
        provider === 'openrouter'
          ? [
            {
              id: 'key-1',
              provider: 'openrouter',
              key_prefix: 'sk-or...1234',
              label: 'staging',
              source: 'db',
              status: 'active',
              created_at: null,
            },
          ]
          : [],
    }));
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
    const openRouterSelect = screen.getByLabelText('OpenRouter provider');
    await waitFor(() => {
      expect(within(openRouterSelect).getByRole('option', { name: 'Inceptron' }))
        .toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText('OpenRouter provider'), {
      target: { value: 'inceptron' },
    });

    await waitFor(() => {
      expect(listProviderKeys).toHaveBeenCalledWith('openrouter');
    });

    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify & Add' }));

    await waitFor(() => {
      expect(createProviderRouteCandidate).toHaveBeenCalledWith('minimax-fast', {
        route_type: 'on_demand',
        upstream_provider: 'openrouter',
        openrouter_provider: 'inceptron',
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
    expect(within(providerSelect).queryByRole('option', { name: 'Chutes' })).not.toBeInTheDocument();
    expect(
      within(providerSelect).queryByRole('option', { name: 'Featherless' }),
    ).not.toBeInTheDocument();
    expect(
      within(providerSelect).queryByRole('option', { name: 'DeepInfra' }),
    ).not.toBeInTheDocument();
    expect(within(providerSelect).getByRole('option', { name: 'OpenRouter' })).toBeInTheDocument();

    const openRouterSelect = screen.getByLabelText('OpenRouter provider');
    await waitFor(() => {
      expect(within(openRouterSelect).queryByRole('option', { name: 'DeepInfra' }))
        .not.toBeInTheDocument();
    });
    expect(within(openRouterSelect).getByRole('option', { name: 'Inceptron' }))
      .toBeInTheDocument();
    expect(within(openRouterSelect).getByRole('option', { name: 'Chutes' }))
      .toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Route type'), { target: { value: 'quota' } });

    expect(providerSelect).toHaveValue('chutes');
    expect(within(providerSelect).getByRole('option', { name: 'Chutes' })).toBeInTheDocument();
    expect(screen.getByLabelText('Base URL')).toHaveValue('https://llm.chutes.ai/v1');
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
