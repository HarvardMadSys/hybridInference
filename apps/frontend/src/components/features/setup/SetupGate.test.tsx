// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { buildTimeSiteConfig, type RuntimeSiteConfig } from '@/config/site-config';

const navigation = vi.hoisted(() => ({ pathname: '/', replace: vi.fn() }));

vi.mock('next/navigation', () => ({
  usePathname: () => navigation.pathname,
  useRouter: () => ({ replace: navigation.replace, push: vi.fn(), refresh: vi.fn() }),
}));

import { SetupGate, isSetupPath } from './SetupGate';

function renderAt(pathname: string, required: boolean | undefined) {
  navigation.pathname = pathname;
  const config =
    required === undefined
      ? // A configuration object from before the key existed.
        ({ ...buildTimeSiteConfig, setup: undefined } as unknown as RuntimeSiteConfig)
      : { ...buildTimeSiteConfig, setup: { required } };
  return render(
    <SiteConfigProvider initialConfig={config}>
      <SetupGate>
        <p>PAGE</p>
      </SetupGate>
    </SiteConfigProvider>,
  );
}

afterEach(() => {
  cleanup();
  navigation.replace.mockReset();
});

describe('SetupGate', () => {
  it.each(['/', '/login', '/dashboard', '/dashboard/admin/users', '/terms'])(
    'sends %s to /setup while setup is pending, without rendering it',
    (pathname) => {
      renderAt(pathname, true);

      expect(navigation.replace).toHaveBeenCalledWith('/setup');
      expect(screen.queryByText('PAGE')).not.toBeInTheDocument();
      expect(screen.getByRole('status')).toHaveTextContent('Opening first-run setup');
    },
  );

  it.each(['/setup', '/setup/'])('renders %s itself', (pathname) => {
    renderAt(pathname, true);

    expect(screen.getByText('PAGE')).toBeInTheDocument();
    expect(navigation.replace).not.toHaveBeenCalled();
  });

  it('stays out of the way once setup is done', () => {
    renderAt('/dashboard', false);

    expect(screen.getByText('PAGE')).toBeInTheDocument();
    expect(navigation.replace).not.toHaveBeenCalled();
  });

  it('treats a configuration without the key as set up', () => {
    renderAt('/dashboard', undefined);

    expect(screen.getByText('PAGE')).toBeInTheDocument();
    expect(navigation.replace).not.toHaveBeenCalled();
  });
});

describe('isSetupPath', () => {
  it('matches the setup page only', () => {
    expect(isSetupPath('/setup')).toBe(true);
    expect(isSetupPath('/setup/')).toBe(true);
    expect(isSetupPath('/setup-guide')).toBe(false);
    expect(isSetupPath('/dashboard/setup')).toBe(false);
    expect(isSetupPath(null)).toBe(false);
  });
});
