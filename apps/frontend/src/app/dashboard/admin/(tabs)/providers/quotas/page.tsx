import { redirect } from 'next/navigation';

import { PROVIDERS_PATH, PROVIDER_QUOTAS_ANCHOR } from '@/components/features/admin/providerRoutes';

/** Quotas became a section of Providers → Overview. An old link or bookmark lands there. */
export default function Page() {
  redirect(`${PROVIDERS_PATH}#${PROVIDER_QUOTAS_ANCHOR}`);
}
