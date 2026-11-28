import { redirect } from 'next/navigation';

import { ADMIN_MODELS_PATH } from '@/components/features/configuration/paths';

/** Models became a section of Routing. An old link or bookmark lands there. */
export default function Page() {
  redirect(ADMIN_MODELS_PATH);
}
