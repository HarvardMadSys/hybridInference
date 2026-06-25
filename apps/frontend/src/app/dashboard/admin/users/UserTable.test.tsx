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
import type { UserDetail } from '@/lib/api/admin';

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
  signup_reason: null,
  admin_note: null,
  created_at: '2024-01-01T00:00:00Z',
  last_login_at: null,
  has_key: true,
  key_prefix: 'hyi_123',
  key_status: 'active',
  usage_today_usd: '0',
  usage_month_usd: '0',
  usage_alltime_usd: '0',
};

function renderTable(
  user: UserRow = baseUser,
  overrides: Partial<Parameters<typeof UserTable>[0]> = {},
) {
  return render(
    <UserTable
      users={[user]}
      costHistories={{}}
      turnAverages={{}}
      automationScores={{}}
      scoreState="idle"
      scoreSortDir={null}
      onScoreHeader={vi.fn()}
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
      {...overrides}
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
        admin_note: null,
        avg_turns: null,
        avg_user_turns: null,
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
        admin_note: null,
        avg_turns: null,
        avg_user_turns: null,
      });
    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'claude-3-5-sonnet',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
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

  it('renders models above the user role as role-locked and does not toggle them', async () => {
    const proUser: UserRow = { ...baseUser, role: 'pro' };
    vi.mocked(getUserDetail).mockResolvedValue({
      id: 'user-1',
      email: 'user@example.com',
      user_name: 'User',
      role: 'pro',
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
      models_used: [],
      disabled_models: [],
      last_request_at: null,
      max_concurrent_requests: null,
      admin_note: null,
      avg_turns: null,
      avg_user_turns: null,
    });
    vi.mocked(listModelVisibility).mockResolvedValue({
      models: [
        {
          model_id: 'deepseek-v4-flash',
          baseline_required_role: 'internal',
          override_required_role: null,
          effective_required_role: 'internal',
        },
        {
          model_id: 'gpt-4o-mini',
          baseline_required_role: 'free',
          override_required_role: null,
          effective_required_role: 'free',
        },
      ],
    });

    renderTable(proUser);

    fireEvent.click(screen.getByText('user@example.com'));

    expect(await screen.findByText('Model access')).toBeInTheDocument();
    const modelAccess = screen.getByTestId('disabled-models-panel');

    // Role-locked model: disabled checkbox, requires-internal badge.
    const lockedCheckbox = within(modelAccess).getByLabelText(
      'deepseek-v4-flash requires internal role',
    );
    expect(lockedCheckbox).toBeDisabled();
    expect(lockedCheckbox).not.toBeChecked();
    expect(within(modelAccess).getByText('requires internal')).toBeInTheDocument();

    // Reachable model still togglable.
    expect(within(modelAccess).getByLabelText('Disable gpt-4o-mini')).toBeChecked();

    // Clicking the locked checkbox is a no-op: it must not add the model to the
    // denylist. With no other change, saving sends no disabled_models patch at all.
    fireEvent.click(lockedCheckbox);
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    // Toggle a reachable model afterward to force a save call we can inspect,
    // proving the locked model never entered the denylist.
    fireEvent.click(within(modelAccess).getByLabelText('Disable gpt-4o-mini'));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => {
      expect(apiUpdateUser).toHaveBeenCalled();
    });
    for (const call of vi.mocked(apiUpdateUser).mock.calls) {
      const patch = call[1] as { disabled_models?: string[] };
      expect(patch.disabled_models ?? []).not.toContain('deepseek-v4-flash');
    }
  });

  it('shows the signup reason for a pending user when expanded', async () => {
    const pendingUser: UserRow = {
      ...baseUser,
      status: 'pending_approval',
      has_key: false,
      signup_reason: 'Building a course assistant for CS50.',
    };
    vi.mocked(getUserDetail).mockResolvedValue({
      id: 'user-1',
      email: 'user@example.com',
      user_name: 'User',
      role: 'free',
      status: 'pending_approval',
      email_verified: true,
      created_at: '2024-01-01T00:00:00Z',
      last_login_at: null,
      has_key: false,
      key_prefix: null,
      quota_daily_usd: null,
      quota_monthly_usd: null,
      usage_today_usd: 0,
      usage_today_requests: 0,
      usage_month_usd: 0,
      usage_month_requests: 0,
      models_used: [],
      disabled_models: [],
      last_request_at: null,
      max_concurrent_requests: null,
      admin_note: null,
      avg_turns: null,
      avg_user_turns: null,
    });
    vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });

    renderTable(pendingUser);

    fireEvent.click(screen.getByText('user@example.com'));

    expect(await screen.findByText('Signup reason')).toBeInTheDocument();
    expect(screen.getByText('Building a course assistant for CS50.')).toBeInTheDocument();
  });
});

