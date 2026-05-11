// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RoutingTab } from './RoutingTab';

vi.mock('@/lib/api/admin', () => ({
  clearRouteWeight: vi.fn(),
  listRouteWeights: vi.fn(),
  setRouteWeight: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

import { clearRouteWeight, listRouteWeights, setRouteWeight } from '@/lib/api/admin';

describe('RoutingTab', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads and renders route weights grouped by model', async () => {
    vi.mocked(listRouteWeights).mockResolvedValue([
      {
        model_id: 'gpt-4o-mini',
        endpoint_id: 'gpt-4o-mini:local',
        provider: 'local',
        base_url: 'http://localhost:8000',
        yaml_weight: 1,
        override_weight: null,
        effective_weight: 1,
      },
    ]);

    render(<RoutingTab />);

    expect(screen.getByText('Loading routing weights...')).toBeInTheDocument();
    expect(await screen.findByText('gpt-4o-mini')).toBeInTheDocument();
    expect(screen.getByText('gpt-4o-mini:local')).toBeInTheDocument();
  });

  it('saves an edited route weight with an explicit save button', async () => {
    vi.mocked(listRouteWeights).mockResolvedValue([
      {
        model_id: 'gpt-4o-mini',
        endpoint_id: 'gpt-4o-mini:remote',
        provider: 'remote',
        base_url: 'https://api.example.test',
        yaml_weight: 2,
        override_weight: null,
        effective_weight: 2,
      },
    ]);
    vi.mocked(setRouteWeight).mockResolvedValue({
      model_id: 'gpt-4o-mini',
      endpoint_id: 'gpt-4o-mini:remote',
      provider: 'remote',
      base_url: 'https://api.example.test',
      yaml_weight: 2,
      override_weight: 4,
      effective_weight: 4,
    });

    render(<RoutingTab />);

    const input = await screen.findByLabelText('Runtime weight for gpt-4o-mini:remote');
    fireEvent.change(input, { target: { value: '4' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save gpt-4o-mini:remote weight' }));

    await waitFor(() => {
      expect(setRouteWeight).toHaveBeenCalledWith('gpt-4o-mini', 'gpt-4o-mini:remote', 4);
    });
    expect(await screen.findByText('Override active')).toBeInTheDocument();
  });

  it('clears an active route weight override', async () => {
    vi.mocked(listRouteWeights).mockResolvedValue([
      {
        model_id: 'gpt-4o-mini',
        endpoint_id: 'gpt-4o-mini:remote',
        provider: 'remote',
        base_url: 'https://api.example.test',
        yaml_weight: 2,
        override_weight: 4,
        effective_weight: 4,
      },
    ]);
    vi.mocked(clearRouteWeight).mockResolvedValue({
      model_id: 'gpt-4o-mini',
      endpoint_id: 'gpt-4o-mini:remote',
      provider: 'remote',
      base_url: 'https://api.example.test',
      yaml_weight: 2,
      override_weight: null,
      effective_weight: 2,
    });

    render(<RoutingTab />);

    const clearButton = await screen.findByRole('button', {
      name: 'Clear gpt-4o-mini:remote override',
    });
    fireEvent.click(clearButton);

    await waitFor(() => {
      expect(clearRouteWeight).toHaveBeenCalledWith('gpt-4o-mini', 'gpt-4o-mini:remote');
    });
    expect(screen.queryByText('Override active')).not.toBeInTheDocument();
  });
});
