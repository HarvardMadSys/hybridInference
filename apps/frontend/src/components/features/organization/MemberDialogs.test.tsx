// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import type { MemberKey, Organization, OrganizationMember } from '@/lib/api/organization';

const state = vi.hoisted(() => ({
  setLimit: vi.fn(),
  remove: vi.fn(),
  restore: vi.fn(),
  models: vi.fn(),
  setModels: vi.fn(),
  revokeKey: vi.fn(),
  success: vi.fn(),
}));
vi.mock('@/lib/hooks', () => ({
  useSetMemberMonthlyLimit: () => ({ mutateAsync: state.setLimit, isPending: false }),
  useRemoveOrganizationMember: () => ({ mutateAsync: state.remove, isPending: false }),
  useRestoreOrganizationMember: () => ({ mutateAsync: state.restore, isPending: false }),
  useOrganizationMemberModels: (userId: string) => state.models(userId),
  useSetMemberModels: () => ({ mutateAsync: state.setModels, isPending: false }),
  useRevokeMemberKey: () => ({ mutateAsync: state.revokeKey, isPending: false }),
}));
vi.mock('@/lib/hooks/useAccountToast', () => ({
  useAccountToast: () => ({ success: state.success, error: vi.fn() }),
}));

import { MemberDialogs, type MemberDialog } from './MemberDialogs';

const LEI: OrganizationMember = {
  user_id: 'u-lei',
  email: 'lei.wang@example.edu',
  user_name: 'Lei Wang',
  role: 'member',
  status: 'active',
  removed: false,
  joined_at: '2026-10-09T12:00:00Z',
  requests: 982,
  total_tokens: 3_000_000,
  cost_usd: 24.46,
  spent_month_usd: 71.66,
  monthly_limit_usd: 80,
  monthly_limit_source: 'member',
  own_monthly_limit_usd: 80,
};

const ORGANIZATION = {
  id: 'o-1',
  name: 'Example University',
  monthly_limit_usd: 1000,
  default_member_monthly_limit_usd: 20,
  spent_month_usd: 298.23,
} as Organization;

function open(dialog: MemberDialog, organization: Organization | undefined = ORGANIZATION) {
  const onClose = vi.fn();
  render(<MemberDialogs dialog={dialog} organization={organization} onClose={onClose} />);
  return { dialog: screen.getByRole('dialog'), onClose };
}

/** The limit field. Its label carries the hint too, so it is found by how it starts. */
function field(dialog: HTMLElement, currency = 'USD'): HTMLElement {
  return within(dialog).getByLabelText(new RegExp(`^Monthly limit \\(${currency}\\)`));
}

function type(dialog: HTMLElement, value: string) {
  fireEvent.change(field(dialog), { target: { value } });
}

beforeEach(() => {
  state.setLimit.mockReset().mockResolvedValue(undefined);
  state.remove.mockReset().mockResolvedValue(undefined);
  state.restore.mockReset().mockResolvedValue(undefined);
  state.models.mockReset().mockReturnValue({
    data: {
      available: ['deepseek-v3.2', 'glm-4.6', 'qwen-max'],
      disabled_models: ['glm-4.6'],
      platform_disabled_models: ['big-model'],
    },
    error: null,
    isLoading: false,
    refetch: vi.fn(),
  });
  state.setModels.mockReset().mockResolvedValue(undefined);
  state.revokeKey.mockReset().mockResolvedValue(undefined);
  state.success.mockReset();
});
afterEach(cleanup);

