// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SettingsTab } from '../SettingsTab';

const refreshMock = vi.fn();

vi.mock('next/navigation', () => ({
  usePathname: () => '/dashboard/admin/settings',
  useRouter: () => ({ replace: vi.fn(), refresh: refreshMock }),
}));

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    // SettingsTab's other sections read these on mount; unmocked they would
    // reach the real gateway (see vitest.setup.ts).
    listModelVisibility: vi.fn(async () => ({ models: [] })),
    listModelConcurrency: vi.fn(async () => ({ models: [] })),
    listBatchModelLimits: vi.fn(async () => ({ default_concurrency: 4, models: [] })),
    listRoutewiseSettings: vi.fn(async () => ({ settings: [] })),
    listRouteWeights: vi.fn(async () => []),
    getAlertSnooze: vi.fn(async () => ({
      snoozed: false,
      snooze_until: null,
      seconds_remaining: 0,
    })),
    listAlertMutes: vi.fn(async () => ({ types: [] })),
    getUsageInsightsSettings: vi.fn(async () => ({
      configured: false,
      api_key_hint: null,
      model: 'gpt-4o-mini',
    })),
    listSignupAllowedDomains: vi.fn(async () => ({ domains: [] })),
    listRuntimeSettings: vi.fn(async () => ({
      settings: [
        {
          key: 'signup_require_email_verification',
          value: true,
          value_type: 'bool',
          default_value: true,
          description: 'Require email verification for new signups',
        },
        {
          key: 'max_concurrent_requests_per_user',
          value: 3,
          value_type: 'int',
          default_value: 3,
          description: 'Concurrent requests per user',
          min: 1,
          max: null,
        },
      ],
    })),
    updateRuntimeSetting: vi.fn(),
    addSignupAllowedDomain: vi.fn(),
    removeSignupAllowedDomain: vi.fn(),
  };
});

import {
  addSignupAllowedDomain,
  listSignupAllowedDomains,
  removeSignupAllowedDomain,
  updateRuntimeSetting,
} from '@/lib/api/admin';

function renderSettings() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <SettingsTab />
    </QueryClientProvider>,
  );
}

/** Press the confirm button of the dialog a change opens. */
function confirm(label: string) {
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: label }));
}

