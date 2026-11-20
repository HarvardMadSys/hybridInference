// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig } from '@/config/site-config';
import type { Organization, OrganizationConnection } from '@/lib/api/organization';

const state = vi.hoisted(() => ({
  organization: vi.fn(),
  save: vi.fn(),
  refetch: vi.fn(),
  success: vi.fn(),
}));
vi.mock('@/lib/hooks', () => ({
  useOrganization: () => state.organization(),
  useSetDefaultMemberLimit: () => ({ mutateAsync: state.save, isPending: false }),
}));
vi.mock('@/lib/hooks/useAccountToast', () => ({
  useAccountToast: () => ({ success: state.success, error: vi.fn() }),
}));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: 'u-yu' } } }),
}));

import { SettingsView } from './SettingsView';

const CONNECTION: OrganizationConnection = {
  provider_id: 'example-univ',
  available: true,
  display_name: 'Example University',
  type: 'oidc',
  domains: ['example.edu', 'alumni.example.edu'],
  enabled: true,
  auto_create: true,
  require_approval: false,
};

const ORGANIZATION: Organization = {
  id: '01M7R5Z77ZWRJ5SQWHEZR6ACQ6',
  name: 'Example University',
  status: 'active',
  created_at: '2026-11-01T12:00:00Z',
  member_count: 14,
  monthly_limit_usd: 1000,
  default_member_monthly_limit_usd: 40,
  spent_month_usd: 295.3,
  connections: [CONNECTION],
  administrators: [
    {
      user_id: 'u-jing',
      email: 'jing.yang@example.edu',
      user_name: 'Jing Yang',
      last_login_at: null,
    },
    { user_id: 'u-yu', email: 'yu.chen@example.edu', user_name: 'Yu Chen', last_login_at: null },
  ],
};

function answer(data: Organization | undefined, more: Record<string, unknown> = {}) {
  state.organization.mockReturnValue({
    data,
    error: null,
    isLoading: false,
    refetch: state.refetch,
    ...more,
  });
}

/** The value beside a term of a list of facts. */
function fact(term: string, root: HTMLElement = document.body): string {
  return within(root).getByText(term).nextElementSibling?.textContent ?? '';
}

function section(name: string): HTMLElement {
  return screen.getByRole('region', { name });
}

function field(currency = 'USD'): HTMLElement {
  return screen.getByLabelText(`Default monthly limit for a member (${currency})`);
}

function save(): HTMLElement {
  return screen.getByRole('button', { name: 'Save' });
}

beforeEach(() => {
  state.organization.mockReset();
  state.save.mockReset().mockResolvedValue(ORGANIZATION);
  state.refetch.mockReset();
  state.success.mockReset();
});
afterEach(cleanup);

