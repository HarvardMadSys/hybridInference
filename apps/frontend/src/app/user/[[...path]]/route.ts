// The signed-in user's dashboard API, on the gateway, batch uploads included.
// Forwarded by a route handler rather than a rewrite, which would copy each
// request body into the console's memory and cut it off at
// `middlewareClientMaxBodySize`: see src/lib/server/gatewayProxy.ts.
import { gatewayRoute } from '@/lib/server/gatewayProxy';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const relay = gatewayRoute('/user');

export {
  relay as GET,
  relay as HEAD,
  relay as POST,
  relay as PUT,
  relay as PATCH,
  relay as DELETE,
  relay as OPTIONS,
};
