import { redirect } from 'next/navigation';

import { PROVIDERS_PATH } from '@/components/features/admin/providerRoutes';

/**
 * Availability became the registry's Routing column on Providers → Overview.
 * An old link or bookmark lands there.
 */
export default function Page() {
  redirect(PROVIDERS_PATH);
}
