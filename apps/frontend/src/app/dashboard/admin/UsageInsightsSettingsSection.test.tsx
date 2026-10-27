// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/admin', () => ({
  getUsageInsightsSettings: vi.fn(),
  updateUsageInsightsSettings: vi.fn(),
}));

import { getUsageInsightsSettings, updateUsageInsightsSettings } from '@/lib/api/admin';

import { UsageInsightsSettingsSection } from './UsageInsightsSettingsSection';

const TYPED_KEY = 'sk-typed-new-key-never-shown';
const SAVED = {
  configured: true,
  api_key_hint: 'sk-…abcd',
  model: 'glm-5.1',
} as Awaited<ReturnType<typeof getUsageInsightsSettings>>;

beforeEach(() => {
  vi.mocked(getUsageInsightsSettings).mockResolvedValue(SAVED);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function renderSection(onToast = vi.fn()) {
  render(<UsageInsightsSettingsSection onToast={onToast} />);
  await screen.findByLabelText('Model');
  return onToast;
}

const saveButton = () => screen.queryByRole('button', { name: 'Save changes to Usage Insights' });

describe('UsageInsightsSettingsSection', () => {
  it('edits the model in place, with an empty key field and no save bar until a change', async () => {
    render(<UsageInsightsSettingsSection onToast={vi.fn()} />);
    expect(await screen.findByLabelText('Model')).toHaveValue('glm-5.1');
    // The stored key is never filled in: an empty field leaves it unchanged.
    expect(screen.getByLabelText('Gateway API key')).toHaveValue('');
    expect(screen.getByLabelText('Gateway API key')).toHaveAttribute('type', 'password');
    expect(screen.queryByRole('button', { name: /^Edit / })).toBeNull();
    expect(saveButton()).toBeNull();

    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'glm-6' } });
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    expect(saveButton()).toBeEnabled();
    expect(updateUsageInsightsSettings).not.toHaveBeenCalled();
  });

  it('Discard drops the drafts and writes nothing', async () => {
    await renderSection();
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'glm-6' } });
    fireEvent.change(screen.getByLabelText('Gateway API key'), { target: { value: TYPED_KEY } });
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Usage Insights' }));
    expect(screen.getByLabelText('Model')).toHaveValue('glm-5.1');
    expect(screen.getByLabelText('Gateway API key')).toHaveValue('');
    expect(saveButton()).toBeNull();
    expect(updateUsageInsightsSettings).not.toHaveBeenCalled();
  });

  it('hides the save bar again once the model is typed back', async () => {
    await renderSection();
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'glm-6' } });
    expect(saveButton()).toBeEnabled();
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'glm-5.1' } });
    expect(saveButton()).toBeNull();
  });

  it('saves the model and a typed key once on Save, with no confirmation', async () => {
    vi.mocked(updateUsageInsightsSettings).mockResolvedValue({ ...SAVED, model: 'glm-6' });
    const onToast = await renderSection();
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'glm-6' } });
    fireEvent.change(screen.getByLabelText('Gateway API key'), { target: { value: TYPED_KEY } });
    expect(screen.getByText('2 unsaved changes')).toBeVisible();
    expect(updateUsageInsightsSettings).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Usage Insights' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(saveButton()).toBeNull());
    expect(updateUsageInsightsSettings).toHaveBeenCalledTimes(1);
    expect(updateUsageInsightsSettings).toHaveBeenCalledWith({
      api_key: TYPED_KEY,
      model: 'glm-6',
    });
    expect(onToast).toHaveBeenCalledWith('Usage Insights provider saved');
    // The saved model stays in place; the key field is empty again, and nothing is left to save.
    expect(screen.getByLabelText('Model')).toHaveValue('glm-6');
    expect(screen.getByLabelText('Gateway API key')).toHaveValue('');
    expect(saveButton()).toBeNull();
  });

  it('keeps the drafts and shows the error when the save fails, and Discard clears both', async () => {
    vi.mocked(updateUsageInsightsSettings).mockRejectedValueOnce(new Error('upstream down'));
    await renderSection();
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'glm-6' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Usage Insights' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('upstream down');
    expect(screen.getByLabelText('Model')).toHaveValue('glm-6');
    expect(saveButton()).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Usage Insights' }));
    expect(screen.getByLabelText('Model')).toHaveValue('glm-5.1');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(saveButton()).toBeNull();
  });

  it('holds Save, Discard and the fields while the save is in flight', async () => {
    let finish!: (value: typeof SAVED) => void;
    vi.mocked(updateUsageInsightsSettings).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await renderSection();
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'glm-6' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Usage Insights' }));

    expect(saveButton()).toHaveTextContent('Saving…');
    expect(saveButton()).toBeDisabled();
    expect(
      screen.getByRole('button', { name: 'Discard changes to Usage Insights' }),
    ).toBeDisabled();
    expect(screen.getByLabelText('Model')).toBeDisabled();
    expect(screen.getByLabelText('Gateway API key')).toBeDisabled();
    fireEvent.click(saveButton() as HTMLElement);
    expect(updateUsageInsightsSettings).toHaveBeenCalledTimes(1);

    finish({ ...SAVED, model: 'glm-6' });
    await waitFor(() => expect(saveButton()).toBeNull());
    expect(updateUsageInsightsSettings).toHaveBeenCalledTimes(1);
  });

  it('masks the typed key in a failed save, and keeps it for the retry', async () => {
    vi.mocked(updateUsageInsightsSettings)
      .mockRejectedValueOnce(new Error(`api_key ${TYPED_KEY} was rejected`))
      .mockResolvedValueOnce(SAVED);
    await renderSection();
    fireEvent.change(screen.getByLabelText('Gateway API key'), {
      target: { value: ` ${TYPED_KEY} ` },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Usage Insights' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('api_key •••• was rejected');
    expect(document.body.textContent).not.toContain(TYPED_KEY);

    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Usage Insights' }));
    await waitFor(() => expect(saveButton()).toBeNull());
    expect(screen.queryByRole('alert')).toBeNull();
    expect(vi.mocked(updateUsageInsightsSettings).mock.calls).toEqual([
      [{ api_key: TYPED_KEY }],
      [{ api_key: TYPED_KEY }],
    ]);
  });

  it('refuses an empty model without writing', async () => {
    await renderSection();
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: '  ' } });
    // An emptied model alone still shows the bar, with Save held back and the reason.
    expect(screen.getByText('Model name cannot be empty.')).toBeVisible();
    expect(saveButton()).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Gateway API key'), { target: { value: TYPED_KEY } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to Usage Insights' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText('Model name cannot be empty.')).toBeVisible();
    expect(updateUsageInsightsSettings).not.toHaveBeenCalled();
  });

  it('clears the key only after a confirmation', async () => {
    vi.mocked(updateUsageInsightsSettings).mockResolvedValue({
      ...SAVED,
      configured: false,
      api_key_hint: null,
    } as unknown as typeof SAVED);
    render(<UsageInsightsSettingsSection onToast={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Clear key' }));
    const dialog = screen.getByRole('dialog', { name: 'Clear the Usage Insights API key?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(updateUsageInsightsSettings).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Clear key' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Clear key' }));
    // Clearing is not a save: the save bar stays away while it runs.
    expect(saveButton()).toBeNull();
    await waitFor(() => expect(updateUsageInsightsSettings).toHaveBeenCalledWith({ api_key: '' }));
    expect(updateUsageInsightsSettings).toHaveBeenCalledTimes(1);
  });

  it('holds Clear key back while there are unsaved changes', async () => {
    await renderSection();
    fireEvent.change(screen.getByLabelText('Gateway API key'), { target: { value: TYPED_KEY } });
    expect(screen.getByRole('button', { name: 'Clear key' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Usage Insights' }));
    expect(screen.getByRole('button', { name: 'Clear key' })).toBeEnabled();
    expect(updateUsageInsightsSettings).not.toHaveBeenCalled();
  });
});
