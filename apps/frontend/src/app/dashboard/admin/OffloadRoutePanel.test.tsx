// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

const NO_LIMITS = { engineQueueLimit: null, maxInputTokens: null };

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

function edit() {
  fireEvent.click(screen.getByRole('button', { name: 'Edit Queue offload' }));
}

function saveButton() {
  return screen.getByRole('button', { name: 'Save changes to Queue offload' });
}

function confirmSave() {
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm save' }));
}

describe('OffloadRoutePanel', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('saves a newly chosen offload route with its wait after confirmation', async () => {
    vi.mocked(setOffloadRoute).mockResolvedValue({ model_id: MODEL, offload: storedOffload });
    const { onChange } = renderPanel();

    expect(screen.getByLabelText('Offload route')).toHaveTextContent('None');
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    edit();
    expect(saveButton()).toBeDisabled();
    expect(screen.getByText('No changes yet')).toBeVisible();

    fireEvent.change(screen.getByLabelText('Offload route'), {
      target: { value: reserved.route_id },
    });
    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '4' } });
    expect(saveButton()).toBeEnabled();
    fireEvent.click(saveButton());

    const dialog = screen.getByRole('dialog', { name: 'Set up the offload route for glm-4.7?' });
    expect(dialog).toHaveTextContent('Offload routeNone → reserved (glm-4.7:reserved-api)');
    expect(dialog).toHaveTextContent('Wait before offloadingNone → 4s');
    expect(setOffloadRoute).not.toHaveBeenCalled();
    confirmSave();

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(setOffloadRoute).toHaveBeenCalledTimes(1);
    expect(setOffloadRoute).toHaveBeenCalledWith(MODEL, reserved.route_id, 4, NO_LIMITS);
    expect(onChange).toHaveBeenCalledWith(MODEL, storedOffload);
    expect(toast.success).toHaveBeenCalledWith('Offload route saved');
    expect(screen.getByRole('button', { name: 'Edit Queue offload' })).toBeInTheDocument();
  });

  it('shows the stored route read-only and clears it only after confirmation', async () => {
    vi.mocked(clearOffloadRoute).mockResolvedValue({ model_id: MODEL, offload: null });
    const { onChange } = renderPanel({ offload: storedOffload });

    expect(screen.getByLabelText('Offload route')).toHaveTextContent(
      'reserved (glm-4.7:reserved-api)',
    );
    expect(screen.getByLabelText('Offload wait seconds')).toHaveTextContent('4s');
    expect(screen.getByLabelText('Engine queue limit')).toHaveTextContent('Off');
    expect(screen.getByTestId('offload-route-status')).toHaveTextContent('Active');
    expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Clear offload route' }));
    const dialog = screen.getByRole('dialog', { name: 'Clear the offload route for glm-4.7?' });
    expect(dialog).toHaveTextContent('reserved (glm-4.7:reserved-api)');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(clearOffloadRoute).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Clear offload route' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Clear' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(clearOffloadRoute).toHaveBeenCalledTimes(1);
    expect(clearOffloadRoute).toHaveBeenCalledWith(MODEL);
    expect(onChange).toHaveBeenCalledWith(MODEL, null);
    expect(toast.success).toHaveBeenCalledWith('Offload route cleared');
  });

  it('starts every edit from the stored values, and Cancel writes nothing', () => {
    renderPanel({ offload: storedOffload });

    edit();
    expect(screen.getByLabelText('Offload route')).toHaveValue(reserved.route_id);
    expect(screen.getByLabelText('Offload wait seconds')).toHaveValue(4);
    // Nothing changed yet, so there is nothing to save.
    expect(saveButton()).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '9' } });
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    // Typed back to the stored value, it is no change.
    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '4' } });
    expect(saveButton()).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '9' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel editing Queue offload' }));
    expect(screen.getByLabelText('Offload wait seconds')).toHaveTextContent('4s');
    edit();
    expect(screen.getByLabelText('Offload wait seconds')).toHaveValue(4);
    expect(setOffloadRoute).not.toHaveBeenCalled();
  });

  it('keeps the drafts when the administrator goes back to editing', () => {
    renderPanel({ offload: storedOffload });

    edit();
    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '9' } });
    fireEvent.click(saveButton());
    const dialog = screen.getByRole('dialog', {
      name: 'Save 1 change to the glm-4.7 offload route?',
    });
    expect(dialog).toHaveTextContent('Wait before offloading4s → 9s');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Back to editing' }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Offload wait seconds')).toHaveValue(9);
    expect(setOffloadRoute).not.toHaveBeenCalled();
  });

  it('blocks a second Confirm and closing while the save is in flight', async () => {
    let finish!: (value: { model_id: string; offload: OffloadRoute }) => void;
    vi.mocked(setOffloadRoute).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    renderPanel({ offload: storedOffload });

    edit();
    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '9' } });
    fireEvent.click(saveButton());
    confirmSave();

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('button', { name: 'Saving…' })).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: 'Back to editing' })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Saving…' }));
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(setOffloadRoute).toHaveBeenCalledTimes(1);

    finish({ model_id: MODEL, offload: { ...storedOffload, wait_seconds: 9 } });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(setOffloadRoute).toHaveBeenCalledTimes(1);
  });

  it('rejects a wait at zero before sending it', () => {
    renderPanel({ offload: storedOffload });

    edit();
    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '0' } });
    expect(screen.getByRole('alert')).toHaveTextContent('Must be greater than 0.');
    fireEvent.click(saveButton());
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByText('Fix the values marked below before saving.')).toBeVisible();
    expect(setOffloadRoute).not.toHaveBeenCalled();
  });

  it('accepts a wait past the queue timeout, for an engine slow to start', async () => {
    vi.mocked(setOffloadRoute).mockResolvedValue({
      model_id: MODEL,
      offload: { ...storedOffload, wait_seconds: 90 },
    });
    renderPanel({ offload: storedOffload });

    edit();
    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '90' } });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    fireEvent.click(saveButton());
    confirmSave();

    await waitFor(() => {
      expect(setOffloadRoute).toHaveBeenCalledWith(MODEL, reserved.route_id, 90, NO_LIMITS);
    });
  });

  it('saves an engine queue limit with the route', async () => {
    vi.mocked(setOffloadRoute).mockResolvedValue({
      model_id: MODEL,
      offload: { ...storedOffload, engine_queue_limit: 2 },
    });
    renderPanel({ offload: storedOffload });

    edit();
    expect(screen.getByLabelText('Engine queue limit')).toHaveValue(null);
    fireEvent.change(screen.getByLabelText('Engine queue limit'), { target: { value: '2' } });
    fireEvent.click(saveButton());
    expect(screen.getByRole('dialog')).toHaveTextContent('Engine queue limitOff → 2');
    confirmSave();

    await waitFor(() => {
      expect(setOffloadRoute).toHaveBeenCalledWith(MODEL, reserved.route_id, 4, {
        engineQueueLimit: 2,
        maxInputTokens: null,
      });
    });
  });

  it('shows a stored engine queue limit and turns it off when emptied', async () => {
    vi.mocked(setOffloadRoute).mockResolvedValue({ model_id: MODEL, offload: storedOffload });
    renderPanel({ offload: { ...storedOffload, engine_queue_limit: 3 } });

    expect(screen.getByLabelText('Engine queue limit')).toHaveTextContent('3');
    edit();
    expect(screen.getByLabelText('Engine queue limit')).toHaveValue(3);
    expect(saveButton()).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Engine queue limit'), { target: { value: '' } });
    expect(saveButton()).toBeEnabled();
    fireEvent.click(saveButton());
    expect(screen.getByRole('dialog')).toHaveTextContent('Engine queue limit3 → Off');
    confirmSave();

    await waitFor(() => {
      expect(setOffloadRoute).toHaveBeenCalledWith(MODEL, reserved.route_id, 4, NO_LIMITS);
    });
  });

  it.each([
    ['0', 'Must be ≥ 1.'],
    ['1.5', 'Must be a whole number.'],
  ])('rejects an engine queue limit of %s before sending it', (value, error) => {
    renderPanel({ offload: storedOffload });

    edit();
    fireEvent.change(screen.getByLabelText('Engine queue limit'), { target: { value } });

    expect(screen.getByRole('alert')).toHaveTextContent(`Engine queue limit: ${error}`);
    fireEvent.click(saveButton());
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(setOffloadRoute).not.toHaveBeenCalled();
  });

  it('saves a max offload input with the route', async () => {
    vi.mocked(setOffloadRoute).mockResolvedValue({
      model_id: MODEL,
      offload: { ...storedOffload, max_input_tokens: 32000 },
    });
    renderPanel({ offload: { ...storedOffload, engine_queue_limit: 2 } });

    edit();
    expect(screen.getByLabelText('Max offload input tokens')).toHaveValue(null);
    fireEvent.change(screen.getByLabelText('Max offload input tokens'), {
      target: { value: '32000' },
    });
    fireEvent.click(saveButton());
    confirmSave();

    await waitFor(() => {
      expect(setOffloadRoute).toHaveBeenCalledWith(MODEL, reserved.route_id, 4, {
        engineQueueLimit: 2,
        maxInputTokens: 32000,
      });
    });
  });

  it('shows a stored max offload input and lifts it when emptied', async () => {
    vi.mocked(setOffloadRoute).mockResolvedValue({ model_id: MODEL, offload: storedOffload });
    renderPanel({ offload: { ...storedOffload, max_input_tokens: 16000 } });

    expect(screen.getByLabelText('Max offload input tokens')).toHaveTextContent('16000');
    edit();
    expect(screen.getByLabelText('Max offload input tokens')).toHaveValue(16000);
    expect(saveButton()).toBeDisabled();

    fireEvent.change(screen.getByLabelText('Max offload input tokens'), { target: { value: '' } });
    expect(saveButton()).toBeEnabled();
    fireEvent.click(saveButton());
    expect(screen.getByRole('dialog')).toHaveTextContent('Max offload input16000 → No limit');
    confirmSave();

    await waitFor(() => {
      expect(setOffloadRoute).toHaveBeenCalledWith(MODEL, reserved.route_id, 4, NO_LIMITS);
    });
  });

  it.each([
    ['0', 'Must be ≥ 1.'],
    ['1.5', 'Must be a whole number.'],
  ])('rejects a max offload input of %s before sending it', (value, error) => {
    renderPanel({ offload: storedOffload });

    edit();
    fireEvent.change(screen.getByLabelText('Max offload input tokens'), { target: { value } });

    expect(screen.getByRole('alert')).toHaveTextContent(`Max offload input: ${error}`);
    fireEvent.click(saveButton());
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(setOffloadRoute).not.toHaveBeenCalled();
  });

  it('says a request over the max offload input is never offloaded', () => {
    renderPanel({ offload: storedOffload });

    expect(screen.getByText(/never sent to\s+the offload route/)).toBeInTheDocument();
  });

  it('says a held request that cannot be offloaded waits its turn', () => {
    renderPanel({ offload: storedOffload });

    expect(screen.getByText(/one that cannot waits its turn/)).toBeInTheDocument();
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

  it('says each request is judged on its own wait', () => {
    renderPanel({ offload: storedOffload });

    expect(
      screen.getByText(/Each request is judged on its own wait, so the route that kept one/),
    ).toBeInTheDocument();
  });

  it('says when nothing queues because the limiter is off', () => {
    renderPanel({ queue: { enabled: false, maxWaitSeconds: 30 } });

    expect(screen.getByText(/UPSTREAM_CONCURRENCY_ENABLED=false/)).toBeInTheDocument();
  });

  it('needs a second route before one can be reserved', () => {
    renderPanel({ routes: [primary] });

    expect(screen.getByText(/Add another route first/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit Queue offload' })).toBeDisabled();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('offers only clearing when the routing policy cannot use an offload route', () => {
    renderPanel({ offload: { ...storedOffload, active: false }, editable: false });

    expect(screen.queryByLabelText('Offload route')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit Queue offload' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Clear offload route' })).toBeInTheDocument();
  });

  it('keeps the dialog and drafts after a rejected save, and retries from the dialog', async () => {
    vi.mocked(setOffloadRoute)
      .mockRejectedValueOnce(new Error('model needs fixed routing'))
      .mockResolvedValueOnce({ model_id: MODEL, offload: storedOffload });
    const { onChange } = renderPanel();

    edit();
    fireEvent.change(screen.getByLabelText('Offload route'), {
      target: { value: reserved.route_id },
    });
    fireEvent.click(saveButton());
    confirmSave();

    const dialog = screen.getByRole('dialog');
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('model needs fixed routing');
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByLabelText('Offload route')).toHaveValue(reserved.route_id);

    confirmSave();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(setOffloadRoute).toHaveBeenCalledTimes(2);
    expect(onChange).toHaveBeenCalledWith(MODEL, storedOffload);
  });
});
