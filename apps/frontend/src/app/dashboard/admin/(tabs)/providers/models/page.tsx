import { redirect } from 'next/navigation';

import { ROUTING_MODELS_PATH } from '@/components/features/admin/routingRoutes';

/** Models moved to the Routing tab. An old link or bookmark lands there. */
export default function Page() {
  redirect(ROUTING_MODELS_PATH);
}
