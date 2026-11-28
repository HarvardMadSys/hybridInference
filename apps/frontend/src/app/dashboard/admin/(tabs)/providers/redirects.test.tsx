import { describe, expect, it, vi } from 'vitest';

// Next's redirect never returns: it throws, and the router follows it.
const redirectMock = vi.hoisted(() =>
  vi.fn((url: string) => {
    throw new Error(`NEXT_REDIRECT ${url}`);
  }),
);

vi.mock('next/navigation', () => ({ redirect: redirectMock }));

import AvailabilityPage from './availability/page';
import KeysPage from './keys/page';
import ModelsPage from './models/page';
import QuotasPage from './quotas/page';

describe('the old Providers addresses', () => {
  it('land on the section of Overview that took each one in', () => {
    expect(() => KeysPage()).toThrow('NEXT_REDIRECT /dashboard/admin/providers#keys');
    expect(() => QuotasPage()).toThrow('NEXT_REDIRECT /dashboard/admin/providers#quotas');
    // Availability is the registry's Routing column now.
    expect(() => AvailabilityPage()).toThrow('NEXT_REDIRECT /dashboard/admin/providers');
  });

  it('send Models to the Routing tab, where it moved', () => {
    expect(() => ModelsPage()).toThrow('NEXT_REDIRECT /dashboard/admin/routing/models');
  });
});