describe('the organization as it is set up', () => {
  it('shows its record', () => {
    answer(ORGANIZATION);

    render(<SettingsView />);

    const record = section('Organization');
    expect(fact('Name', record)).toBe('Example University');
    expect(fact('ID', record)).toBe('01M7R5Z77ZWRJ5SQWHEZR6ACQ6');
    expect(fact('Created', record)).toBe('Nov 1, 2026');
    expect(fact('Members', record)).toBe('14');
  });

  it('shows each SSO connection with its domains and what a first sign-in does', () => {
    answer(ORGANIZATION);

    render(<SettingsView />);

    const sso = section('Single sign-on');
    expect(within(sso).getByRole('heading', { name: 'Example University' })).toBeInTheDocument();
    expect(sso).toHaveTextContent('Enabled');
    expect(fact('Connection', sso)).toBe('example-univ');
    expect(fact('Email domains', sso)).toBe('example.edu, alumni.example.edu');
    expect(fact('First sign-in', sso)).toBe('Creates an account that works at once.');
  });

  it.each([
    [
      { auto_create: true, require_approval: true },
      'Creates an account that waits for the approval of the platform administrator.',
    ],
    [
      { auto_create: false, require_approval: false },
      'Does not create an account. The platform administrator creates the accounts.',
    ],
    // Approval means nothing for a connection that creates no account.
    [
      { auto_create: false, require_approval: true },
      'Does not create an account. The platform administrator creates the accounts.',
    ],
  ])('says what a first sign-in does for %j', (flags, words) => {
    answer({ ...ORGANIZATION, connections: [{ ...CONNECTION, ...flags }] });

    render(<SettingsView />);

    expect(fact('First sign-in', section('Single sign-on'))).toBe(words);
  });

  it('marks a connection that is turned off, and names one without a display name by its id', () => {
    answer({
      ...ORGANIZATION,
      connections: [{ ...CONNECTION, display_name: null, enabled: false }],
    });

    render(<SettingsView />);

    const sso = section('Single sign-on');
    expect(within(sso).getByRole('heading', { name: 'example-univ' })).toBeInTheDocument();
    expect(sso).toHaveTextContent('Disabled');
    expect(sso).not.toHaveTextContent('Enabled');
  });

  it('says what to do about a connection whose provider is gone', () => {
    answer({
      ...ORGANIZATION,
      connections: [
        {
          provider_id: 'old-idp',
          available: false,
          domains: [],
          enabled: false,
          auto_create: false,
          require_approval: false,
        },
      ],
    });

    render(<SettingsView />);

    const sso = section('Single sign-on');
    expect(sso).toHaveTextContent('Not available');
    expect(sso).toHaveTextContent(
      'The deployment has no provider old-idp any more, so nobody signs in through this connection. Ask the platform administrator to attach its new id.',
    );
    // Nothing is known of it, so nothing is shown as if it were.
    expect(within(sso).queryByText('Email domains')).not.toBeInTheDocument();
    expect(sso).not.toHaveTextContent('Disabled');
  });

  it('says so when no connection is attached', () => {
    answer({ ...ORGANIZATION, connections: [] });

    render(<SettingsView />);

    expect(section('Single sign-on')).toHaveTextContent(
      'No SSO connection is attached, so no new account becomes a member.',
    );
  });

  it('lists the organization administrators, the reader marked', () => {
    answer(ORGANIZATION);

    render(<SettingsView />);

    const administrators = section('Organization administrators');
    expect(administrators).toHaveTextContent(
      'The platform administrator gives this role and takes it back.',
    );
    const [jing, yu] = within(administrators).getAllByRole('row');
    expect(jing).toHaveTextContent('Jing Yang');
    expect(jing).toHaveTextContent('jing.yang@example.edu');
    expect(jing).toHaveTextContent('Last sign-in Never');
    expect(jing).not.toHaveTextContent('You');
    expect(yu).toHaveTextContent('Yu Chen');
    expect(yu).toHaveTextContent('You');
    // The role is given elsewhere: the list offers no action.
    expect(within(administrators).queryByRole('button')).not.toBeInTheDocument();
  });

  it('shows the limit of the organization as something the platform administrator sets', () => {
    answer(ORGANIZATION);

    render(<SettingsView />);

    const limits = section('Limits');
    expect(limits).toHaveTextContent('Monthly limit of the organization$1,000.00');
    expect(limits).toHaveTextContent(
      '$295.30 spent this month (UTC). Set by the platform administrator.',
    );
  });

  it('says No limit for an organization without one', () => {
    answer({ ...ORGANIZATION, monthly_limit_usd: null });

    render(<SettingsView />);

    expect(section('Limits')).toHaveTextContent('Monthly limit of the organizationNo limit');
  });

  it('offers to try again when the organization cannot be read', () => {
    answer(undefined, { error: new Error('The gateway is not reachable.') });

    render(<SettingsView />);

    expect(screen.getByRole('alert')).toHaveTextContent('The gateway is not reachable.');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(state.refetch).toHaveBeenCalledTimes(1);
  });

  it('shows that it is loading', () => {
    answer(undefined, { isLoading: true });

    render(<SettingsView />);

    expect(screen.getByRole('status')).toHaveTextContent('Loading...');
  });
});

