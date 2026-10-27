// @vitest-environment jsdom
import { chooseOption } from '@/test-utils/select';
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import toast from 'react-hot-toast';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { UserTable } from './UserTable';
import type { UserRow } from './types';

vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }));
vi.mock('@/lib/api/admin', () => ({
  getUserDetail: vi.fn(),
  listModelVisibility: vi.fn(),
  updateUser: vi.fn(),
}));

import { getUserDetail, listModelVisibility, updateUser as apiUpdateUser } from '@/lib/api/admin';
import type { UpdateUserData, UserDetail } from '@/lib/api/admin';

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
  usage_alltime_requests: 0,
  usage_alltime_tokens: 0,
};

function tableProps(
  user: UserRow = baseUser,
  overrides: Partial<Parameters<typeof UserTable>[0]> = {},
): Parameters<typeof UserTable>[0] {
  return {
    users: [user],
    costHistories: {},
    turnAverages: {},
    askQuestionFractions: {},
    automationScores: {},
    scoreState: 'idle',
    scoreSortDir: null,
    onScoreHeader: vi.fn(),
    density: 'comfortable',
    filterState: {
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
    },
    onSortChange: vi.fn(),
    onApprove: vi.fn(async () => undefined),
    onReject: vi.fn(async () => undefined),
    onUpdate: vi.fn(async () => undefined),
    onSuspend: vi.fn(async () => undefined),
    onResume: vi.fn(async () => undefined),
    onDelete: vi.fn(async () => undefined),
    onHardDelete: vi.fn(async () => undefined),
    ...overrides,
  };
}

function renderTable(
  user: UserRow = baseUser,
  overrides: Partial<Parameters<typeof UserTable>[0]> = {},
) {
  return render(<UserTable {...tableProps(user, overrides)} />);
}

function dialog() {
  return screen.getByRole('dialog');
}

function clickInDialog(name: string | RegExp) {
  fireEvent.click(within(dialog()).getByRole('button', { name }));
}

async function expand(label = 'user@example.com') {
  fireEvent.click(screen.getByText(label));
  await screen.findByRole('textbox', { name: 'Admin note' });
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
        suspension_message: null,
        avg_turns: null,
        avg_user_turns: null,
        ask_question_fraction: null,
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
        suspension_message: null,
        avg_turns: null,
        avg_user_turns: null,
        ask_question_fraction: null,
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

    const onUpdate = vi.fn(async () => undefined);
    renderTable(baseUser, { onUpdate });

    fireEvent.click(screen.getByText('user@example.com'));

    // Editable in place: the checkboxes show the saved denylist, with no Edit
    // step and no save bar until something changes.
    const modelAccess = await screen.findByTestId('disabled-models-panel');
    expect(screen.getByText('Model access')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Edit / })).toBeNull();
    expect(within(modelAccess).getByLabelText('Enable claude-3-5-sonnet')).not.toBeChecked();
    expect(within(modelAccess).getByLabelText('Disable gpt-4o-mini')).toBeChecked();
    expect(screen.queryByRole('button', { name: 'Save changes to account settings' })).toBeNull();

    fireEvent.click(within(modelAccess).getByLabelText('Disable gpt-4o-mini'));
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    expect(onUpdate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to account settings' }));

    // Save writes at once: no confirmation.
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Save changes to account settings' })).toBeNull(),
    );
    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(onUpdate).toHaveBeenCalledWith('user-1', {
      disabled_models: ['claude-3-5-sonnet', 'gpt-4o-mini'],
    });
    // The panel shows the refetched, saved values, with no drafts left.
    expect(within(modelAccess).getByLabelText('Enable claude-3-5-sonnet')).not.toBeChecked();
    expect(within(modelAccess).getByLabelText('Enable gpt-4o-mini')).not.toBeChecked();
    expect(screen.queryByRole('button', { name: 'Save changes to account settings' })).toBeNull();
    // The table writes only through onUpdate, never a second direct PATCH.
    expect(apiUpdateUser).not.toHaveBeenCalled();
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
      suspension_message: null,
      avg_turns: null,
      avg_user_turns: null,
      ask_question_fraction: null,
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

    const onUpdate = vi.fn(async () => undefined);
    renderTable(proUser, { onUpdate });

    fireEvent.click(screen.getByText('user@example.com'));

    const modelAccess = await screen.findByTestId('disabled-models-panel');
    expect(screen.getByText('Model access')).toBeInTheDocument();

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
    // denylist. With no other change there is nothing to save, so no save bar.
    fireEvent.click(lockedCheckbox);
    expect(screen.queryByRole('button', { name: 'Save changes to account settings' })).toBeNull();

    // Toggle a reachable model afterward to force a save call we can inspect,
    // proving the locked model never entered the denylist.
    fireEvent.click(within(modelAccess).getByLabelText('Disable gpt-4o-mini'));
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to account settings' }));

    await waitFor(() => {
      expect(onUpdate).toHaveBeenCalled();
    });
    expect(onUpdate).toHaveBeenCalledWith('user-1', { disabled_models: ['gpt-4o-mini'] });
    for (const call of onUpdate.mock.calls as unknown as Array<[string, Record<string, unknown>]>) {
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
      suspension_message: null,
      avg_turns: null,
      avg_user_turns: null,
      ask_question_fraction: null,
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
    suspension_message: null,
    avg_turns: null,
    avg_user_turns: null,
    ask_question_fraction: null,
    ...overrides,
  };
}

