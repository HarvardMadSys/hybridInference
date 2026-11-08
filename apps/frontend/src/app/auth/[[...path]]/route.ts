// Sign-in, sign-up and the session, on the gateway. Forwarded by a route
// handler rather than a rewrite, which would copy each request body into the
// console's memory: see src/lib/server/gatewayProxy.ts.
import { gatewayRoute } from '@/lib/server/gatewayProxy';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const relay = gatewayRoute('/auth');

export {
  relay as GET,
  relay as HEAD,
  relay as POST,
  relay as PUT,
  relay as PATCH,
  relay as DELETE,
  relay as OPTIONS,
};
