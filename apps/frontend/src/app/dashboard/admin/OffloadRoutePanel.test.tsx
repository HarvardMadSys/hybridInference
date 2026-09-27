// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { OffloadRoute, ProviderRoute } from '@/lib/api/admin';

import { OffloadRoutePanel } from './OffloadRoutePanel';

vi.mock('@/lib/api/admin', () => ({
  clearOffloadRoute: vi.fn(),
  setOffloadRoute: vi.fn(),
}));

vi.mock('react-hot-toast', () => ({
  default: {
    error: vi.fn(),
    success: vi.fn(),
  },
}));

import toast from 'react-hot-toast';
import { clearOffloadRoute, setOffloadRoute } from '@/lib/api/admin';

const MODEL = 'glm-4.7';

function route(routeId: string, provider: string): ProviderRoute {
  return {
    model_id: MODEL,
    strategy: 'fixed',
    route_id: routeId,
    route_type: 'on_demand',
    provider,
    upstream_provider: provider,
    key_provider: provider,
    base_url: `https://api.${provider}.example/v1`,
    api_key_id: null,
    api_key: {
      id: null,
      provider,
      label: 'Provider default',
      key_prefix: null,
      source: 'default',
    },
    provider_model_id: 'glm-4.7',
    quota_limit: null,
    concurrency_limit: null,
    endpoint_id: routeId,
    yaml_weight: 1,
    effective_weight: 1,
    source: 'yaml',
    updated_at: null,
    updated_by: null,
  };
}

const primary = route('glm-4.7:primary-api', 'primary');
const reserved = route('glm-4.7:reserved-api', 'reserved');

const storedOffload: OffloadRoute = {
  model_id: MODEL,
  route_id: reserved.route_id,
  wait_seconds: 4,
  endpoint_id: reserved.endpoint_id,
  active: true,
  inactive_reason: null,
  updated_at: null,
  updated_by: 'admin@example.com',
};

const queue = { enabled: true, maxWaitSeconds: 30 };

function renderPanel(overrides: Partial<Parameters<typeof OffloadRoutePanel>[0]> = {}) {
  const onChange = vi.fn();
  render(
    <OffloadRoutePanel
      modelId={MODEL}
      routes={[primary, reserved]}
      offload={null}
      queue={queue}
      editable
      routeLabel={(r) => `${r.provider} (${r.endpoint_id})`}
      onChange={onChange}
      {...overrides}
    />,
  );
  return { onChange };
}

describe('OffloadRoutePanel', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('saves a newly chosen offload route with its wait', async () => {
    vi.mocked(setOffloadRoute).mockResolvedValue({ model_id: MODEL, offload: storedOffload });
    const { onChange } = renderPanel();

    const save = screen.getByRole('button', { name: 'Save offload route' });
    expect(save).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Offload route'), {
      target: { value: reserved.route_id },
    });
    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '4' } });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() => {
      expect(setOffloadRoute).toHaveBeenCalledWith(MODEL, reserved.route_id, 4);
    });
    expect(onChange).toHaveBeenCalledWith(MODEL, storedOffload);
    expect(toast.success).toHaveBeenCalledWith('Offload route saved');
  });

  it('shows the stored route and clears it', async () => {
    vi.mocked(clearOffloadRoute).mockResolvedValue({ model_id: MODEL, offload: null });
    const { onChange } = renderPanel({ offload: storedOffload });

    expect(screen.getByLabelText('Offload route')).toHaveValue(reserved.route_id);
    expect(screen.getByLabelText('Offload wait seconds')).toHaveValue(4);
    expect(screen.getByTestId('offload-route-status')).toHaveTextContent('Active');
    // Nothing changed yet, so there is nothing to save.
    expect(screen.getByRole('button', { name: 'Save offload route' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Clear offload route' }));

    await waitFor(() => {
      expect(clearOffloadRoute).toHaveBeenCalledWith(MODEL);
    });
    expect(onChange).toHaveBeenCalledWith(MODEL, null);
  });

  it('rejects a wait past the queue timeout or at zero before sending it', () => {
    renderPanel({ offload: storedOffload });
    const wait = screen.getByLabelText('Offload wait seconds');

    fireEvent.change(wait, { target: { value: '31' } });
    expect(screen.getByRole('alert')).toHaveTextContent('Must be ≤ 30.');
    expect(screen.getByRole('button', { name: 'Save offload route' })).toBeDisabled();

    fireEvent.change(wait, { target: { value: '0' } });
    expect(screen.getByRole('alert')).toHaveTextContent('Must be greater than 0.');
    expect(screen.getByRole('button', { name: 'Save offload route' })).toBeDisabled();
    expect(setOffloadRoute).not.toHaveBeenCalled();
  });

  it('explains why a stored route is not in force', () => {
    renderPanel({
      offload: {
        ...storedOffload,
        active: false,
        inactive_reason: 'The route no longer exists',
      },
    });

    expect(screen.getByTestId('offload-route-status')).toHaveTextContent('Inactive');
    expect(screen.getByText('The route no longer exists')).toBeInTheDocument();
  });

  it('names the routes that are stalled right now', () => {
    renderPanel({
      offload: {
        ...storedOffload,
        stalled_endpoints: [primary.endpoint_id, 'glm-4.7:gone-api'],
      },
    });

    // Known routes by their label; an endpoint no longer on the route by its id.
    expect(screen.getByTestId('offload-stalled-routes')).toHaveTextContent(
      'Stalled now: primary (glm-4.7:primary-api), glm-4.7:gone-api.',
    );
  });

  it('shows no stall notice when nothing is stalled or the policy is not in force', () => {
    renderPanel({ offload: { ...storedOffload, stalled_endpoints: [] } });
    expect(screen.queryByTestId('offload-stalled-routes')).not.toBeInTheDocument();
    cleanup();

    renderPanel({
      offload: {
        ...storedOffload,
        active: false,
        inactive_reason: 'The route no longer exists',
        stalled_endpoints: [primary.endpoint_id],
      },
    });
    expect(screen.queryByTestId('offload-stalled-routes')).not.toBeInTheDocument();
  });

  it('says when nothing queues because the limiter is off', () => {
    renderPanel({ queue: { enabled: false, maxWaitSeconds: 30 } });

    expect(screen.getByText(/UPSTREAM_CONCURRENCY_ENABLED=false/)).toBeInTheDocument();
  });

  it('needs a second route before one can be reserved', () => {
    renderPanel({ routes: [primary] });

    expect(screen.getByText(/Add another route first/)).toBeInTheDocument();
    expect(screen.getByLabelText('Offload route')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Save offload route' })).toBeDisabled();
  });

  it('offers only clearing when the routing policy cannot use an offload route', () => {
    renderPanel({ offload: { ...storedOffload, active: false }, editable: false });

    expect(screen.queryByLabelText('Offload route')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Clear offload route' })).toBeInTheDocument();
  });

  it('reports a rejected save', async () => {
    vi.mocked(setOffloadRoute).mockRejectedValue(new Error('model needs fixed routing'));
    const { onChange } = renderPanel();

    fireEvent.change(screen.getByLabelText('Offload route'), {
      target: { value: reserved.route_id },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save offload route' }));

    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith(
        'Offload route update failed: model needs fixed routing',
      );
    });
    expect(onChange).not.toHaveBeenCalled();
  });
});
