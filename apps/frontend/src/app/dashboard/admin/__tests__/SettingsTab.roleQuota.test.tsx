// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { SettingsTab } from '../SettingsTab';

vi.mock('@/lib/api/admin', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api/admin')>('@/lib/api/admin');
  return {
    ...actual,
    listRuntimeSettings: vi.fn(async () => ({
      settings: [
        {
          key: 'user_daily_quota_pro',
          value: 250,
          value_type: 'float',
          default_value: 100,
          description: 'pro quota',
          min: 0,
          max: null,
        },
      ],
    })),
    listSignupAllowedDomains: vi.fn(async () => ({ domains: [] })),
    previewRoleQuotaApply: vi.fn(async () => ({
      role: 'pro',
      quota: 250,
      keys_affected: 42,
      users_affected: 38,
    })),
    applyRoleQuota: vi.fn(async () => ({ role: 'pro', quota: 250, keys_updated: 42 })),
    updateRuntimeSetting: vi.fn(),
  };
});

describe('SettingsTab role quota', () => {
  it('renders Apply button only on quota settings and runs the confirm flow', async () => {
    const api = await import('@/lib/api/admin');
    render(<SettingsTab />);

    const applyBtn = await screen.findByRole('button', { name: /apply to existing users/i });
    fireEvent.click(applyBtn);
    await waitFor(() => expect(api.previewRoleQuotaApply).toHaveBeenCalledWith('pro'));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('42');
    expect(dialog).toHaveTextContent(/pro/i);

    fireEvent.click(screen.getByRole('button', { name: /^confirm$/i }));
    await waitFor(() => expect(api.applyRoleQuota).toHaveBeenCalledWith('pro'));
  });
});