describe('UserTable phone-only identity', () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it.each([
    ['+8613812345678', '+8613812345678'],
    [null, 'user-1'],
  ])(
    'requires the displayed account identifier before hard-deleting a user with phone %s',
    async (phone, identifier) => {
      const onHardDelete = vi.fn(async () => undefined);
      vi.mocked(getUserDetail).mockResolvedValueOnce(
        detailFixture({ email: null, phone, status: 'deleted' }),
      );
      vi.mocked(listModelVisibility).mockResolvedValueOnce({ models: [] });
      renderTable({ ...baseUser, email: null, phone, status: 'deleted' }, { onHardDelete });
      fireEvent.click(screen.getByText(identifier));
      fireEvent.click(await screen.findByRole('button', { name: 'Permanently Delete' }));
      expect(dialog()).toHaveAccessibleName(`Permanently delete ${identifier}?`);
      const confirm = within(dialog()).getByRole('button', { name: 'Permanently delete' });
      expect(confirm).toBeDisabled();
      const input = screen.getByLabelText(`Type ${identifier} to confirm`);
      fireEvent.change(input, { target: { value: 'wrong-identity' } });
      expect(confirm).toBeDisabled();
      fireEvent.change(input, { target: { value: identifier } });
      expect(confirm).toBeEnabled();
      fireEvent.click(confirm);
      await waitFor(() => expect(onHardDelete).toHaveBeenCalledWith('user-1', ''));
      expect(onHardDelete).toHaveBeenCalledTimes(1);
    },
  );
});

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

    await expand();
    // Editable in place, with no Edit step.
    const textarea = screen.getByLabelText('Admin note');
    expect(textarea.tagName).toBe('TEXTAREA');
    expect(screen.queryByRole('button', { name: 'Edit admin note' })).toBeNull();
    // No save bar until the note actually changes.
    expect(screen.queryByRole('button', { name: 'Save changes to admin note' })).toBeNull();

    fireEvent.change(textarea, { target: { value: '  VIP customer  ' } });
    const saveBtn = screen.getByRole('button', { name: 'Save changes to admin note' });
    expect(saveBtn).toBeEnabled();
    expect(onUpdate).not.toHaveBeenCalled();
    fireEvent.click(saveBtn);
    expect(screen.queryByRole('dialog')).toBeNull();

    await waitFor(() => {
      expect(onUpdate).toHaveBeenCalledWith('user-1', { admin_note: 'VIP customer' });
    });
    expect(onUpdate).toHaveBeenCalledTimes(1);
    // Detail is refetched so the panel reflects the persisted (trimmed) value.
    expect(getUserDetail).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.getByLabelText('Admin note')).toHaveValue('VIP customer'));
    expect(screen.queryByRole('button', { name: 'Save changes to admin note' })).toBeNull();
  });

  it('shows the save bar only while the trimmed note differs, and Discard restores it', async () => {
    vi.mocked(getUserDetail).mockResolvedValueOnce(detailFixture({ admin_note: 'old note' }));
    const onUpdate = vi.fn(async () => undefined);

    renderTable(baseUser, { onUpdate });

    await expand();
    const textarea = screen.getByLabelText('Admin note');
    expect(textarea).toHaveValue('old note');

    // Surrounding whitespace is trimmed away on save, so it is no change.
    fireEvent.change(textarea, { target: { value: '  old note ' } });
    expect(screen.queryByRole('button', { name: 'Save changes to admin note' })).toBeNull();

    fireEvent.change(textarea, { target: { value: 'new note' } });
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to admin note' }));
    expect(textarea).toHaveValue('old note');
    expect(screen.queryByRole('button', { name: 'Save changes to admin note' })).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(onUpdate).not.toHaveBeenCalled();
  });

  it('clears an existing note by saving an empty value', async () => {
    vi.mocked(getUserDetail)
      .mockResolvedValueOnce(detailFixture({ admin_note: 'old note' }))
      .mockResolvedValueOnce(detailFixture({ admin_note: null }));
    const onUpdate = vi.fn(async () => undefined);

    renderTable(baseUser, { onUpdate });

    await expand();
    const textarea = screen.getByLabelText('Admin note');
    expect(textarea).toHaveValue('old note');

    fireEvent.change(textarea, { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to admin note' }));

    await waitFor(() => {
      expect(onUpdate).toHaveBeenCalledWith('user-1', { admin_note: null });
    });
    expect(onUpdate).toHaveBeenCalledTimes(1);
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

  it('does not duplicate the insight panels after repeated quota saves', async () => {
    // Regression: the automation/insights/recent-requests panels were keyed on
    // the same `u.id`, colliding among siblings. React then duplicated them on
    // each re-render (every quota save refetches the detail), stacking a new
    // copy of each box. Each panel must keep a single instance across saves.
    vi.mocked(getUserDetail)
      .mockResolvedValueOnce(detailFixture({ quota_daily_usd: 100 }))
      .mockResolvedValueOnce(detailFixture({ quota_daily_usd: 200 }))
      .mockResolvedValueOnce(detailFixture({ quota_daily_usd: 300 }))
      .mockResolvedValueOnce(detailFixture({ quota_daily_usd: 400 }));
    vi.mocked(apiUpdateUser).mockResolvedValue({
      user_id: 'user-1',
      updated_fields: ['quota_daily_cost_usd'],
      message: 'ok',
    });

    const onUpdate = vi.fn(async () => undefined);
    renderTable(baseUser, { onUpdate });

    fireEvent.click(screen.getByText('user@example.com'));
    await screen.findByText('Usage insights');

    for (const value of ['200', '300', '400']) {
      // The quota field is the first number input in the settings row.
      const quotaInput = screen.getAllByRole('spinbutton')[0];
      fireEvent.change(quotaInput, { target: { value } });
      fireEvent.click(screen.getByRole('button', { name: 'Save changes to account settings' }));
      await waitFor(() => {
        expect(onUpdate).toHaveBeenCalledWith('user-1', {
          quota_daily_cost_usd: Number(value),
        });
      });
      // The refetched value shows once the save is done and its draft is gone.
      await waitFor(() =>
        expect(
          screen.queryByRole('button', { name: 'Save changes to account settings' }),
        ).toBeNull(),
      );
      expect(screen.getByRole('spinbutton', { name: 'Daily quota (USD)' })).toHaveValue(
        Number(value),
      );
    }
    expect(onUpdate).toHaveBeenCalledTimes(3);

    // Each panel must appear exactly once. Assert on the panels' action
    // buttons, whose accessible names are unique and unambiguous (the table's
    // own "Automation" column header reads "Score ▸" while scoring is idle, so
    // it never collides with the panel here).
    expect(screen.getAllByRole('button', { name: 'Analyze usage' })).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: 'View user turns' })).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: 'Compute automation score' })).toHaveLength(1);
    expect(screen.getAllByText('Usage insights')).toHaveLength(1);
  });

  it('lets an admin note be edited for a non-active (suspended) user', async () => {
    const suspendedUser: UserRow = { ...baseUser, status: 'suspended' };
    vi.mocked(getUserDetail)
      .mockResolvedValueOnce(detailFixture({ status: 'suspended', admin_note: null }))
      .mockResolvedValueOnce(detailFixture({ status: 'suspended', admin_note: 'spam risk' }));
    const onUpdate = vi.fn(async () => undefined);

    renderTable(suspendedUser, { onUpdate });

    await expand();
    fireEvent.change(screen.getByLabelText('Admin note'), { target: { value: 'spam risk' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to admin note' }));

    await waitFor(() => {
      expect(onUpdate).toHaveBeenCalledWith('user-1', { admin_note: 'spam risk' });
    });
  });
});

describe('UserTable ask-question column', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders the ask-question tool fraction as a percentage', () => {
    renderTable(baseUser, {
      askQuestionFractions: {
        'user-1': { ask_question_fraction: 0.5, n_requests: 10, n_ask_requests: 5 },
      },
    });

    expect(screen.getByText('Ask-question')).toBeInTheDocument();
    const cell = screen.getByTitle(
      '5 of 10 opening requests (first 5 turns) offer an ask-question tool',
    );
    expect(cell).toHaveTextContent('50%');
  });
});