beforeEach(() => {
  // No answer queued by an earlier test.
  vi.mocked(updateRuntimeSetting).mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function saveSection(name: string) {
  fireEvent.click(screen.getByRole('button', { name: `Save changes to ${name}` }));
}

// The "required settings are missing" banner reads the server-rendered site
// configuration, and email verification decides whether SMTP is required.
describe('SettingsTab and the site configuration', () => {
  it('re-reads it after a flag changes', async () => {
    vi.mocked(updateRuntimeSetting).mockResolvedValueOnce({
      key: 'signup_require_email_verification',
      value: false,
      value_type: 'bool',
      default_value: true,
      description: 'Require email verification for new signups',
    });
    renderSettings();

    fireEvent.click(
      await screen.findByRole('switch', { name: 'Toggle signup require email verification' }),
    );
    saveSection('Feature Flags');
    expect(
      screen.getByRole('dialog', { name: 'Save 1 change to Feature Flags?' }),
    ).toHaveTextContent('Signup Require Email VerificationOn → Off');
    confirm('Confirm save');

    await waitFor(() =>
      expect(updateRuntimeSetting).toHaveBeenCalledWith('signup_require_email_verification', false),
    );
    await waitFor(() => expect(refreshMock).toHaveBeenCalledTimes(1));
    expect(updateRuntimeSetting).toHaveBeenCalledTimes(1);
  });

  it('re-reads it after a number is saved', async () => {
    vi.mocked(updateRuntimeSetting).mockResolvedValueOnce({
      key: 'max_concurrent_requests_per_user',
      value: 5,
      value_type: 'int',
      default_value: 3,
      description: 'Concurrent requests per user',
      min: 1,
      max: null,
    });
    renderSettings();

    const input = await screen.findByLabelText('max_concurrent_requests_per_user value');
    fireEvent.change(input, { target: { value: '5' } });
    saveSection('Numeric Settings');
    expect(screen.getByRole('dialog')).toHaveTextContent('3 → 5');
    confirm('Confirm save');

    await waitFor(() =>
      expect(updateRuntimeSetting).toHaveBeenCalledWith('max_concurrent_requests_per_user', 5),
    );
    await waitFor(() => expect(refreshMock).toHaveBeenCalledTimes(1));
  });

  it('edits saved values in place, and changes nothing until the change is confirmed', async () => {
    renderSettings();

    // Editable in place: no Edit step, and no save bar until something changes.
    const flag = await screen.findByRole('switch', {
      name: 'Toggle signup require email verification',
    });
    expect(flag).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByLabelText('max_concurrent_requests_per_user value')).toHaveValue(3);
    expect(screen.queryByRole('button', { name: /^Edit / })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull();

    fireEvent.click(flag);
    saveSection('Feature Flags');
    fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Back to editing' }),
    );
    // Back keeps the draft; Discard drops it.
    expect(flag).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to Feature Flags' }));
    expect(flag).toHaveAttribute('aria-checked', 'true');
    expect(screen.queryByRole('button', { name: 'Save changes to Feature Flags' })).toBeNull();

    const input = screen.getByLabelText('max_concurrent_requests_per_user value');
    fireEvent.change(input, { target: { value: '5' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Back to editing' }),
    );
    expect(screen.getByLabelText('max_concurrent_requests_per_user value')).toHaveValue(5);

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(updateRuntimeSetting).not.toHaveBeenCalled();
  });

  it('refuses an out-of-range number without asking', async () => {
    renderSettings();
    const input = await screen.findByLabelText('max_concurrent_requests_per_user value');
    fireEvent.change(input, { target: { value: '0' } });
    // The bar shows for the invalid draft, with Save held back and the reason.
    expect(screen.getByRole('button', { name: 'Save changes to Numeric Settings' })).toBeDisabled();
    expect(
      screen.getByText('Correct max_concurrent_requests_per_user before saving.'),
    ).toBeVisible();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(updateRuntimeSetting).not.toHaveBeenCalled();
  });

  it('hides the save bar again once a change is undone by hand', async () => {
    renderSettings();
    const flag = await screen.findByRole('switch', {
      name: 'Toggle signup require email verification',
    });
    fireEvent.click(flag);
    expect(screen.getByRole('button', { name: 'Save changes to Feature Flags' })).toBeEnabled();
    fireEvent.click(flag);
    expect(screen.queryByRole('button', { name: 'Save changes to Feature Flags' })).toBeNull();

    const input = screen.getByLabelText('max_concurrent_requests_per_user value');
    fireEvent.change(input, { target: { value: '5' } });
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    fireEvent.change(input, { target: { value: '3' } });
    expect(screen.queryByRole('button', { name: 'Save changes to Numeric Settings' })).toBeNull();
    expect(updateRuntimeSetting).not.toHaveBeenCalled();
  });

  it('leaves it alone when the change is refused, keeping the dialog and the draft', async () => {
    vi.mocked(updateRuntimeSetting).mockRejectedValueOnce(new Error('forbidden'));
    renderSettings();

    fireEvent.click(
      await screen.findByRole('switch', { name: 'Toggle signup require email verification' }),
    );
    saveSection('Feature Flags');
    confirm('Confirm save');

    expect(await within(screen.getByRole('dialog')).findByRole('alert')).toHaveTextContent(
      'signup_require_email_verification could not be saved: forbidden',
    );
    expect(refreshMock).not.toHaveBeenCalled();
    expect(
      screen.getByRole('switch', { name: 'Toggle signup require email verification' }),
    ).toHaveAttribute('aria-checked', 'false');
  });
});

describe('SettingsTab signup allowlist', () => {
  it('adds a domain only through Add domain and a review', async () => {
    vi.mocked(addSignupAllowedDomain).mockResolvedValue(undefined as never);
    renderSettings();

    // No input until Add domain is chosen.
    expect(screen.queryByLabelText('Domain to add')).toBeNull();
    fireEvent.click(await screen.findByRole('button', { name: 'Add domain' }));
    fireEvent.change(screen.getByLabelText('Domain to add'), {
      target: { value: '*.partner.example' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Review and add' }));

    const dialog = screen.getByRole('dialog', {
      name: 'Add *.partner.example to the signup allowlist?',
    });
    expect(dialog).toHaveTextContent('approved automatically');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Back to editing' }));
    expect(screen.getByLabelText('Domain to add')).toHaveValue('*.partner.example');
    expect(addSignupAllowedDomain).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Review and add' }));
    confirm('Add domain');
    await waitFor(() => expect(addSignupAllowedDomain).toHaveBeenCalledWith('*.partner.example'));
    expect(addSignupAllowedDomain).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByLabelText('Domain to add')).toBeNull());
  });

  it('removes a domain only after a confirmation naming it', async () => {
    vi.mocked(listSignupAllowedDomains).mockResolvedValue({
      domains: [{ domain: 'acme.example', is_wildcard: false, created_at: null }],
    } as never);
    vi.mocked(removeSignupAllowedDomain).mockResolvedValue(undefined as never);
    renderSettings();

    fireEvent.click(await screen.findByRole('button', { name: 'Remove acme.example' }));
    const dialog = screen.getByRole('dialog', {
      name: 'Remove acme.example from the signup allowlist?',
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(removeSignupAllowedDomain).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Remove acme.example' }));
    confirm('Remove');
    await waitFor(() =>
      expect(removeSignupAllowedDomain).toHaveBeenCalledWith('acme.example', false),
    );
    expect(removeSignupAllowedDomain).toHaveBeenCalledTimes(1);
  });
});

describe('SettingsTab feedback', () => {
  it('shows a change’s result above the cards, not inside one of them', async () => {
    vi.mocked(updateRuntimeSetting).mockResolvedValueOnce({
      key: 'signup_require_email_verification',
      value: false,
      value_type: 'bool',
      default_value: true,
      description: 'Require email verification for new signups',
    });
    renderSettings();

    // A flag change is reviewed first; its result shows once it is confirmed.
    fireEvent.click(
      await screen.findByRole('switch', { name: 'Toggle signup require email verification' }),
    );
    saveSection('Feature Flags');
    confirm('Confirm save');

    const message = await screen.findByText('signup_require_email_verification disabled');
    const feedback = message.closest('[role="status"]') as HTMLElement;
    expect(feedback).not.toBeNull();
    // It floats over the sections, so it must not swallow clicks meant for them.
    expect(feedback.closest('.pointer-events-none')).not.toBeNull();
    expect(feedback.closest('section')).toBeNull();
    const signupPolicy = screen.getByRole('region', { name: 'Signup Policy' });
    expect(feedback.compareDocumentPosition(signupPolicy)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(
      feedback.compareDocumentPosition(screen.getByRole('region', { name: 'Feature Flags' })),
    ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });
});

describe('SettingsTab section index', () => {
  it('links to every section on the page', async () => {
    renderSettings();
    // Once the settings have loaded.
    await screen.findByLabelText('Toggle signup require email verification');

    const index = screen.getByRole('navigation', { name: 'Settings sections' });
    for (const link of within(index).getAllByRole('link')) {
      const id = link.getAttribute('href')!.slice(1);
      expect(document.getElementById(id), id).not.toBeNull();
    }
    expect(within(index).getAllByRole('link')).toHaveLength(9);
  });
});
