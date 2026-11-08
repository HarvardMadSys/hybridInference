// The admin API, on the gateway. Forwarded by a route handler rather than a
// rewrite, which would copy each request body -- a configuration restore, say --
// into the console's memory: see src/lib/server/gatewayProxy.ts.
import { gatewayRoute } from '@/lib/server/gatewayProxy';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const relay = gatewayRoute('/admin');

export {
  relay as GET,
  relay as HEAD,
  relay as POST,
  relay as PUT,
  relay as PATCH,
  relay as DELETE,
  relay as OPTIONS,
};