describe('UserDetailPanel activity stats gating', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });
  });

  afterEach(() => {
    cleanup();
  });

  it('defers the expensive activity stats to an on-demand button', async () => {
    vi.mocked(getUserDetail)
      // Initial expand: detail comes back without the gated stats.
      .mockResolvedValueOnce(detailFixture())
      // Button click: the opt-in fetch returns the computed stats.
      .mockResolvedValueOnce(
        detailFixture({ avg_turns: 2, avg_user_turns: 1.5, ask_question_fraction: 0.5 }),
      );

    renderTable(baseUser);
    fireEvent.click(screen.getByText('user@example.com'));

    // The panel opens with a button instead of paying for the stats up front.
    const button = await screen.findByRole('button', { name: 'Compute activity stats' });
    // Expand fetched the detail without opting into the expensive stats.
    expect(getUserDetail).toHaveBeenCalledTimes(1);
    expect(getUserDetail).toHaveBeenLastCalledWith('user-1');
    // No computed tile yet — only the table header carries these labels.
    expect(screen.getAllByText('Avg turns')).toHaveLength(1);

    fireEvent.click(button);

    // Clicking opts in, and the computed tiles render.
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: 'Compute activity stats' }),
      ).not.toBeInTheDocument(),
    );
    expect(getUserDetail).toHaveBeenLastCalledWith('user-1', { includeActivityStats: true });
    expect(screen.getAllByText('Avg turns')).toHaveLength(2); // header + computed tile
    expect(screen.getByText('50%')).toBeInTheDocument();
  });
});

describe('UserTable for an account without an email', () => {
  // The first-run administrator: a login name, no email address.
  const setupAdmin: UserRow = {
    ...baseUser,
    id: 'admin-1',
    email: null,
    login_name: 'admin',
    user_name: 'Ops',
    role: 'admin',
    status: 'deleted',
  };

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });
    vi.mocked(getUserDetail).mockResolvedValue(
      detailFixture({
        id: 'admin-1',
        email: null,
        login_name: 'admin',
        user_name: 'Ops',
        role: 'admin',
        status: 'deleted',
      }),
    );
  });

  afterEach(() => {
    cleanup();
  });

  it('names the row by login name and confirms a permanent delete with it', async () => {
    const onHardDelete = vi.fn(async () => undefined);
    renderTable(setupAdmin, { onHardDelete });

    // The identity column: the login name, with the display name under it.
    fireEvent.click(screen.getByTitle('admin'));
    fireEvent.click(await screen.findByRole('button', { name: 'Permanently Delete' }));

    expect(dialog()).toHaveAccessibleName('Permanently delete admin?');
    expect(within(dialog()).getByText(/Type the user's\s+username/)).toBeInTheDocument();
    const confirm = screen.getByRole('textbox', { name: 'Type admin to confirm' });
    const dialogSubmit = within(dialog()).getByRole('button', { name: 'Permanently delete' });
    expect(dialogSubmit).toBeDisabled();

    // The display name is not an identifier, so it does not confirm.
    fireEvent.change(confirm, { target: { value: 'Ops' } });
    expect(dialogSubmit).toBeDisabled();
    fireEvent.change(confirm, { target: { value: 'admin' } });
    expect(dialogSubmit).toBeEnabled();

    fireEvent.click(dialogSubmit);
    await waitFor(() => expect(onHardDelete).toHaveBeenCalledWith('admin-1', ''));
  });
});

