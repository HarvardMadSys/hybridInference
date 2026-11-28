import { redirect } from 'next/navigation';

import { PROVIDERS_PATH, PROVIDER_KEYS_ANCHOR } from '@/components/features/admin/providerRoutes';

/** Keys became a section of Providers → Overview. An old link or bookmark lands there. */
export default function Page() {
  redirect(`${PROVIDERS_PATH}#${PROVIDER_KEYS_ANCHOR}`);
}