describe('the monthly limit of a member', () => {
  it('opens with the member’s own limit and says what an empty field means', () => {
    const { dialog } = open({ mode: 'limit', member: LEI });

    expect(within(dialog).getByRole('heading', { name: 'Set monthly limit' })).toBeInTheDocument();
    expect(dialog).toHaveTextContent('Lei Wang (lei.wang@example.edu)');
    expect(field(dialog)).toHaveValue('80');
    expect(dialog).toHaveTextContent('Leave empty to use the default of the organization, $20.00.');
  });

  it('opens with the cursor in the amount, which is what the dialog is for', () => {
    const { dialog } = open({ mode: 'limit', member: LEI });

    expect(field(dialog)).toHaveFocus();
  });

  it('opens empty for a member on the default, which is not a limit of its own', () => {
    const { dialog } = open({
      mode: 'limit',
      member: {
        ...LEI,
        monthly_limit_usd: 20,
        monthly_limit_source: 'default',
        own_monthly_limit_usd: null,
      },
    });

    expect(field(dialog)).toHaveValue('');
    expect(dialog).not.toHaveTextContent('platform administrator');
  });

  it('edits the limit of the organization and shows the one of the platform administrator', async () => {
    // The organization asked for 80; the platform administrator allows 10.
    const { dialog } = open({
      mode: 'limit',
      member: {
        ...LEI,
        monthly_limit_usd: 10,
        monthly_limit_source: 'platform',
        own_monthly_limit_usd: 80,
        platform_monthly_limit_usd: 10,
      },
    });

    // The field has the limit that this dialog changes, not the one that applies.
    expect(field(dialog)).toHaveValue('80');
    expect(dialog).toHaveTextContent(
      'The platform administrator set a limit of $10.00 for this account. The lower of the two limits applies.',
    );
    type(dialog, '5');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(state.setLimit).toHaveBeenCalledWith({
        userId: 'u-lei',
        limit: { amount: 5, currency: 'USD' },
      }),
    );
  });

  it('opens empty, and clears nothing, when only the platform administrator set a limit', () => {
    const { dialog, onClose } = open({
      mode: 'limit',
      member: {
        ...LEI,
        monthly_limit_usd: 10,
        monthly_limit_source: 'platform',
        own_monthly_limit_usd: null,
        platform_monthly_limit_usd: 10,
      },
    });

    expect(field(dialog)).toHaveValue('');
    expect(dialog).toHaveTextContent('The platform administrator set a limit of $10.00');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    // An empty field is the limit the organization has for this member: none.
    expect(state.setLimit).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it('does not save before the organization is read, whose limit is the ceiling', () => {
    const onClose = vi.fn();
    render(
      <MemberDialogs
        dialog={{ mode: 'limit', member: LEI }}
        organization={undefined}
        onClose={onClose}
      />,
    );
    const dialog = screen.getByRole('dialog');

    type(dialog, '5000');
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled();
    // Enter in the field is no way around the button.
    fireEvent.submit(field(dialog).closest('form') as HTMLFormElement);

    expect(state.setLimit).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('says so, and offers to try again, when the organization could not be read', () => {
    const onRetry = vi.fn();
    render(
      <MemberDialogs
        dialog={{ mode: 'limit', member: LEI }}
        organization={undefined}
        onRetryOrganization={onRetry}
        onClose={vi.fn()}
      />,
    );
    const dialog = screen.getByRole('dialog');

    expect(within(dialog).getByRole('alert')).toHaveTextContent(
      'The organization could not be read, so the limit cannot be saved.',
    );
    // The button stays off, and now with its reason: the ceiling is not known.
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Retry' }));

    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(state.setLimit).not.toHaveBeenCalled();
  });

  it('says nothing of a failed read while the organization is read, or when the dialog has it', () => {
    const { rerender } = render(
      <MemberDialogs
        dialog={{ mode: 'limit', member: LEI }}
        organization={undefined}
        onClose={vi.fn()}
      />,
    );
    expect(within(screen.getByRole('dialog')).queryByRole('alert')).not.toBeInTheDocument();

    // A later read that failed leaves the organization as it was read before.
    rerender(
      <MemberDialogs
        dialog={{ mode: 'limit', member: LEI }}
        organization={ORGANIZATION}
        onRetryOrganization={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    const dialog = screen.getByRole('dialog');

    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeEnabled();
  });

  it('leaves the ceiling to the gateway when the organization was read in another currency', async () => {
    // 7,000 of one currency is no ceiling for an amount of another.
    const { dialog } = open({ mode: 'limit', member: LEI }, {
      ...ORGANIZATION,
      currency: 'CNY',
      monthly_limit: 7000,
    } as Organization);

    type(dialog, '5000');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(state.setLimit).toHaveBeenCalledWith({
        userId: 'u-lei',
        limit: { amount: 5000, currency: 'USD' },
      }),
    );
  });

  it('saves a new limit in the ledger currency and says so', async () => {
    const { dialog, onClose } = open({ mode: 'limit', member: LEI });

    type(dialog, '95.5');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(state.setLimit).toHaveBeenCalledWith({
        userId: 'u-lei',
        limit: { amount: 95.5, currency: 'USD' },
      }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(state.success).toHaveBeenCalledWith('Monthly limit saved.');
  });

  it('clears the limit when the field is emptied', async () => {
    const { dialog, onClose } = open({ mode: 'limit', member: LEI });

    type(dialog, '');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(state.setLimit).toHaveBeenCalledWith({
        userId: 'u-lei',
        limit: { amount: null, currency: 'USD' },
      }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(state.success).toHaveBeenCalledWith('Monthly limit cleared.');
  });

  it('accepts zero, which refuses every request, as a limit and not as none', async () => {
    const { dialog } = open({ mode: 'limit', member: LEI });

    type(dialog, '0');
    fireEvent.submit(field(dialog).closest('form') as HTMLFormElement);

    await waitFor(() =>
      expect(state.setLimit).toHaveBeenCalledWith({
        userId: 'u-lei',
        limit: { amount: 0, currency: 'USD' },
      }),
    );
  });

  // Number() reads 0x10 as 16 and 1e3 as 1000; neither is what was typed.
  it.each(['-5', 'eighty', '0x10', '1e3', '9.12345'])(
    'refuses %j instead of saving another amount, or no limit',
    (value) => {
      const { dialog, onClose } = open({ mode: 'limit', member: LEI });

      type(dialog, value);
      fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

      expect(state.setLimit).not.toHaveBeenCalled();
      expect(onClose).not.toHaveBeenCalled();
      expect(within(dialog).getByRole('alert')).toHaveTextContent(
        'Enter an amount in USD, such as 5 or 12.50, or leave it empty.',
      );
    },
  );

  it('refuses a limit above the limit of the organization', () => {
    const { dialog } = open({ mode: 'limit', member: LEI });

    type(dialog, '1000.01');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    expect(state.setLimit).not.toHaveBeenCalled();
    expect(within(dialog).getByRole('alert')).toHaveTextContent(
      'A member cannot have a limit above the limit of the organization, $1,000.00.',
    );
  });

  it('has no ceiling when the organization has no limit, and no default to fall back to', async () => {
    const { dialog } = open(
      { mode: 'limit', member: LEI },
      { ...ORGANIZATION, monthly_limit_usd: null, default_member_monthly_limit_usd: null },
    );

    expect(dialog).toHaveTextContent('Leave empty for no limit of its own.');
    type(dialog, '5000');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(state.setLimit).toHaveBeenCalled());
  });

  it('writes nothing when the limit is left as it was', () => {
    const { dialog, onClose } = open({ mode: 'limit', member: LEI });

    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    expect(state.setLimit).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it('writes nothing for the same amount typed another way', () => {
    const { dialog, onClose } = open({ mode: 'limit', member: LEI });

    type(dialog, '80.0');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    expect(state.setLimit).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it.each([
    // The platform administrator lowered the limit of the organization below it.
    ['above the limit of the organization', 1500],
    // A migrated amount, with more decimal places than a new one may have.
    ['with six decimal places', 80.123456],
  ])('closes on a limit %s that is left as it was read', (_what, stored) => {
    const { dialog, onClose } = open({
      mode: 'limit',
      member: { ...LEI, monthly_limit_usd: stored, own_monthly_limit_usd: stored },
    });

    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
    expect(state.setLimit).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it('keeps the dialog open with what the gateway said when the save is refused', async () => {
    state.setLimit.mockRejectedValue(new Error('The limit of an administrator is not set here.'));
    const { dialog, onClose } = open({ mode: 'limit', member: LEI });

    type(dialog, '50');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'The limit of an administrator is not set here.',
    );
    expect(onClose).not.toHaveBeenCalled();
    expect(state.success).not.toHaveBeenCalled();
  });

  it('types and sends a native limit with its currency on a ledger that is not in USD', async () => {
    const onClose = vi.fn();
    const member = {
      ...LEI,
      currency: 'CNY',
      monthly_limit: 500,
      monthly_limit_usd: 74.4269,
      own_monthly_limit: 500,
      own_monthly_limit_usd: 74.4269,
      spent_month: 480,
    } as OrganizationMember;
    const organization = {
      ...ORGANIZATION,
      currency: 'CNY',
      monthly_limit: 7000,
      default_member_monthly_limit: 140,
    } as Organization;
    render(
      <SiteConfigProvider
        initialConfig={{ ...buildTimeSiteConfig, accounting: { currency: 'CNY' as const } }}
      >
        <MemberDialogs
          dialog={{ mode: 'limit', member }}
          organization={organization}
          onClose={onClose}
        />
      </SiteConfigProvider>,
    );
    const dialog = screen.getByRole('dialog');

    // The native amount, never its USD equivalent.
    expect(field(dialog, 'CNY')).toHaveValue('500');
    expect(dialog).toHaveTextContent(
      'Leave empty to use the default of the organization, ¥140.00.',
    );
    fireEvent.change(field(dialog, 'CNY'), {
      target: { value: '600' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(state.setLimit).toHaveBeenCalledWith({
        userId: 'u-lei',
        limit: { amount: 600, currency: 'CNY' },
      }),
    );
  });
});

describe('removing a member', () => {
  it('says what the removal does before it is confirmed', () => {
    const { dialog } = open({ mode: 'remove', member: LEI });

    expect(
      within(dialog).getByRole('heading', { name: 'Remove from organization' }),
    ).toBeInTheDocument();
    expect(dialog).toHaveTextContent(
      'The account of Lei Wang (lei.wang@example.edu) is suspended:',
    );
    expect(
      within(dialog)
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual([
      'its open sessions end;',
      'its API keys are revoked;',
      'it can no longer sign in, through single sign-on either.',
    ]);
    expect(dialog).toHaveTextContent('The usage history stays. You can restore the member later.');
    expect(state.remove).not.toHaveBeenCalled();
  });

  it('removes the member only when confirmed', async () => {
    const { dialog, onClose } = open({ mode: 'remove', member: LEI });

    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove member' }));

    await waitFor(() => expect(state.remove).toHaveBeenCalledWith('u-lei'));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(state.success).toHaveBeenCalledWith('Member removed.');
  });

  it('removes nobody when it is cancelled', () => {
    const { dialog, onClose } = open({ mode: 'remove', member: LEI });

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    expect(state.remove).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it('shows the refusal of the gateway in the dialog', async () => {
    state.remove.mockRejectedValue(new Error('An administrator cannot be removed here.'));
    const { dialog, onClose } = open({ mode: 'remove', member: LEI });

    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove member' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'An administrator cannot be removed here.',
    );
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('restoring a member', () => {
  it('says that the revoked keys stay revoked, and restores when confirmed', async () => {
    const member = { ...LEI, status: 'suspended', removed: true };
    const { dialog, onClose } = open({ mode: 'restore', member });

    expect(dialog).toHaveTextContent(
      'The account of Lei Wang (lei.wang@example.edu) becomes active again, and the member can sign in.',
    );
    expect(dialog).toHaveTextContent(
      'The revoked API keys stay revoked. The member makes new ones.',
    );
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restore member' }));

    await waitFor(() => expect(state.restore).toHaveBeenCalledWith('u-lei'));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(state.success).toHaveBeenCalledWith('Member restored.');
  });
});

describe('the models of a member', () => {
  const box = (dialog: HTMLElement, model: string) =>
    within(dialog).getByRole('checkbox', { name: new RegExp(`^${model}`) });

  it('reads the models of the member, and checks those that are on', () => {
    const { dialog } = open({ mode: 'models', member: LEI });

    expect(state.models).toHaveBeenCalledWith('u-lei');
    expect(dialog).toHaveTextContent(
      'The models that Lei Wang (lei.wang@example.edu) can use. A model that is not checked is refused for this member.',
    );
    expect(box(dialog, 'deepseek-v3.2')).toBeChecked();
    expect(box(dialog, 'glm-4.6')).not.toBeChecked();
    expect(box(dialog, 'qwen-max')).toBeChecked();
  });

  it('shows a model that the platform administrator turned off, and does not let it change', () => {
    const { dialog } = open({ mode: 'models', member: LEI });

    const locked = box(dialog, 'big-model');
    expect(locked).not.toBeChecked();
    expect(locked).toBeDisabled();
    expect(dialog).toHaveTextContent('disabled by the platform administrator');
  });

  it('saves the list of the organization only, and says so', async () => {
    const { dialog, onClose } = open({ mode: 'models', member: LEI });

    fireEvent.click(box(dialog, 'qwen-max'));
    fireEvent.click(box(dialog, 'glm-4.6'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    // Never the model of the platform administrator's list.
    await waitFor(() =>
      expect(state.setModels).toHaveBeenCalledWith({
        userId: 'u-lei',
        disabledModels: ['qwen-max'],
      }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(state.success).toHaveBeenCalledWith('Models saved.');
  });

  it('writes nothing when the list is left as it was', () => {
    const { dialog, onClose } = open({ mode: 'models', member: LEI });

    fireEvent.click(box(dialog, 'qwen-max'));
    fireEvent.click(box(dialog, 'qwen-max'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    expect(state.setModels).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it('keeps a model that is off in the list when the platform no longer offers it', () => {
    state.models.mockReturnValue({
      data: { available: ['qwen-max'], disabled_models: ['retired'], platform_disabled_models: [] },
      error: null,
      isLoading: false,
      refetch: vi.fn(),
    });

    const { dialog } = open({ mode: 'models', member: LEI });

    expect(box(dialog, 'retired')).not.toBeChecked();
    expect(box(dialog, 'retired')).toBeEnabled();
  });

  it('does not save before the models are read, and offers to read them again', () => {
    const again = vi.fn();
    state.models.mockReturnValue({
      data: undefined,
      error: new Error('The gateway is not reachable.'),
      isLoading: false,
      refetch: again,
    });

    const { dialog } = open({ mode: 'models', member: LEI });

    expect(within(dialog).getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(within(dialog).getByRole('alert')).toHaveTextContent('The gateway is not reachable.');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Retry' }));
    expect(again).toHaveBeenCalledTimes(1);
  });

  it('keeps the dialog open with what the gateway said when the save is refused', async () => {
    state.setModels.mockRejectedValue(new Error('Unknown model: qwen-max'));
    const { dialog, onClose } = open({ mode: 'models', member: LEI });

    fireEvent.click(box(dialog, 'qwen-max'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));

    expect(await within(dialog).findByText('Unknown model: qwen-max')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('revoking an API key of a member', () => {
  const KEY: MemberKey = {
    id: 41,
    name: 'laptop',
    status: 'active',
    project_id: 'p-1',
    project_name: 'Thesis',
    created_at: '2026-10-10T12:00:00Z',
    spent_today_usd: 0,
    spent_month_usd: 0,
  };

  it('says what the revoking does, and revokes the key only when confirmed', async () => {
    const { dialog, onClose } = open({ mode: 'revoke-key', member: LEI, apiKey: KEY });

    expect(dialog).toHaveTextContent(
      'The API key "laptop" of Lei Wang (lei.wang@example.edu) stops working at once.',
    );
    expect(dialog).toHaveTextContent('A revoked key stays revoked. The member can make a new one.');
    expect(state.revokeKey).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Revoke key' }));

    await waitFor(() =>
      expect(state.revokeKey).toHaveBeenCalledWith({ userId: 'u-lei', keyId: 41 }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(state.success).toHaveBeenCalledWith('API key revoked.');
  });

  it('names a key that has no name, and revokes nothing when it is cancelled', () => {
    const { dialog, onClose } = open({
      mode: 'revoke-key',
      member: LEI,
      apiKey: { ...KEY, name: null },
    });

    expect(dialog).toHaveTextContent('The API key "Unnamed key" of');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    expect(state.revokeKey).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it('shows the refusal of the gateway in the dialog', async () => {
    state.revokeKey.mockRejectedValue(new Error('This API key is not active.'));
    const { dialog, onClose } = open({ mode: 'revoke-key', member: LEI, apiKey: KEY });

    fireEvent.click(within(dialog).getByRole('button', { name: 'Revoke key' }));

    expect(await within(dialog).findByText('This API key is not active.')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });
});

it('draws nothing when no dialog is open', () => {
  const { container } = render(
    <MemberDialogs dialog={null} organization={ORGANIZATION} onClose={vi.fn()} />,
  );

  expect(container).toBeEmptyDOMElement();
});