describe('UserTable column visibility', () => {
  afterEach(() => {
    cleanup();
  });

  it('drops hidden metric columns and their empty group header', () => {
    renderTable(baseUser, {
      hiddenColumns: ['avgTurns', 'avgUserTurns', 'askQuestion', 'automation'],
    });

    expect(screen.queryByText('Ask-question')).not.toBeInTheDocument();
    expect(screen.queryByText('Avg turns')).not.toBeInTheDocument();
    expect(screen.queryByText('Behavior')).not.toBeInTheDocument();
    // Columns that were not hidden stay.
    expect(screen.getByText('Requests')).toBeInTheDocument();
    expect(screen.getByText('Spend (USD)')).toBeInTheDocument();
  });
});

describe('UserTable account settings: in place, written by Save', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });
    vi.mocked(getUserDetail).mockResolvedValue(
      detailFixture({ role: 'free', quota_daily_usd: 100, max_concurrent_requests: null }),
    );
  });

  afterEach(() => {
    cleanup();
  });

  async function renderAccountSettings(
    onUpdate: (userId: string, patch: UpdateUserData) => Promise<void> = vi.fn(
      async () => undefined,
    ),
  ) {
    renderTable(baseUser, { onUpdate });
    await expand();
    return onUpdate;
  }

  function saveAccount() {
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to account settings' }));
  }

  it('refuses an emptied daily quota instead of saving it as 0', async () => {
    const onUpdate = await renderAccountSettings();
    const quota = screen.getByRole('spinbutton', { name: 'Daily quota (USD)' });
    fireEvent.change(quota, { target: { value: '' } });

    // The change is visible and explained, but Save sends nothing.
    expect(screen.getByText('Enter a daily quota in USD. Use 0 to allow no spend.')).toBeVisible();
    expect(quota).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    saveAccount();
    expect(screen.getByRole('alert')).toHaveTextContent('Correct the daily quota before saving');
    expect(quota).toHaveValue(null);
    expect(onUpdate).not.toHaveBeenCalled();

    // Fixing it clears the error, and Save sends the real value.
    fireEvent.change(quota, { target: { value: '25' } });
    expect(screen.queryByRole('alert')).toBeNull();
    saveAccount();
    await waitFor(() =>
      expect(onUpdate).toHaveBeenCalledExactlyOnceWith('user-1', { quota_daily_cost_usd: 25 }),
    );
  });

  it('refuses a negative daily quota', async () => {
    const onUpdate = await renderAccountSettings();
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Daily quota (USD)' }), {
      target: { value: '-5' },
    });
    expect(screen.getByText('Enter a daily quota of 0 or more.')).toBeVisible();
    saveAccount();
    expect(screen.getByRole('alert')).toHaveTextContent('Correct the daily quota before saving');
    expect(onUpdate).not.toHaveBeenCalled();
  });

  it('still allows a deliberate zero quota', async () => {
    const onUpdate = await renderAccountSettings();
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Daily quota (USD)' }), {
      target: { value: '0' },
    });
    saveAccount();
    await waitFor(() =>
      expect(onUpdate).toHaveBeenCalledWith('user-1', { quota_daily_cost_usd: 0 }),
    );
    expect(onUpdate).toHaveBeenCalledTimes(1);
  });

  it('leaves an unsaved (null) quota out of a save that changes other fields', async () => {
    vi.mocked(getUserDetail).mockResolvedValue(
      detailFixture({ role: 'free', quota_daily_usd: null, max_concurrent_requests: null }),
    );
    const onUpdate = await renderAccountSettings();
    chooseOption('Role', 'pro');
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    saveAccount();
    await waitFor(() =>
      expect(onUpdate).toHaveBeenCalledExactlyOnceWith('user-1', { role: 'pro' }),
    );
  });

  it('edits role, quota and concurrency in place, writing nothing until Save', async () => {
    const onUpdate = await renderAccountSettings();

    // No Edit step, and no save bar until something changes.
    expect(screen.queryByRole('button', { name: /^Edit / })).toBeNull();
    expect(screen.getByRole('combobox', { name: 'Role' })).toHaveTextContent('free');
    expect(screen.getByRole('spinbutton', { name: 'Daily quota (USD)' })).toHaveValue(100);
    const maxConcurrent = screen.getByRole('spinbutton', { name: 'Max concurrent' });
    expect(maxConcurrent).toHaveValue(null);
    expect(maxConcurrent).toHaveAttribute('placeholder', 'Role default');
    expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull();

    chooseOption('Role', 'pro');
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Save changes to account settings' })).toBeEnabled();
    expect(onUpdate).not.toHaveBeenCalled();
  });

  it('Discard drops the drafts and writes nothing', async () => {
    const onUpdate = await renderAccountSettings();
    chooseOption('Role', 'pro');
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: 'Discard changes to account settings' }));
    expect(screen.getByRole('combobox', { name: 'Role' })).toHaveTextContent('free');
    expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull();
    expect(onUpdate).not.toHaveBeenCalled();
  });

  it('treats a value typed back to its saved value as no change', async () => {
    await renderAccountSettings();
    const quota = screen.getByRole('spinbutton', { name: 'Daily quota (USD)' });
    fireEvent.change(quota, { target: { value: '250' } });
    expect(screen.getByText('1 unsaved change')).toBeVisible();
    fireEvent.change(quota, { target: { value: '100' } });
    expect(screen.queryByText('1 unsaved change')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save changes to account settings' })).toBeNull();
  });

  it('lets a refresh show through a note typed back to its saved text', async () => {
    vi.mocked(getUserDetail)
      .mockResolvedValueOnce(detailFixture({ role: 'free', admin_note: null }))
      .mockResolvedValueOnce(detailFixture({ role: 'pro', admin_note: 'Set elsewhere' }));
    await renderAccountSettings();
    const note = screen.getByLabelText('Admin note');
    fireEvent.change(note, { target: { value: 'draft' } });
    fireEvent.change(note, { target: { value: '' } });
    expect(screen.queryByRole('button', { name: 'Save changes to admin note' })).toBeNull();

    chooseOption('Role', 'pro');
    saveAccount();
    await waitFor(() => expect(getUserDetail).toHaveBeenCalledTimes(2));

    await waitFor(() => expect(screen.getByLabelText('Admin note')).toHaveValue('Set elsewhere'));
    expect(screen.queryByRole('button', { name: 'Save changes to admin note' })).toBeNull();
  });

  it('writes every changed field in one patch on Save, with no confirmation', async () => {
    const onUpdate = await renderAccountSettings();
    chooseOption('Role', 'pro');
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Max concurrent' }), {
      target: { value: '4' },
    });
    expect(screen.getByText('2 unsaved changes')).toBeVisible();
    expect(onUpdate).not.toHaveBeenCalled();

    vi.mocked(getUserDetail).mockResolvedValueOnce(
      detailFixture({ role: 'pro', max_concurrent_requests: 4 }),
    );
    saveAccount();
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Save changes/ })).toBeNull());
    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(onUpdate).toHaveBeenCalledWith('user-1', { role: 'pro', max_concurrent_requests: 4 });
    expect(screen.getByRole('combobox', { name: 'Role' })).toHaveTextContent('pro');
    expect(screen.getByRole('spinbutton', { name: 'Max concurrent' })).toHaveValue(4);
  });

  it('keeps the open user while its save runs, and opens another one only after', async () => {
    const other: UserRow = {
      ...baseUser,
      id: 'user-2',
      email: 'other@example.com',
      user_name: 'Other',
    };
    vi.mocked(getUserDetail).mockImplementation(async (userId: string) =>
      userId === 'user-2'
        ? detailFixture({ id: 'user-2', email: 'other@example.com', role: 'pro' })
        : detailFixture({ role: 'free', quota_daily_usd: 100, max_concurrent_requests: null }),
    );
    let finish!: () => void;
    const onUpdate = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    renderTable(baseUser, { onUpdate, users: [baseUser, other] });
    await expand();
    chooseOption('Role', 'pro');
    saveAccount();
    expect(onUpdate).toHaveBeenCalledWith('user-1', { role: 'pro' });

    // The save belongs to the open user: another row waits until it ends, so
    // the saved values never land on a different user's panel.
    fireEvent.click(screen.getByText('other@example.com'));
    expect(getUserDetail).not.toHaveBeenCalledWith('user-2');
    expect(
      screen.getByRole('button', { name: 'Save changes to account settings' }),
    ).toHaveTextContent('Saving…');

    finish();
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Save changes to account settings' })).toBeNull(),
    );
    fireEvent.click(screen.getByText('other@example.com'));
    await waitFor(() => expect(getUserDetail).toHaveBeenCalledWith('user-2'));
    expect(onUpdate).toHaveBeenCalledTimes(1);
  });

  it('holds the fields, Save and Discard while the save is in flight, and writes once', async () => {
    let finish!: () => void;
    const onUpdate = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    await renderAccountSettings(onUpdate);
    chooseOption('Role', 'pro');
    saveAccount();

    const saving = screen.getByRole('button', { name: 'Save changes to account settings' });
    expect(saving).toHaveTextContent('Saving…');
    expect(saving).toBeDisabled();
    expect(
      screen.getByRole('button', { name: 'Discard changes to account settings' }),
    ).toBeDisabled();
    expect(screen.getByRole('combobox', { name: 'Role' })).toBeDisabled();
    // The write flow is shared, but the untouched note does not show a save bar.
    expect(screen.getByRole('textbox', { name: 'Admin note' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Save changes to admin note' })).toBeNull();
    fireEvent.click(saving);
    expect(onUpdate).toHaveBeenCalledTimes(1);

    finish();
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Save changes to account settings' })).toBeNull(),
    );
    expect(onUpdate).toHaveBeenCalledTimes(1);
  });

  it('shows a failed save beside its bar, keeps the drafts, and saves them again', async () => {
    const onUpdate = vi
      .fn<(userId: string, patch: UpdateUserData) => Promise<void>>()
      .mockRejectedValueOnce(new Error('quota must be positive'))
      .mockResolvedValueOnce(undefined);
    await renderAccountSettings(onUpdate);
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Daily quota (USD)' }), {
      target: { value: '250' },
    });
    saveAccount();

    expect(await screen.findByRole('alert')).toHaveTextContent('quota must be positive');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('spinbutton', { name: 'Daily quota (USD)' })).toHaveValue(250);
    expect(screen.getByText('1 unsaved change')).toBeVisible();

    saveAccount();
    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(2));
    expect(onUpdate).toHaveBeenLastCalledWith('user-1', { quota_daily_cost_usd: 250 });
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });
});

