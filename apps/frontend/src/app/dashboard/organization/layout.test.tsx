// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

interface TestUser {
  id: string;
  role: string;
  is_admin: boolean;
  organization: { id: string; name: string; role: 'member' | 'admin' } | null;
}

const state = vi.hoisted(() => ({
  replace: vi.fn(),
  push: vi.fn(),
  pathname: '/dashboard/organization/overview',
  auth: { loading: false, isAuthenticated: true, user: null as unknown },
  refreshUser: vi.fn(),
  // The organization as the gateway has it now; none until it is read.
  organization: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: state.replace, push: state.push }),
  usePathname: () => state.pathname,
}));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: state.auth, refreshUser: state.refreshUser }),
}));
vi.mock('@/lib/hooks', () => ({
  useOrganization: (enabled?: boolean) => state.organization(enabled),
}));
vi.mock('@/components/ui/revealHorizontally', () => ({ revealHorizontally: vi.fn() }));

import OrganizationLayout from './layout';

function signedIn(user: Partial<TestUser>) {
  state.auth = {
    loading: false,
    isAuthenticated: true,
    user: { id: 'u-1', role: 'free', is_admin: false, organization: null, ...user },
  };
}

const ADMIN_OF = { id: 'o-1', name: 'Example University', role: 'admin' as const };

beforeEach(() => {
  state.replace.mockReset();
  state.push.mockReset();
  state.pathname = '/dashboard/organization/overview';
  state.refreshUser.mockReset().mockResolvedValue(undefined);
  state.organization.mockReset().mockReturnValue({ data: undefined });
});
afterEach(cleanup);

describe('the organization console', () => {
  it('shows its frame and the page to an administrator of the organization', () => {
    signedIn({ organization: ADMIN_OF });

    render(
      <OrganizationLayout>
        <p>the page</p>
      </OrganizationLayout>,
    );

    expect(screen.getByRole('heading', { name: 'Example University' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Dashboard' })).toHaveAttribute('href', '/dashboard');
    expect(screen.getByText('the page')).toBeInTheDocument();
    expect(state.replace).not.toHaveBeenCalled();

    const tabs = screen.getByRole('navigation', { name: 'Organization sections' });
    expect(
      [...tabs.querySelectorAll('a')].map((link) => [link.textContent, link.getAttribute('href')]),
    ).toEqual([
      ['Overview', '/dashboard/organization/overview'],
      ['Members', '/dashboard/organization/members'],
      ['Usage', '/dashboard/organization/usage'],
      ['Audit log', '/dashboard/organization/audit-log'],
      ['Settings', '/dashboard/organization/settings'],
    ]);
    expect(screen.getByRole('link', { name: 'Overview' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Members' })).not.toHaveAttribute('aria-current');
  });

  it('shows the name the organization has now, and reads the profile again, after a rename', () => {
    // The profile still has the name from when the page loaded.
    signedIn({ organization: ADMIN_OF });
    state.organization.mockReturnValue({ data: { id: 'o-1', name: 'Example Institute' } });

    render(
      <OrganizationLayout>
        <p>the page</p>
      </OrganizationLayout>,
    );

    expect(screen.getByRole('heading', { name: 'Example Institute' })).toBeInTheDocument();
    // The navigation entry and the audit log take the name from the profile.
    expect(state.refreshUser).toHaveBeenCalledTimes(1);
  });

  it('leaves the profile as it is while its name is the name of the organization', () => {
    signedIn({ organization: ADMIN_OF });
    state.organization.mockReturnValue({ data: { id: 'o-1', name: 'Example University' } });

    const { rerender } = render(
      <OrganizationLayout>
        <p>the page</p>
      </OrganizationLayout>,
    );
    rerender(
      <OrganizationLayout>
        <p>another page</p>
      </OrganizationLayout>,
    );

    expect(state.organization).toHaveBeenLastCalledWith(true);
    expect(state.refreshUser).not.toHaveBeenCalled();
  });

  it('asks once for the profile when the profile comes back with the old name', () => {
    signedIn({ organization: ADMIN_OF });
    state.organization.mockReturnValue({ data: { id: 'o-1', name: 'Example Institute' } });

    const { rerender } = render(
      <OrganizationLayout>
        <p>the page</p>
      </OrganizationLayout>,
    );
    // The profile was read again and is a new object with the same old name.
    signedIn({ organization: { ...ADMIN_OF } });
    rerender(
      <OrganizationLayout>
        <p>the page</p>
      </OrganizationLayout>,
    );

    expect(state.refreshUser).toHaveBeenCalledTimes(1);
  });

  it.each([
    [
      'a member that is not an administrator',
      { organization: { ...ADMIN_OF, role: 'member' as const } },
    ],
    ['an account in no organization', { organization: null }],
    // The deployment's administrator has no role in an organization.
    ['a platform administrator', { role: 'admin', is_admin: true, organization: null }],
  ])('sends %s back to the dashboard and shows nothing of the page', (_who, user) => {
    signedIn(user);

    render(
      <OrganizationLayout>
        <p>the page</p>
      </OrganizationLayout>,
    );

    expect(state.replace).toHaveBeenCalledWith('/dashboard');
    expect(screen.queryByText('the page')).not.toBeInTheDocument();
    // No organization route is called for an account that is not an administrator of one.
    expect(state.organization).toHaveBeenLastCalledWith(false);
    expect(state.refreshUser).not.toHaveBeenCalled();
    expect(
      screen.getByRole('heading', { name: 'Organization administrator access required' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Back to Dashboard' })).toHaveAttribute(
      'href',
      '/dashboard',
    );
  });

  it('sends a visitor that is not signed in to the sign-in page', () => {
    state.auth = { loading: false, isAuthenticated: false, user: null };

    render(
      <OrganizationLayout>
        <p>the page</p>
      </OrganizationLayout>,
    );

    expect(state.push).toHaveBeenCalledWith('/login');
    expect(state.replace).not.toHaveBeenCalled();
    expect(screen.queryByText('the page')).not.toBeInTheDocument();
  });

  it('decides nothing while the session is still loading', () => {
    state.auth = { loading: true, isAuthenticated: false, user: null };

    render(
      <OrganizationLayout>
        <p>the page</p>
      </OrganizationLayout>,
    );

    expect(state.replace).not.toHaveBeenCalled();
    expect(state.push).not.toHaveBeenCalled();
    expect(screen.queryByText('the page')).not.toBeInTheDocument();
  });

  it('marks the tab of the page being shown', () => {
    signedIn({ organization: ADMIN_OF });
    state.pathname = '/dashboard/organization/audit-log';

    render(
      <OrganizationLayout>
        <p>the page</p>
      </OrganizationLayout>,
    );

    expect(screen.getByRole('link', { name: 'Audit log' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Overview' })).not.toHaveAttribute('aria-current');
  });
});
