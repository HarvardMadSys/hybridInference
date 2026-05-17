// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { UserTable } from './UserTable';
import type { UserRow } from './types';

vi.mock('@/lib/api/admin', () => ({
  getUserDetail: vi.fn(),
  listModelVisibility: vi.fn(),
  updateUser: vi.fn(),
}));

import { getUserDetail, listModelVisibility, updateUser as apiUpdateUser } from '@/lib/api/admin';

const baseUser: UserRow = {
  id: 'user-1',
  email: 'user@example.com',
  user_name: 'User',
  role: 'free',
  status: 'active',
  email_verified: true,
  approval_note: null,
  reviewed_at: null,
  reviewed_by: null,
  created_at: '2024-01-01T00:00:00Z',
  last_login_at: null,
  has_key: true,
  key_prefix: 'hyi_123',
  key_status: 'active',
  usage_today_usd: '0',
  usage_month_usd: '0',
  usage_alltime_usd: '0',
};

function renderTable() {
  return render(
    <UserTable
      users={[baseUser]}
      costHistories={{}}
      density="comfortable"
      filterState={{
        status: null,
        search: '',
        sortBy: 'created',
        minCostToday: null,
        minCostMonth: null,
        quotaState: null,
        provider: null,
        activeWithinHours: null,
        anomaly: null,
        view: null,
      }}
      onSortChange={vi.fn()}
      onApprove={vi.fn(async () => undefined)}
      onReject={vi.fn(async () => undefined)}
      onUpdate={vi.fn(async () => undefined)}
      onSuspend={vi.fn(async () => undefined)}
      onResume={vi.fn(async () => undefined)}
      onDelete={vi.fn(async () => undefined)}
      onHardDelete={vi.fn(async () => undefined)}
      onRegenerateKey={vi.fn(async () => undefined)}
    />,
  );
}

describe('UserTable disabled models editing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it('renders disabled models from user detail and saves selection changes', async () => {
    vi.mocked(getUserDetail)
      .mockResolvedValueOnce({
        id: 'user-1',
        email: 'user@example.com',
        user_name: 'User',
        role: 'free',
        status: 'active',
        email_verified: true,
        created_at: '2024-01-01T00:00:00Z',
        last_login_at: null,
        has_key: true,
        key_prefix: 'hyi_123',
        quota_daily_usd: 100,
        quota_monthly_usd: null,
        usage_today_usd: 0,
        usage_today_requests: 0,
        usage_month_usd: 0,
        usage_month_requests: 0,
        models_used: ['gpt-4o-mini'],
        disabled_models: ['claude-3-5-sonnet'],
        last_request_at: null,
        max_concurrent_requests: null,
      })
      .mockResolvedValueOnce({
        id: 'user-1',
        email: 'user@example.com',
        user_name: 'User',
        role: 'free',
        status: 'active',
        email_verified: true,
        created_at: '2024-01-01T00:00:00Z',
        last_login_at: null,
        has_key: true,
        key_prefix: 'hyi_123',
        quota_daily_usd: 100,
        quota_monthly_usd: null,
        usage_today_usd: 0,
        usage_today_requests: 0,
        usage_month_usd: 0,
        usage_month_requests: 0,
        models_used: ['gpt-4o-mini'],
        disabled_models: ['claude-3-5-sonnet', 'gpt-4o-mini'],
        last_request_at: null,
        max_concurrent_requests: null,
      });
    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'claude-3-5-sonnet',
          baseline_required_role: 'pro',
          override_required_role: null,
          effective_required_role: 'pro',
        },
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        },
      ],
    });
    vi.mocked(apiUpdateUser).mockResolvedValue({
      user_id: 'user-1',
      updated_fields: ['disabled_models'],
      message: 'ok',
    });

    renderTable();

    fireEvent.click(screen.getByText('user@example.com'));

    expect(await screen.findByText('Model access')).toBeInTheDocument();
    const modelAccess = screen.getByTestId('disabled-models-panel');
    expect(within(modelAccess).getByLabelText('Enable claude-3-5-sonnet')).not.toBeChecked();
    expect(within(modelAccess).getByLabelText('Disable gpt-4o-mini')).toBeChecked();

    fireEvent.click(within(modelAccess).getByLabelText('Disable gpt-4o-mini'));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(apiUpdateUser).toHaveBeenCalledWith('user-1', {
        disabled_models: ['claude-3-5-sonnet', 'gpt-4o-mini'],
      });
    });
  });
});
