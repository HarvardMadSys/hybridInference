// The Anthropic Messages surface, on the gateway.
// Forwarded by a route handler, not a rewrite, because these responses
// stream: see src/lib/server/gatewayProxy.ts.
import { gatewayRoute } from '@/lib/server/gatewayProxy';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

const relay = gatewayRoute('/anthropic');

export {
  relay as GET,
  relay as HEAD,
  relay as POST,
  relay as PUT,
  relay as PATCH,
  relay as DELETE,
  relay as OPTIONS,
};