describe('UserTable suspension message', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });
  });

  afterEach(() => {
    cleanup();
  });

  it('edits the message in place and saves it on Save', async () => {
    vi.mocked(getUserDetail)
      .mockResolvedValueOnce(detailFixture({ status: 'suspended', suspension_message: 'Spam' }))
      .mockResolvedValueOnce(
        detailFixture({ status: 'suspended', suspension_message: 'Contact support' }),
      );
    const onUpdate = vi.fn(async () => undefined);
    renderTable({ ...baseUser, status: 'suspended' }, { onUpdate });
    await expand();

    const message = screen.getByRole('textbox', { name: 'Suspension message' });
    expect(message).toHaveValue('Spam');
    expect(screen.queryByRole('button', { name: 'Edit suspension message' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save changes to suspension message' })).toBeNull();
    fireEvent.change(message, { target: { value: ' Contact support ' } });
    expect(onUpdate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to suspension message' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(message).toHaveValue('Contact support'));
    expect(onUpdate).toHaveBeenCalledTimes(1);
    expect(onUpdate).toHaveBeenCalledWith('user-1', { suspension_message: 'Contact support' });
    expect(message).toHaveValue('Contact support');
    expect(screen.queryByRole('button', { name: 'Save changes to suspension message' })).toBeNull();
  });

  it('says what an emptied message means, and clears it on Save', async () => {
    vi.mocked(getUserDetail).mockResolvedValue(
      detailFixture({ status: 'suspended', suspension_message: 'Spam' }),
    );
    const onUpdate = vi.fn(async () => undefined);
    renderTable({ ...baseUser, status: 'suspended' }, { onUpdate });
    await expand();

    expect(screen.queryByText(/default suspension notice/)).toBeNull();
    fireEvent.change(screen.getByRole('textbox', { name: 'Suspension message' }), {
      target: { value: '   ' },
    });
    expect(screen.getByText(/default suspension notice/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to suspension message' }));
    await waitFor(() =>
      expect(onUpdate).toHaveBeenCalledWith('user-1', { suspension_message: null }),
    );
  });

  it('shows a failed message save beside its bar, keeps the draft, and saves it again', async () => {
    vi.mocked(getUserDetail).mockResolvedValue(
      detailFixture({ status: 'suspended', suspension_message: 'Spam' }),
    );
    const onUpdate = vi
      .fn<(userId: string, patch: UpdateUserData) => Promise<void>>()
      .mockRejectedValueOnce(new Error('backend unavailable'))
      .mockResolvedValueOnce(undefined);
    renderTable({ ...baseUser, status: 'suspended' }, { onUpdate });
    await expand();

    const message = screen.getByRole('textbox', { name: 'Suspension message' });
    fireEvent.change(message, { target: { value: 'Contact support' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to suspension message' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('backend unavailable');
    expect(message).toHaveValue('Contact support');
    // Only the bar that saved shows the failure.
    expect(screen.queryByRole('button', { name: 'Save changes to admin note' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Save changes to suspension message' }));
    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(2));
    expect(onUpdate).toHaveBeenLastCalledWith('user-1', { suspension_message: 'Contact support' });
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });
});

describe('UserTable account actions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });
  });

  afterEach(() => {
    cleanup();
  });

  it('approves a pending user on the click, without expanding the row', async () => {
    const onApprove = vi.fn(async () => undefined);
    renderTable({ ...baseUser, status: 'pending_approval' }, { onApprove });

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    expect(onApprove).toHaveBeenCalledWith('user-1');
    // Approving from the row does not expand it.
    expect(getUserDetail).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('holds Approve and Reject while an approval runs', async () => {
    let finish!: () => void;
    const onApprove = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    renderTable({ ...baseUser, status: 'pending_approval' }, { onApprove });

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled();
    expect(onApprove).toHaveBeenCalledTimes(1);

    finish();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled());
    expect(screen.getByRole('button', { name: 'Reject' })).toBeEnabled();
    expect(onApprove).toHaveBeenCalledTimes(1);
  });

  it('toasts a failed approval, and approves on the next click', async () => {
    const onApprove = vi
      .fn<(userId: string) => Promise<void>>()
      .mockRejectedValueOnce(new Error('User is not pending approval'))
      .mockResolvedValueOnce(undefined);
    renderTable({ ...baseUser, status: 'pending_approval' }, { onApprove });

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('User is not pending approval'));
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(2));
    expect(toast.error).toHaveBeenCalledTimes(1);
  });

  it('rejects only with a reason', async () => {
    const onReject = vi.fn(async () => undefined);
    renderTable({ ...baseUser, status: 'pending_approval' }, { onReject });

    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
    expect(dialog()).toHaveAccessibleName('Reject user@example.com?');
    clickInDialog('Reject');
    expect(await within(dialog()).findByRole('alert')).toHaveTextContent(
      'Enter a reason for the rejection.',
    );
    expect(onReject).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('Reason for rejection (required)'), {
      target: { value: '  not a student  ' },
    });
    clickInDialog('Reject');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(onReject).toHaveBeenCalledTimes(1);
    expect(onReject).toHaveBeenCalledWith('user-1', 'not a student');
  });

  it('suspends with the optional message after confirming, and Cancel writes nothing', async () => {
    vi.mocked(getUserDetail).mockResolvedValue(detailFixture());
    const onSuspend = vi.fn(async () => undefined);
    renderTable(baseUser, { onSuspend });
    await expand();

    fireEvent.click(screen.getByRole('button', { name: 'Suspend' }));
    expect(dialog()).toHaveAccessibleName('Suspend user@example.com?');
    expect(dialog()).toHaveTextContent('unable to authenticate or use their API key');
    // Safe initial focus: the dismissing button.
    expect(within(dialog()).getByRole('button', { name: 'Cancel' })).toHaveFocus();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(onSuspend).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Suspend' }));
    fireEvent.change(screen.getByLabelText('Message shown to the user at login (optional)'), {
      target: { value: '  Policy violation  ' },
    });
    clickInDialog('Suspend');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(onSuspend).toHaveBeenCalledTimes(1);
    expect(onSuspend).toHaveBeenCalledWith('user-1', 'Policy violation');
    // The panel collapses after a status change.
    expect(screen.queryByRole('textbox', { name: 'Admin note' })).toBeNull();
  });

  it('sends a null suspension message when none is typed', async () => {
    vi.mocked(getUserDetail).mockResolvedValue(detailFixture());
    const onSuspend = vi.fn(async () => undefined);
    renderTable(baseUser, { onSuspend });
    await expand();
    fireEvent.click(screen.getByRole('button', { name: 'Suspend' }));
    clickInDialog('Suspend');
    await waitFor(() => expect(onSuspend).toHaveBeenCalledWith('user-1', null));
  });

  it('reactivates a suspended user on the click, through one status update', async () => {
    vi.mocked(getUserDetail).mockResolvedValue(detailFixture({ status: 'suspended' }));
    const onUpdate = vi.fn(async () => undefined);
    renderTable({ ...baseUser, status: 'suspended' }, { onUpdate });
    await expand();

    fireEvent.click(screen.getByRole('button', { name: 'Reactivate' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
    expect(onUpdate).toHaveBeenCalledWith('user-1', { status: 'active' });
    // The panel collapses after a status change.
    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Admin note' })).toBeNull());
  });

  it('resumes a deleted user on the click, and says beforehand that keys stay revoked', async () => {
    vi.mocked(getUserDetail).mockResolvedValue(detailFixture({ status: 'deleted' }));
    const onResume = vi.fn(async () => undefined);
    renderTable({ ...baseUser, status: 'deleted' }, { onResume });
    await expand();

    expect(screen.getByText(/its API keys stay revoked/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => expect(onResume).toHaveBeenCalledTimes(1));
    expect(onResume).toHaveBeenCalledWith('user-1');
    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Admin note' })).toBeNull());
  });

  it('keeps the panel open and toasts a failed reactivation', async () => {
    vi.mocked(getUserDetail).mockResolvedValue(detailFixture({ status: 'suspended' }));
    const onUpdate = vi
      .fn<(userId: string, patch: UpdateUserData) => Promise<void>>()
      .mockRejectedValueOnce(new Error('backend unavailable'));
    renderTable({ ...baseUser, status: 'suspended' }, { onUpdate });
    await expand();

    fireEvent.click(screen.getByRole('button', { name: 'Reactivate' }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('backend unavailable'));
    expect(screen.getByRole('textbox', { name: 'Admin note' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reactivate' })).toBeEnabled();
  });

  it('deletes only with a reason, keeps the dialog on failure, and retries', async () => {
    vi.mocked(getUserDetail).mockResolvedValue(detailFixture());
    const onDelete = vi
      .fn<(userId: string, reason: string) => Promise<void>>()
      .mockRejectedValueOnce(new Error('backend unavailable'))
      .mockResolvedValueOnce(undefined);
    renderTable(baseUser, { onDelete });
    await expand();

    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(dialog()).toHaveAccessibleName('Delete user@example.com?');
    expect(dialog()).toHaveTextContent('revoke API keys, purge sessions');
    clickInDialog('Delete');
    expect(await within(dialog()).findByRole('alert')).toHaveTextContent(
      'Enter a reason for the deletion.',
    );
    expect(onDelete).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('Reason for deletion (required)'), {
      target: { value: 'spam' },
    });
    clickInDialog('Delete');
    expect(await within(dialog()).findByText('backend unavailable')).toBeInTheDocument();
    expect(onDelete).toHaveBeenCalledTimes(1);
    // The typed reason survives the failure, so the retry sends it again.
    expect(screen.getByLabelText('Reason for deletion (required)')).toHaveValue('spam');

    clickInDialog('Delete');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(onDelete).toHaveBeenCalledTimes(2);
    expect(onDelete).toHaveBeenLastCalledWith('user-1', 'spam');
  });

  it('cancelling a permanent delete writes nothing', async () => {
    vi.mocked(getUserDetail).mockResolvedValue(detailFixture({ status: 'deleted' }));
    const onHardDelete = vi.fn(async () => undefined);
    renderTable({ ...baseUser, status: 'deleted' }, { onHardDelete });
    await expand();

    fireEvent.click(screen.getByRole('button', { name: 'Permanently Delete' }));
    fireEvent.change(screen.getByLabelText('Type user@example.com to confirm'), {
      target: { value: 'user@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Reason (optional, audit trail)'), {
      target: { value: 'GDPR request' },
    });
    clickInDialog('Cancel');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(onHardDelete).not.toHaveBeenCalled();

    // Reopening starts afresh: the typed confirmation is not remembered.
    fireEvent.click(screen.getByRole('button', { name: 'Permanently Delete' }));
    expect(within(dialog()).getByRole('button', { name: 'Permanently delete' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Type user@example.com to confirm'), {
      target: { value: 'user@example.com' },
    });
    fireEvent.change(screen.getByLabelText('Reason (optional, audit trail)'), {
      target: { value: ' GDPR request ' },
    });
    clickInDialog('Permanently delete');
    await waitFor(() => expect(onHardDelete).toHaveBeenCalledWith('user-1', 'GDPR request'));
    expect(onHardDelete).toHaveBeenCalledTimes(1);
  });

  it('refuses to act on a user who has left the list while the dialog was open', async () => {
    const onReject = vi.fn(async () => undefined);
    const pending: UserRow = { ...baseUser, status: 'pending_approval' };
    const { rerender } = render(<UserTable {...tableProps(pending, { onReject })} />);

    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
    fireEvent.change(screen.getByLabelText('Reason for rejection (required)'), {
      target: { value: 'not a student' },
    });
    rerender(<UserTable {...tableProps(pending, { onReject, users: [] })} />);
    clickInDialog('Reject');

    expect(await within(dialog()).findByRole('alert')).toHaveTextContent(
      'user@example.com is no longer in the list.',
    );
    expect(onReject).not.toHaveBeenCalled();
  });
});

describe('UserTable reverted drafts', () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });
  it('follows a refreshed quota after the edit was reverted by hand', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });
    vi.mocked(getUserDetail)
      .mockResolvedValueOnce(detailFixture())
      .mockResolvedValueOnce(detailFixture({ quota_daily_usd: 250, admin_note: 'saved note' }));
    renderTable();
    await expand();
    const quota = screen.getByRole('spinbutton', { name: 'Daily quota (USD)' });
    fireEvent.change(quota, { target: { value: '200' } });
    fireEvent.change(quota, { target: { value: '100' } });
    expect(screen.queryByRole('button', { name: 'Save changes to account settings' })).toBeNull();
    fireEvent.change(screen.getByLabelText('Admin note'), { target: { value: 'saved note' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to admin note' }));
    await waitFor(() => expect(quota).toHaveValue(250));
    expect(screen.queryByRole('button', { name: 'Save changes to account settings' })).toBeNull();
  });
});

describe('UserTable reverted text drafts', () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });
  it.each([
    ['Admin note', 'admin note', 'Suspension message', 'suspension message'],
    ['Suspension message', 'suspension message', 'Admin note', 'admin note'],
  ])(
    'refreshes %s after its trimmed draft is reverted',
    async (label, target, otherLabel, otherTarget) => {
      vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });
      vi.mocked(getUserDetail)
        .mockResolvedValueOnce(
          detailFixture({ status: 'suspended', admin_note: 'old', suspension_message: 'old' }),
        )
        .mockResolvedValueOnce(
          detailFixture({
            status: 'suspended',
            admin_note: 'updated',
            suspension_message: 'updated',
          }),
        );
      renderTable({ ...baseUser, status: 'suspended' });
      await expand();
      const field = screen.getByLabelText(label);
      fireEvent.change(field, { target: { value: 'draft' } });
      fireEvent.change(field, { target: { value: ' old ' } });
      expect(field).toHaveValue(' old ');
      expect(screen.queryByRole('button', { name: `Save changes to ${target}` })).toBeNull();
      fireEvent.change(screen.getByLabelText(otherLabel), { target: { value: 'updated' } });
      fireEvent.click(screen.getByRole('button', { name: `Save changes to ${otherTarget}` }));
      await waitFor(() => expect(field).toHaveValue('updated'));
      expect(screen.queryByRole('button', { name: `Save changes to ${target}` })).toBeNull();
    },
  );
});

describe('UserTable independent account drafts', () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });
  it('keeps other unsaved fields when a quota edit is reverted', async () => {
    vi.mocked(listModelVisibility).mockResolvedValue({ models: [] });
    vi.mocked(getUserDetail).mockResolvedValue(detailFixture());
    const onUpdate = vi.fn(async () => undefined);
    renderTable(baseUser, { onUpdate });
    await expand();
    const quota = screen.getByRole('spinbutton', { name: 'Daily quota (USD)' });
    fireEvent.change(quota, { target: { value: '200' } });
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Max concurrent' }), {
      target: { value: '4' },
    });
    fireEvent.change(quota, { target: { value: '100' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes to account settings' }));
    await waitFor(() =>
      expect(onUpdate).toHaveBeenCalledExactlyOnceWith('user-1', { max_concurrent_requests: 4 }),
    );
  });
});
