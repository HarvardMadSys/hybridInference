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
import { chooseOption } from '@/test-utils/select';

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

function saveButton() {
  return screen.getByRole('button', { name: 'Save changes to Queue offload' });
}

/** The save bar appears only once something changed. */
function querySaveButton() {
  return screen.queryByRole('button', { name: 'Save changes to Queue offload' });
}

describe('OffloadRoutePanel', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('saves a newly chosen offload route with its wait on Save, with no confirmation', async () => {
    vi.mocked(setOffloadRoute).mockResolvedValue({ model_id: MODEL, offload: storedOffload });
    const { onChange } = renderPanel();

    // Editable in place: no Edit step, and no save bar until something changes.
    expect(screen.queryByRole('button', { name: /^Edit / })).not.toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Offload route' })).toHaveTextContent(
      'Choose a route…',
    );
    expect(screen.getByLabelText('Offload wait seconds')).toHaveValue(5);
    expect(querySaveButton()).not.toBeInTheDocument();

    chooseOption('Offload route', /^reserved/);
    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '4' } });
    expect(saveButton()).toBeEnabled();
    expect(setOffloadRoute).not.toHaveBeenCalled();
    fireEvent.click(saveButton());

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() => expect(setOffloadRoute).toHaveBeenCalledTimes(1));
    expect(setOffloadRoute).toHaveBeenCalledWith(MODEL, reserved.route_id, 4, NO_LIMITS);
    expect(onChange).toHaveBeenCalledWith(MODEL, storedOffload);
    expect(toast.success).toHaveBeenCalledWith('Offload route saved');
    // Nothing is left unsaved, so the bar is gone.
    await waitFor(() => expect(querySaveButton()).not.toBeInTheDocument());
  });

  it('shows the stored route in place and clears it only after confirmation', async () => {
    vi.mocked(clearOffloadRoute).mockResolvedValue({ model_id: MODEL, offload: null });
    const { onChange } = renderPanel({ offload: storedOffload });

    expect(screen.getByRole('combobox', { name: 'Offload route' })).toHaveTextContent(
      'reserved (glm-4.7:reserved-api)',
    );
    expect(screen.getByLabelText('Offload wait seconds')).toHaveValue(4);
    expect(screen.getByLabelText('Engine queue limit')).toHaveValue(null);
    expect(screen.getByLabelText('Engine queue limit')).toHaveAttribute('placeholder', 'Off');
    expect(screen.getByTestId('offload-route-status')).toHaveTextContent('Active');

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

  it('edits the stored values in place, and Discard writes nothing', () => {
    renderPanel({ offload: storedOffload });

    expect(screen.getByRole('combobox', { name: 'Offload route' })).toHaveAttribute(
      'data-value',
      reserved.route_id,
    );
    expect(screen.getByLabelText('Offload wait seconds')).toHaveValue(4);
    // Nothing changed yet, so there is nothing to save.
    expect(querySaveButton()).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '9' } });
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    expect(saveButton()).toBeEnabled();
    // Typed back to the stored value, it is no change.
    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '4' } });
    expect(querySaveButton()).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '9' } });
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Queue offload' }));
    expect(screen.getByLabelText('Offload wait seconds')).toHaveValue(4);
    expect(querySaveButton()).not.toBeInTheDocument();
    expect(setOffloadRoute).not.toHaveBeenCalled();
  });

  it('fills in a new route first, and saves it once a route is chosen', async () => {
    vi.mocked(setOffloadRoute).mockResolvedValue({ model_id: MODEL, offload: storedOffload });
    renderPanel();

    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '7' } });
    expect(screen.getByText('Choose the offload route to save these settings')).toBeVisible();
    expect(saveButton()).toBeDisabled();

    chooseOption('Offload route', /^reserved/);
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    fireEvent.click(saveButton());

    await waitFor(() => {
      expect(setOffloadRoute).toHaveBeenCalledWith(MODEL, reserved.route_id, 7, NO_LIMITS);
    });
  });

  it('reports unsaved changes, an open confirmation or a write in flight to the parent', async () => {
    let finish!: (value: { model_id: string; offload: OffloadRoute }) => void;
    vi.mocked(setOffloadRoute).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const onEditingChange = vi.fn();
    renderPanel({ offload: storedOffload, onEditingChange });
    expect(onEditingChange).toHaveBeenLastCalledWith(false);

    // Typing the stored value is no change.
    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '4' } });
    expect(onEditingChange).toHaveBeenLastCalledWith(false);
    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '9' } });
    expect(onEditingChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Queue offload' }));
    expect(onEditingChange).toHaveBeenLastCalledWith(false);

    fireEvent.click(screen.getByRole('button', { name: 'Clear offload route' }));
    expect(onEditingChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
    expect(onEditingChange).toHaveBeenLastCalledWith(false);

    // A save holds it until the write ends, not only while its drafts are unsaved.
    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '9' } });
    fireEvent.click(saveButton());
    expect(onEditingChange).toHaveBeenLastCalledWith(true);
    finish({ model_id: MODEL, offload: { ...storedOffload, wait_seconds: 9 } });
    await waitFor(() => expect(onEditingChange).toHaveBeenLastCalledWith(false));
  });

  it('holds Clear offload route while a change is unsaved', () => {
    renderPanel({ offload: storedOffload });

    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '9' } });
    const clear = screen.getByRole('button', { name: 'Clear offload route' });
    expect(clear).toBeDisabled();
    expect(clear).toHaveAttribute('title', 'Save or discard the unsaved changes first');

    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Queue offload' }));
    expect(clear).toBeEnabled();
    expect(clearOffloadRoute).not.toHaveBeenCalled();
  });

  it('saves a changed wait for the stored route, keeping its route and limits', async () => {
    vi.mocked(setOffloadRoute).mockResolvedValue({
      model_id: MODEL,
      offload: { ...storedOffload, wait_seconds: 9 },
    });
    const { onChange } = renderPanel({ offload: storedOffload });

    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '9' } });
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    fireEvent.click(saveButton());

    await waitFor(() => expect(querySaveButton()).not.toBeInTheDocument());
    expect(setOffloadRoute).toHaveBeenCalledTimes(1);
    expect(setOffloadRoute).toHaveBeenCalledWith(MODEL, reserved.route_id, 9, NO_LIMITS);
    expect(onChange).toHaveBeenCalledWith(MODEL, { ...storedOffload, wait_seconds: 9 });
  });

  it('holds a second Save, Discard, the fields and Clear while the save is in flight', async () => {
    let finish!: (value: { model_id: string; offload: OffloadRoute }) => void;
    vi.mocked(setOffloadRoute).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    renderPanel({ offload: storedOffload });

    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '9' } });
    fireEvent.click(saveButton());

    expect(saveButton()).toHaveTextContent('Saving…');
    expect(saveButton()).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Discard changes to Queue offload' })).toBeDisabled();
    expect(screen.getByLabelText('Offload wait seconds')).toBeDisabled();
    expect(screen.getByLabelText('Offload wait seconds')).toHaveValue(9);
    expect(screen.getByRole('combobox', { name: 'Offload route' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Clear offload route' })).toBeDisabled();
    fireEvent.click(saveButton());
    expect(setOffloadRoute).toHaveBeenCalledTimes(1);

    finish({ model_id: MODEL, offload: { ...storedOffload, wait_seconds: 9 } });
    await waitFor(() => expect(querySaveButton()).not.toBeInTheDocument());
    expect(setOffloadRoute).toHaveBeenCalledTimes(1);
  });

  it('rejects a wait at zero before sending it', () => {
    renderPanel({ offload: storedOffload });

    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '0' } });
    expect(screen.getByRole('alert')).toHaveTextContent('Must be greater than 0.');
    fireEvent.click(saveButton());
    expect(screen.getByText('Fix the values marked below before saving.')).toBeVisible();
    expect(setOffloadRoute).not.toHaveBeenCalled();
  });

  it('accepts a wait past the queue timeout, for an engine slow to start', async () => {
    vi.mocked(setOffloadRoute).mockResolvedValue({
      model_id: MODEL,
      offload: { ...storedOffload, wait_seconds: 90 },
    });
    renderPanel({ offload: storedOffload });

    fireEvent.change(screen.getByLabelText('Offload wait seconds'), { target: { value: '90' } });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    fireEvent.click(saveButton());

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

    expect(screen.getByLabelText('Engine queue limit')).toHaveValue(null);
    fireEvent.change(screen.getByLabelText('Engine queue limit'), { target: { value: '2' } });
    fireEvent.click(saveButton());

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

    expect(screen.getByLabelText('Engine queue limit')).toHaveValue(3);
    expect(querySaveButton()).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Engine queue limit'), { target: { value: '' } });
    expect(saveButton()).toBeEnabled();
    fireEvent.click(saveButton());

    await waitFor(() => {
      expect(setOffloadRoute).toHaveBeenCalledWith(MODEL, reserved.route_id, 4, NO_LIMITS);
    });
  });

  it.each([
    ['0', 'Must be ≥ 1.'],
    ['1.5', 'Must be a whole number.'],
  ])('rejects an engine queue limit of %s before sending it', (value, error) => {
    renderPanel({ offload: storedOffload });

    fireEvent.change(screen.getByLabelText('Engine queue limit'), { target: { value } });

    expect(screen.getByRole('alert')).toHaveTextContent(`Engine queue limit: ${error}`);
    fireEvent.click(saveButton());
    expect(screen.getByText('Fix the values marked below before saving.')).toBeVisible();
    expect(setOffloadRoute).not.toHaveBeenCalled();
  });

  it('saves a max offload input with the route', async () => {
    vi.mocked(setOffloadRoute).mockResolvedValue({
      model_id: MODEL,
      offload: { ...storedOffload, max_input_tokens: 32000 },
    });
    renderPanel({ offload: { ...storedOffload, engine_queue_limit: 2 } });

    expect(screen.getByLabelText('Max offload input tokens')).toHaveValue(null);
    fireEvent.change(screen.getByLabelText('Max offload input tokens'), {
      target: { value: '32000' },
    });
    fireEvent.click(saveButton());

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

    expect(screen.getByLabelText('Max offload input tokens')).toHaveValue(16000);
    expect(querySaveButton()).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Max offload input tokens'), { target: { value: '' } });
    expect(saveButton()).toBeEnabled();
    fireEvent.click(saveButton());

    await waitFor(() => {
      expect(setOffloadRoute).toHaveBeenCalledWith(MODEL, reserved.route_id, 4, NO_LIMITS);
    });
  });

  it.each([
    ['0', 'Must be ≥ 1.'],
    ['1.5', 'Must be a whole number.'],
  ])('rejects a max offload input of %s before sending it', (value, error) => {
    renderPanel({ offload: storedOffload });

    fireEvent.change(screen.getByLabelText('Max offload input tokens'), { target: { value } });

    expect(screen.getByRole('alert')).toHaveTextContent(`Max offload input: ${error}`);
    fireEvent.click(saveButton());
    expect(screen.getByText('Fix the values marked below before saving.')).toBeVisible();
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
    expect(screen.getByRole('combobox', { name: 'Offload route' })).toBeDisabled();
    expect(screen.getByLabelText('Offload wait seconds')).toBeDisabled();
    expect(querySaveButton()).not.toBeInTheDocument();
  });

  it('offers only clearing when the routing policy cannot use an offload route', () => {
    renderPanel({ offload: { ...storedOffload, active: false }, editable: false });

    expect(screen.queryByLabelText('Offload route')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Offload wait seconds')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Clear offload route' })).toBeEnabled();
  });

  it('keeps the drafts and shows the error after a rejected save, and saves them again', async () => {
    vi.mocked(setOffloadRoute)
      .mockRejectedValueOnce(new Error('model needs fixed routing'))
      .mockResolvedValueOnce({ model_id: MODEL, offload: storedOffload });
    const { onChange } = renderPanel();

    chooseOption('Offload route', /^reserved/);
    fireEvent.click(saveButton());

    expect(await screen.findByRole('alert')).toHaveTextContent('model needs fixed routing');
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole('combobox', { name: 'Offload route' })).toHaveAttribute(
      'data-value',
      reserved.route_id,
    );
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    fireEvent.click(saveButton());
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(MODEL, storedOffload));
    expect(setOffloadRoute).toHaveBeenCalledTimes(2);
    expect(screen.queryByText('model needs fixed routing')).not.toBeInTheDocument();
  });
});