function detailFixture(overrides: Partial<UserDetail> = {}): UserDetail {
  return {
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
    models_used: [],
    disabled_models: [],
    last_request_at: null,
    max_concurrent_requests: null,
    admin_note: null,
    avg_turns: null,
    avg_user_turns: null,
    ...overrides,
  };
}

describe('UserTable admin note', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });
    vi.mocked(apiUpdateUser).mockResolvedValue({
      user_id: 'user-1',
      updated_fields: ['admin_note'],
      message: 'ok',
    });
  });

  afterEach(() => {
    cleanup();
  });

  it('saves a trimmed admin note via onUpdate and refreshes the detail', async () => {
    vi.mocked(getUserDetail)
      .mockResolvedValueOnce(detailFixture({ admin_note: null }))
      .mockResolvedValueOnce(detailFixture({ admin_note: 'VIP customer' }));
    const onUpdate = vi.fn(async () => undefined);

    renderTable(baseUser, { onUpdate });

    fireEvent.click(screen.getByText('user@example.com'));

    const textarea = await screen.findByLabelText('Admin note');
    // Save is disabled until the note actually changes.
    const saveBtn = screen.getByRole('button', { name: 'Save note' });
    expect(saveBtn).toBeDisabled();

    fireEvent.change(textarea, { target: { value: '  VIP customer  ' } });
    expect(saveBtn).not.toBeDisabled();
    fireEvent.click(saveBtn);

    await waitFor(() => {
      expect(onUpdate).toHaveBeenCalledWith('user-1', { admin_note: 'VIP customer' });
    });
    // Detail is refetched so the panel reflects the persisted value.
    expect(getUserDetail).toHaveBeenCalledTimes(2);
  });

  it('clears an existing note by saving an empty value', async () => {
    vi.mocked(getUserDetail)
      .mockResolvedValueOnce(detailFixture({ admin_note: 'old note' }))
      .mockResolvedValueOnce(detailFixture({ admin_note: null }));
    const onUpdate = vi.fn(async () => undefined);

    renderTable(baseUser, { onUpdate });

    fireEvent.click(screen.getByText('user@example.com'));

    const textarea = await screen.findByLabelText('Admin note');
    expect(textarea).toHaveValue('old note');

    fireEvent.change(textarea, { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save note' }));

    await waitFor(() => {
      expect(onUpdate).toHaveBeenCalledWith('user-1', { admin_note: null });
    });
  });

  it('shows a note indicator in the table row when a user has an admin note', () => {
    renderTable({ ...baseUser, admin_note: 'keep an eye on this one' });
    const indicator = screen.getByLabelText('Has admin note');
    expect(indicator).toBeInTheDocument();
    expect(indicator).toHaveAttribute('title', 'Admin note: keep an eye on this one');
  });

  it('omits the note indicator when a user has no admin note', () => {
    renderTable({ ...baseUser, admin_note: null });
    expect(screen.queryByLabelText('Has admin note')).not.toBeInTheDocument();
  });

  it('lets an admin note be edited for a non-active (suspended) user', async () => {
    const suspendedUser: UserRow = { ...baseUser, status: 'suspended' };
    vi.mocked(getUserDetail)
      .mockResolvedValueOnce(detailFixture({ status: 'suspended', admin_note: null }))
      .mockResolvedValueOnce(detailFixture({ status: 'suspended', admin_note: 'spam risk' }));
    const onUpdate = vi.fn(async () => undefined);

    renderTable(suspendedUser, { onUpdate });

    fireEvent.click(screen.getByText('user@example.com'));

    const textarea = await screen.findByLabelText('Admin note');
    fireEvent.change(textarea, { target: { value: 'spam risk' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save note' }));

    await waitFor(() => {
      expect(onUpdate).toHaveBeenCalledWith('user-1', { admin_note: 'spam risk' });
    });
  });
});