describe('the default monthly limit of a member', () => {
  it('opens with the default as it is, with nothing to save', () => {
    answer(ORGANIZATION);

    render(<SettingsView />);

    expect(field()).toHaveValue('40');
    expect(save()).toBeDisabled();
    expect(field()).toHaveAccessibleDescription(
      'Applies at once to each member that has no monthly limit of its own. Leave empty for no default.',
    );
  });

  it('saves a new default in the ledger currency and says so', async () => {
    answer(ORGANIZATION);

    render(<SettingsView />);
    fireEvent.change(field(), { target: { value: '45.5' } });
    expect(save()).toBeEnabled();
    fireEvent.click(save());

    await waitFor(() => expect(state.save).toHaveBeenCalledWith({ amount: 45.5, currency: 'USD' }));
    await waitFor(() => expect(state.success).toHaveBeenCalledWith('Default monthly limit saved.'));
  });

  it('shows the default the organization comes back with, with nothing left to save', async () => {
    answer(ORGANIZATION);
    const { rerender } = render(<SettingsView />);
    fireEvent.change(field(), { target: { value: '45.50' } });
    fireEvent.click(save());
    await waitFor(() => expect(state.save).toHaveBeenCalled());

    answer({ ...ORGANIZATION, default_member_monthly_limit_usd: 45.5 });
    rerender(<SettingsView />);

    await waitFor(() => expect(field()).toHaveValue('45.5'));
    expect(save()).toBeDisabled();
  });

  it('clears the default when the field is emptied', async () => {
    answer(ORGANIZATION);

    render(<SettingsView />);
    fireEvent.change(field(), { target: { value: ' ' } });
    fireEvent.submit(field().closest('form') as HTMLFormElement);

    await waitFor(() => expect(state.save).toHaveBeenCalledWith({ amount: null, currency: 'USD' }));
    await waitFor(() =>
      expect(state.success).toHaveBeenCalledWith('Default monthly limit cleared.'),
    );
  });

  it('sets a default where there was none', async () => {
    answer({ ...ORGANIZATION, default_member_monthly_limit_usd: null });

    render(<SettingsView />);
    expect(field()).toHaveValue('');
    expect(save()).toBeDisabled();
    fireEvent.change(field(), { target: { value: '0' } });
    fireEvent.click(save());

    // Zero is a default that refuses every request, not no default.
    await waitFor(() => expect(state.save).toHaveBeenCalledWith({ amount: 0, currency: 'USD' }));
  });

  // Number() reads 0x10 as 16 and 1e3 as 1000; neither is what was typed.
  it.each(['-5', 'forty', '0x10', '1e3', '9.12345'])(
    'refuses %j instead of saving another default, or none',
    (value) => {
      const invalid = 'Enter an amount in USD, such as 5 or 12.50, or leave it empty.';
      answer(ORGANIZATION);

      render(<SettingsView />);
      fireEvent.change(field(), { target: { value } });
      fireEvent.click(save());

      expect(state.save).not.toHaveBeenCalled();
      expect(screen.getByRole('alert')).toHaveTextContent(invalid);
      expect(field()).toHaveAttribute('aria-invalid', 'true');
      expect(field()).toHaveAccessibleDescription(invalid);

      // The complaint goes when the amount is typed again.
      fireEvent.change(field(), { target: { value: '5' } });
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      expect(field()).not.toHaveAttribute('aria-invalid');
    },
  );

  it('has nothing to save for the same amount typed another way', () => {
    answer(ORGANIZATION);

    render(<SettingsView />);
    fireEvent.change(field(), { target: { value: '40.0' } });

    // 40.0 is the 40 that is stored: a click would send it again, each time.
    expect(save()).toBeDisabled();
    fireEvent.submit(field().closest('form') as HTMLFormElement);
    expect(state.save).not.toHaveBeenCalled();
  });

  it('refuses a default above the limit of the organization', () => {
    answer(ORGANIZATION);

    render(<SettingsView />);
    fireEvent.change(field(), { target: { value: '1000.01' } });
    fireEvent.click(save());

    expect(state.save).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'The default cannot be above the limit of the organization, $1,000.00.',
    );
  });

  it('has no ceiling when the organization has no limit', async () => {
    answer({ ...ORGANIZATION, monthly_limit_usd: null });

    render(<SettingsView />);
    fireEvent.change(field(), { target: { value: '5000' } });
    fireEvent.click(save());

    await waitFor(() => expect(state.save).toHaveBeenCalledWith({ amount: 5000, currency: 'USD' }));
  });

  it('shows what the gateway said when it refuses the default', async () => {
    state.save.mockRejectedValue(new Error('The default cannot exceed the organization limit.'));
    answer(ORGANIZATION);

    render(<SettingsView />);
    fireEvent.change(field(), { target: { value: '60' } });
    fireEvent.click(save());

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The default cannot exceed the organization limit.',
    );
    expect(state.success).not.toHaveBeenCalled();
    // What was typed stays, to be corrected.
    expect(field()).toHaveValue('60');
  });

  it('types and sends a native default with its currency on a ledger that is not in USD', async () => {
    answer({
      ...ORGANIZATION,
      currency: 'CNY',
      monthly_limit: 7000,
      default_member_monthly_limit: 280,
      spent_month: 2004.11,
    });

    render(
      <SiteConfigProvider
        initialConfig={{ ...buildTimeSiteConfig, accounting: { currency: 'CNY' as const } }}
      >
        <SettingsView />
      </SiteConfigProvider>,
    );

    const limits = section('Limits');
    expect(limits).toHaveTextContent('Monthly limit of the organization¥7,000.00');
    expect(limits).toHaveTextContent('¥2,004.11 spent this month (UTC).');
    // The native amount, never the USD equivalent of 40.
    expect(field('CNY')).toHaveValue('280');
    fireEvent.change(field('CNY'), { target: { value: '300' } });
    fireEvent.click(save());

    await waitFor(() => expect(state.save).toHaveBeenCalledWith({ amount: 300, currency: 'CNY' }));
  });
});
